"""kb views: 知识库管理 + RAG 问答 + SSE 流式端点。"""
from __future__ import annotations

import json
import re
import shutil
import unicodedata
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404, redirect, render

from .models import Conversation, Document, KnowledgeBase, Message


# ------------------------------------------------------------------
# 工具
# ------------------------------------------------------------------


def _derive_unique_slug(original_name: str) -> str:
    """从名称派生一个合法、全局唯一、ASCII 的 slug。

    纯中文名没有 ASCII 部分，用短 hash 保证可区分（比全部塌成 "doc" 好）。
    """
    stem = Path(original_name).stem
    ascii_part = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    tokens = re.findall(r"[A-Za-z0-9]+", ascii_part)
    if tokens:
        base = "-".join(t.lower() for t in tokens)
    else:
        # 无 ASCII（纯中文等）：用名字的短 hash 保证不同名不撞
        import hashlib
        base = "kb-" + hashlib.md5(original_name.encode("utf-8")).hexdigest()[:8]
    base = re.sub(r"-{2,}", "-", base).strip("-")[:60] or "kb"
    used = set(KnowledgeBase.objects.values_list("slug", flat=True))
    slug, n = base, 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _scope_docs(kb: KnowledgeBase):
    """文件夹 → 其下所有子文档库的文档（扁平）；文档库 → 自身文档。"""
    if kb.is_folder:
        child_ids = list(kb.children.filter(is_folder=False).values_list("id", flat=True))
        return Document.objects.filter(kb_id__in=child_ids)
    return kb.documents.all()


def _scope_kb_ids(kb: KnowledgeBase) -> list:
    """文件夹 → [自身(不挂文档)] ∪ 子库 id 列表（用于按 id 找文档）；
    文档库 → [自身 id]。"""
    if kb.is_folder:
        return list(kb.children.filter(is_folder=False).values_list("id", flat=True))
    return [kb.id]


@login_required
@require_http_methods(["POST"])
def doc_page_embed(request, slug, doc_id):
    """切换文档的视觉文档模式（图纸/扫描件：整页视觉入库）。

    开启 → 后台线程渲染每页 + 多模态嵌入 + 写溯源；关闭 → 同步删除全部
    整页块（向量 + 溯源行）。权限与 doc_desc_update 一致。
    """
    from . import access as kb_access
    kb = get_object_or_404(KnowledgeBase, slug=slug)
    if not kb_access.can_manage_kb(request.user, kb):
        raise Http404("知识库不存在")
    doc = get_object_or_404(Document, id=doc_id, kb__in=_scope_kb_ids(kb))
    enable = (request.POST.get("enable") or "").strip() == "1"

    if doc.file_type != "pdf":
        return JsonResponse({"ok": False, "message": "仅 PDF 文档支持视觉模式"})
    if enable == doc.page_embed:
        return JsonResponse({"ok": True, "message": "状态未变化", "chunk_count": doc.chunk_count})

    doc.page_embed = enable
    doc.save(update_fields=["page_embed", "updated_at"])
    if enable:
        from .pipeline import _page_embed_async
        _page_embed_async(str(doc.id))
        return JsonResponse({"ok": True,
                             "message": "已开启视觉模式，页面向量后台生成中"})
    # 关闭：同步移除整页块（向量 + 溯源）
    from .pipeline import _remove_page_chunks
    n = _remove_page_chunks(doc.kb.slug, doc.id)
    if n:
        doc.chunk_count = max(0, doc.chunk_count - n)
        doc.save(update_fields=["chunk_count", "updated_at"])
        _recount(doc.kb)
    return JsonResponse({"ok": True, "message": f"已关闭视觉模式（移除 {n} 个整页块）",
                         "chunk_count": doc.chunk_count})


def _recount(kb: KnowledgeBase) -> None:
    """重算一个（子/顶层）库的 doc_count/chunk_count（仅 completed 文档）。"""
    docs = kb.documents.filter(status=Document.Status.COMPLETED)
    kb.doc_count = docs.count()
    kb.chunk_count = sum(d.chunk_count for d in docs)
    kb.save(update_fields=["doc_count", "chunk_count", "updated_at"])


def _create_manual_document(kb: KnowledgeBase, upload, user,
                            page_embed: bool = False) -> Document:
    """把手册上传到指定顶层库；文件夹库自动创建隔离的子文档库。

    page_embed：视觉文档模式（图纸/扫描件）——PDF 每页渲染成图入多模态
    向量库，OCR 文字少的图纸页也能被自然语言搜到。
    """
    from django.db import transaction

    fname = Path(upload.name).name
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    file_types = {
        "pdf": "pdf", "md": "md", "markdown": "md", "txt": "txt",
        # 照片直传：JPG/PNG/WebP 走多模态索引
        "jpg": "image", "jpeg": "image", "png": "image", "webp": "image",
    }
    if ext not in file_types:
        raise ValueError("仅支持 PDF、Markdown（.md）、TXT 和图片（JPG/PNG/WebP）文件。")

    with transaction.atomic():
        if kb.is_folder:
            target_kb = KnowledgeBase.objects.create(
                name=fname[:80],
                slug=_derive_unique_slug(fname),
                description=f"自动创建：{fname}",
                is_folder=False,
                parent=kb,
                department=kb.department,  # 子库继承父库部门，保持检索可见性一致
                created_by=user,
            )
        else:
            target_kb = kb
        return Document.objects.create(
            kb=target_kb,
            original_name=fname,
            file=upload,
            file_type=file_types[ext],
            page_embed=bool(page_embed) and file_types[ext] == "pdf",
            status=Document.Status.PENDING,
        )
_is_staff = user_passes_test(lambda u: u.is_staff)
_is_manager = user_passes_test(
    lambda u: u.is_staff or u.is_superuser
    or (getattr(u, "is_authenticated", False) and _mgr_role(u))
)


def _mgr_role(u) -> bool:
    from accounts.models import UserProfile, user_role
    return user_role(u) == UserProfile.Role.DEPT_ADMIN


# ------------------------------------------------------------------
# 管理页面（全局管理员 或 部门管理员）
# ------------------------------------------------------------------
@_is_manager
@login_required
def manage_list(request):
    """统一资料上传中心 + 手册库管理。

    手册上传进指定手册库；业务表按类型导入结构化数据层。
    """
    from . import access as kb_access
    if request.method == "POST":
        action = (request.POST.get("action") or "create_kb").strip()

        if action == "create_kb":
            name = request.POST.get("name", "").strip()
            department = (request.POST.get("department") or "").strip()
            # 部门只能从已有部门池中选；池外值回落「通用」。
            # 部门管理员建库强制归属本部门（不能替别的部门建库）。
            if department not in kb_access.all_departments():
                department = ""
            if not kb_access.is_global_admin(request.user):
                department = kb_access.user_department(request.user)
            if name:
                from .models import DEPARTMENT_GENERAL
                slug = _derive_unique_slug(name)
                KnowledgeBase.objects.create(
                    name=name, slug=slug,
                    description=(request.POST.get("description") or "").strip()[:200],
                    created_by=request.user,
                    is_folder=True, parent=None,
                    department=department or DEPARTMENT_GENERAL,
                )
                ok, msg = True, f"手册库「{name}」已创建（部门：{department or DEPARTMENT_GENERAL}），可以上传手册了。"
            else:
                slug = ""
                ok, msg = False, "请填写手册库名称。"
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"ok": ok, "message": msg, "slug": slug})

        elif action == "upload_manual":
            kb = KnowledgeBase.objects.filter(
                slug=(request.POST.get("kb_slug") or "").strip(),
                parent__isnull=True,
            ).first()
            upload = request.FILES.get("file")
            if not kb:
                messages.error(request, "请选择手册要归入的手册库。")
            elif not upload:
                messages.error(request, "请选择要上传的手册文件。")
            elif not kb_access.can_manage_kb(request.user, kb):
                messages.error(request, "没有权限向该手册库上传（仅本部门库可管理）。")
            else:
                try:
                    doc = _create_manual_document(
                        kb, upload, request.user,
                        page_embed=request.POST.get("page_embed") == "on")
                    from .pipeline import process_document_async
                    process_document_async(doc.id)
                    messages.success(request, f"已上传手册「{doc.original_name}」，正在后台处理。")
                except ValueError as exc:
                    messages.error(request, str(exc))

        elif action == "upload_structured":
            from .structured_data import FIELD_LABELS, StructuredDataError, import_structured_dataset
            upload = request.FILES.get("file")
            kind = (request.POST.get("kind") or "").strip()
            if not upload:
                messages.error(request, "请选择 CSV 或 XLSX 文件。")
            else:
                try:
                    dataset = import_structured_dataset(upload, kind, request.user)
                    mapped = "、".join(
                        label for key, label in FIELD_LABELS.items() if key in dataset.mapping
                    ) or "未识别到标准关联字段（原始列已保留）"
                    messages.success(
                        request,
                        f"已导入 {dataset.source_name}：{dataset.row_count} 行；识别字段：{mapped}。",
                    )
                except StructuredDataError as exc:
                    messages.error(request, str(exc))
        return redirect("kb:manage_list")

    # 顶层库（文件夹 + 独立文档库）扁平渲染；子库对用户透明，不单独展示。
    # 部门管理员只看到本部门的库；全局管理员看全部。
    _kb_qs = KnowledgeBase.objects.filter(parent__isnull=True).order_by("name")
    if not kb_access.is_global_admin(request.user):
        _kb_qs = _kb_qs.filter(department=kb_access.user_department(request.user))
    kbs = list(_kb_qs)
    from .models import StructuredDataset
    datasets = StructuredDataset.objects.filter(active=True).order_by("kind", "-created_at")
    # 部门池（用户 ∪ 知识库；新部门唯一入口 = 用户管理页），表单只能从中选
    departments = kb_access.all_departments()
    return render(request, "kb/manage_list.html", {
        "kbs": kbs,
        "datasets": datasets,
        "dataset_kinds": StructuredDataset.Kind.choices,
        "departments": departments,
        "is_global_admin": kb_access.is_global_admin(request.user),
        "my_department": kb_access.user_department(request.user),
    })


@_is_manager
@login_required
def manage_detail(request, slug):
    """知识库详情。

    对用户而言：一个库（文件夹或文档库）就是一个能直接上传/查看文档的地方。
    - 文档库：文档直接挂上来（每库一个 Chroma collection）。
    - 文件夹：上传时系统背后自动为该文档建一个子文档库（每文档独占 collection，
      保持检索隔离），用户只看到文档列表，无需感知「子库」。
    """
    from . import access as kb_access
    kb = get_object_or_404(KnowledgeBase, slug=slug)
    if not kb_access.can_manage_kb(request.user, kb):
        raise Http404("知识库不存在")

    # ---------- 上传（统一入口，按格式自动路由） ----------
    # pdf/md/txt → 手册解析入库；csv/xlsx → 业务数据导入（kind 自动识别）
    if request.method == "POST" and request.FILES.get("file"):
        upload = request.FILES["file"]
        ext = Path(upload.name).suffix.lower()
        is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
        try:
            if ext in (".csv", ".tsv", ".xlsx", ".xlsm"):
                from .structured_data import (
                    FIELD_LABELS, StructuredDataError, import_structured_dataset,
                )
                dataset = import_structured_dataset(upload, "", request.user)
                mapped = "、".join(
                    label for key, label in FIELD_LABELS.items() if key in dataset.mapping
                ) or "未识别到标准关联字段（原始列已保留）"
                msg = (f"已识别为业务数据并导入「{dataset.source_name}」"
                       f"（{dataset.get_kind_display()}）：{dataset.row_count} 行；{mapped}。")
                if is_ajax:
                    return JsonResponse({"ok": True, "message": msg})
                messages.success(request, msg)
            else:
                doc = _create_manual_document(
                    kb, upload, request.user,
                    page_embed=request.POST.get("page_embed") == "on")
                from .pipeline import process_document_async
                process_document_async(doc.id)
                msg = f"已上传「{doc.original_name}」，正在后台处理…"
                if is_ajax:
                    # 批量上传（AJAX 单文件逐个提交）→ JSON（页面不跳转，队列继续）
                    return JsonResponse({"ok": True, "doc_id": str(doc.id), "message": msg})
                messages.success(request, msg)
        except ValueError as exc:
            if is_ajax:
                return JsonResponse({"ok": False, "message": str(exc)})
            messages.error(request, str(exc))
        return redirect("kb:manage_detail", slug=slug)

    # ---------- GET：渲染 ----------
    docs = _scope_docs(kb)
    docs_total, chunks_total = kb.aggregate_counts()
    return render(request, "kb/manage_detail.html", {
        "kb": kb,
        "docs": docs,
        "docs_total": docs_total,
        "chunks_total": chunks_total,
    })


@_is_manager
@login_required
def manage_delete(request, slug):
    """删除知识库（删 Chroma 目录 + DB 记录）。

    文件夹：级联删除所有子文档库（CASCADE）及其向量目录。
    幂等：库已不存在时不再 404，直接回列表并提示。
    """
    from . import access as kb_access
    kb = KnowledgeBase.objects.filter(slug=slug).first()
    if kb is None:
        messages.info(request, "该知识库不存在或已被删除。")
        return redirect("kb:manage_list")
    if not kb_access.can_manage_kb(request.user, kb):
        raise Http404("知识库不存在")
    if request.method == "POST":
        # 收集要清理向量目录的所有 slug：自身 + （文件夹的）所有子库
        slugs = [kb.slug]
        if kb.is_folder:
            slugs += list(kb.children.values_list("slug", flat=True))
        from . import keyword_index
        for s in slugs:
            chroma_dir = Path(settings.CHROMA_ROOT) / s
            if chroma_dir.exists():
                shutil.rmtree(chroma_dir, ignore_errors=True)
            md_dir = Path(settings.MD_ROOT) / s
            if md_dir.exists():
                shutil.rmtree(md_dir, ignore_errors=True)
            # 关键词索引同步清理：slug 删除后可被回收，残留行会让已删内容
            # 「复活」（甚至泄露给回收了该 slug 的其它部门库）
            keyword_index.delete_kb(s)
        name = kb.name
        kb.delete()  # CASCADE 会删掉子库及其文档
        messages.success(request, f"知识库「{name}」已删除。")
    return redirect("kb:manage_list")


@_is_manager
@login_required
@require_http_methods(["POST"])
def kb_rename(request, slug):
    """重命名知识库（名称 + 可选描述；slug 保持不变，避免迁移向量目录）。

    AJAX 优先：返回 JSON；非 AJAX 降级为重定向。
    部门管理员只能改本部门的库，且不能改库的部门归属。
    description 仅在表单携带该字段时更新（留空即清空）。
    """
    from . import access as kb_access
    kb = get_object_or_404(KnowledgeBase, slug=slug)
    if not kb_access.can_manage_kb(request.user, kb):
        raise Http404("知识库不存在")
    name = (request.POST.get("name") or "").strip()
    department = (request.POST.get("department") or "").strip()
    if not name:
        return JsonResponse({"ok": False, "message": "名称不能为空"}, status=400)
    kb.name = name
    update_fields = ["name", "updated_at"]
    if "description" in request.POST:
        kb.description = (request.POST.get("description") or "").strip()
        update_fields.append("description")
    kb.save(update_fields=update_fields)
    # 可选：一并开关追踪表。取消勾选的 checkbox 不会发包，所以用隐藏的
    # tracker_present 标记识别"表单带了开关"（列表页改名弹窗不带，不动追踪表）。
    if request.POST.get("tracker_present") == "1":
        from .models import KbTracker
        tr, _ = KbTracker.objects.get_or_create(kb=kb, defaults={"fields": []})
        tr.enabled = request.POST.get("tracker_enabled") == "on"
        tr.save(update_fields=["enabled", "updated_at"])
    # 可选：一并改部门（文件夹级联更新所有子库）；仅全局管理员可改归属，
    # 且只接受部门池内的值。
    if department and kb_access.is_global_admin(request.user):
        if department in kb_access.all_departments():
            from .access import set_kb_department
            set_kb_department(kb, department)

    is_ajax = (request.headers.get("x-requested-with") == "XMLHttpRequest"
               or "application/json" in (request.META.get("HTTP_ACCEPT") or ""))
    if is_ajax:
        kb.refresh_from_db()
        return JsonResponse({"ok": True, "name": kb.name, "department": kb.department,
                             "description": kb.description})
    messages.success(request, f"已更新「{kb.name}」。")
    return redirect("kb:manage_list")


@login_required
@require_http_methods(["POST"])
def doc_desc_update(request, slug, doc_id):
    """更新文档描述（AJAX；描述会展示给 AI 帮其判断文档相关性）。

    与 doc_delete 同款权限模式：非管理者 404（不泄露存在性）。
    """
    from . import access as kb_access
    kb = get_object_or_404(KnowledgeBase, slug=slug)
    if not kb_access.can_manage_kb(request.user, kb):
        raise Http404("知识库不存在")
    doc = get_object_or_404(Document, id=doc_id, kb__in=_scope_kb_ids(kb))
    doc.description = (request.POST.get("description") or "").strip()[:200]
    doc.save(update_fields=["description", "updated_at"])
    return JsonResponse({"ok": True, "description": doc.description})


def doc_delete(request, slug, doc_id):
    """删除某知识库下的一份文档（文件夹则跨其所有子库查找）。

    清理：① Chroma 中该文档的向量（按 source=original_name 过滤，原生客户端，
          embedding 配置缺失也能删）；② 上传的原始文件；③ DB 记录；
         ④ 文件夹下自动建的空子库一并清理。
    幂等：知识库/文档已不存在时返回 JSON 成功（前端无需报错），避免
         双击删除 / 轮询重建行后再次点击触发 404。
    """
    from . import access as kb_access
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    kb = KnowledgeBase.objects.filter(slug=slug).first()
    if kb is None:
        if is_ajax:
            return JsonResponse({"ok": True, "already_deleted": True,
                                 "message": "知识库不存在或已被删除。"})
        messages.info(request, "该知识库不存在或已被删除。")
        return redirect("kb:manage_list")
    if not kb_access.can_manage_kb(request.user, kb):
        raise Http404("知识库不存在")

    doc = Document.objects.filter(id=doc_id, kb__in=_scope_kb_ids(kb)).first()
    if doc is None:
        # 已不存在 → 幂等：AJAX 返回成功（附带提示），表单则重定向回详情
        if is_ajax:
            return JsonResponse({"ok": True, "already_deleted": True,
                                 "message": "文档不存在或已被删除。"})
        messages.info(request, "该文档不存在或已被删除。")
        return redirect("kb:manage_detail", slug=slug)

    name = doc.original_name
    doc_kb = doc.kb  # 文档实际所在的（子）库

    # ① 删 Chroma 向量（按 source 过滤；原生客户端，不依赖 embedding 配置）
    try:
        from .retriever import delete_doc_vectors
        delete_doc_vectors(doc_kb.slug, name)
    except Exception as e:  # 向量库可能还没建/为空，忽略但记录日志
        import logging
        logging.getLogger(__name__).warning("删除文档「%s」向量失败: %s", name, e)

    # ② 删上传的原始文件
    try:
        if doc.file and doc.file.name:
            doc.file.delete(save=False)
    except Exception:
        pass

    # ③ 删 DB 记录
    doc.delete()

    # ④ 文件夹下自动建的子库：文档删空了就把空壳子库也删掉（连同向量目录），
    #    否则重算该子库的缓存。
    child_purged = False
    if kb.is_folder and doc_kb.parent_id == kb.id and not doc_kb.documents.exists():
        chroma_dir = Path(settings.CHROMA_ROOT) / doc_kb.slug
        if chroma_dir.exists():
            shutil.rmtree(chroma_dir, ignore_errors=True)
        doc_kb.delete()
        child_purged = True

    # ⑤ 重算缓存：文件夹 → 只重算顶层（子库已被删或独立统计）；文档库 → 重算自身
    if kb.is_folder:
        _recount(kb)
    elif not child_purged:
        _recount(doc_kb)

    # AJAX 请求（无刷新）→ 返回最新统计 JSON；否则重定向回详情页
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        kb.refresh_from_db()
        docs_total, chunks_total = kb.aggregate_counts()
        return JsonResponse({
            "ok": True,
            "doc_id": str(doc_id),
            "name": name,
            "doc_count": docs_total,
            "chunk_count": chunks_total,
        })
    messages.success(request, f"文档「{name}」已删除。")
    return redirect("kb:manage_detail", slug=slug)


@_is_manager
@login_required
def tracker_view(request, slug):
    """知识库追踪表：文档入库后由 AI 按自定义字段抽取关键信息登记，供追溯。

    - 配置：字段（一行一个，即列名）+ 补充提示 + 启用开关。
    - 动作：补录已有文档（backfill）/ 单行重试 / 删除行 / 导出 CSV。
    - 权限与 manage_detail 一致（全局管理员 或 该部门管理员）。
    """
    from . import access as kb_access
    from . import tracker as tracker_svc
    from .models import KbTracker, TrackerRow

    kb = get_object_or_404(KnowledgeBase, slug=slug)
    if not kb_access.can_manage_kb(request.user, kb):
        raise Http404("知识库不存在")
    tr, _ = KbTracker.objects.get_or_create(kb=kb, defaults={"fields": []})
    docs_qs = Document.objects.filter(kb_id__in=_scope_kb_ids(kb))
    _json_dumps = lambda obj: json.dumps(obj, ensure_ascii=False)  # noqa: E731

    # 回收卡死的行：daemon 线程随进程退出被杀，行会永久停在 running。
    # LLM 超时 600s，给 20 分钟余量；updated_at 在每次状态写入时都会刷新。
    if request.method == "GET":
        from datetime import timedelta
        from django.utils import timezone as _tz
        from .models import TrackerRow as _TR
        _TR.objects.filter(
            tracker=tr,  # 只回收本表（GET 不对其它库/部门的数据做写操作）
            status=_TR.Status.RUNNING,
            updated_at__lt=_tz.now() - timedelta(minutes=20),
        ).update(status=_TR.Status.FAILED, error="进程重启导致抽取中断，请重试",
                 updated_at=_tz.now())

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "enable":
            tr.enabled = True
            tr.save(update_fields=["enabled", "updated_at"])
            ok, msg = True, "追踪表已启用：先在下方配置字段，之后新文档入库即自动登记。"
        elif action == "disable":
            tr.enabled = False
            tr.save(update_fields=["enabled", "updated_at"])
            ok, msg = True, "追踪表已停用（已登记的记录保留，可随时重新启用）。"
        elif action == "config_save":
            labels = []
            for line in (request.POST.get("fields_text") or "").splitlines():
                # 标签名会进确认弹窗的 innerHTML 与 LLM 提示词：剥掉尖括号防注入
                s = line.strip().strip("：:").strip().replace("<", "").replace(">", "")
                if s and s not in labels:
                    labels.append(s[:40])
            labels = labels[:12]
            old_labels = [f["label"] for f in tr.fields]
            tr.fields = [{"label": x} for x in labels]
            tr.instruction = (request.POST.get("instruction") or "").strip()[:500]
            if labels != old_labels:
                tr.schema_version += 1  # 老行落版本 → 页面打「字段已变更」标
            tr.save()
            ok, msg = True, "追踪表配置已保存。"
        elif action == "row_confirm":
            """人工确认：按弹窗勾选把新值逐字段合并进旧值（choices: {"字段": "new"|"old"}）。"""
            import json as _json
            from django.utils import timezone as _tz
            row = tr.rows.filter(id=request.POST.get("row_id"),
                                 status=TrackerRow.Status.PROPOSED).first()
            if row is None:
                ok, msg = False, "记录不存在或已不在待确认状态。"
            else:
                try:
                    choices = _json.loads(request.POST.get("choices") or "{}")
                except ValueError:
                    choices = {}
                if not isinstance(choices, dict):
                    choices = {}
                merged = dict(row.values)
                for f in tr.fields:
                    key = f["label"]
                    if choices.get(key) == "new":
                        merged[key] = row.proposed_values.get(key, "")
                # compare-and-set：期间被重试/他人确认则放弃，不覆盖新结果
                n = TrackerRow.objects.filter(
                    id=row.id, status=TrackerRow.Status.PROPOSED).update(
                    values=merged, proposed_values={},
                    status=TrackerRow.Status.DONE,
                    schema_version=tr.schema_version, error="",
                    updated_at=_tz.now())
                if not n:
                    ok, msg = False, "记录状态已变化（可能已被重新抽取），请刷新页面。"
                else:
                    n_new = sum(1 for k in choices.values() if k == "new")
                    ok, msg = True, f"已确认：采纳 {n_new} 个新值。"
        elif action == "backfill":
            if not tr.enabled or not tr.fields:
                ok, msg = False, "请先启用追踪表并配置字段，再补录。"
            else:
                n = tracker_svc.backfill_async(tr)
                ok, msg = True, (f"已排队补录 {n} 份文档，抽取完成后自动刷新。"
                                 if n else "没有需要补录的文档（无已完成文档或均已登记）。")
        elif action == "row_retry":
            # 条件重置：抽取中（running）不允许重试——否则两个线程同时抽
            # 同一文档，终态写入互相覆盖
            from django.utils import timezone as _tz
            row_id = request.POST.get("row_id")
            n = tr.rows.filter(
                id=row_id,
                status__in=[TrackerRow.Status.FAILED, TrackerRow.Status.DONE,
                            TrackerRow.Status.PROPOSED],
            ).update(status=TrackerRow.Status.PENDING, error="",
                     updated_at=_tz.now())
            if not n:
                ok, msg = False, "该记录不存在或正在抽取中，请稍候。"
            else:
                row = tr.rows.filter(id=row_id).first()
                if row is None:
                    ok, msg = False, "记录已被删除。"
                else:
                    tracker_svc.run_extraction_async(row.document_id, tr.id)
                    ok, msg = True, "已重新排队抽取。"
        elif action == "row_delete":
            deleted, _ = tr.rows.filter(id=request.POST.get("row_id")).delete()
            ok, msg = (deleted > 0), ("记录已删除。" if deleted else "记录不存在或已被删除。")
        else:
            ok, msg = False, "未知操作。"
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": ok, "message": msg})
        (messages.success if ok else messages.error)(request, msg)
        return redirect("kb:tracker", slug=slug)

    if request.GET.get("xlsx") == "1":
        # 真 .xlsx（openpyxl）：中文无编码坑，双击即开；带表头样式与自适应列宽
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        def _safe_cell(v):
            # 公式注入防护：以 = + - @ 开头的字符串强制为文本（openpyxl 默认
            # 会把 = 开头当公式写入，=WEBSERVICE/HYPERLINK/DDE 可外泄数据）
            s2 = str(v or "")
            return "'" + s2 if s2[:1] in ("=", "+", "-", "@") else s2

        wb = Workbook()
        ws = wb.active
        ws.title = "追踪表"
        # 表头同样过 _safe_cell：字段名可以以 = + - @ 开头（sanitize 只剥尖括号）
        headers = (["文档名"] + [_safe_cell(f["label"]) for f in tr.fields]
                   + ["状态", "字段已变更", "记录时间", "错误"])
        ws.append(headers)
        head_fill = PatternFill("solid", fgColor="1F2937")
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = Font(bold=True, color="FFFFFF", size=11)
            cell.fill = head_fill
            cell.alignment = Alignment(vertical="center")
        ws.row_dimensions[1].height = 22

        smap = dict(TrackerRow.Status.choices)

        for r in tr.rows.select_related("document").order_by("-document__created_at"):
            ws.append([_safe_cell(r.document.original_name)]
                      + [_safe_cell(r.values.get(f["label"], "")) for f in tr.fields]
                      + [smap.get(r.status, r.status),
                         "是" if r.schema_version < tr.schema_version else "",
                         r.updated_at.strftime("%Y-%m-%d %H:%M"),
                         _safe_cell(r.error)])
        widths = {}
        for row in ws.iter_rows(values_only=True):
            for i, v in enumerate(row):
                w = min(max(len(str(v or "")) * 1.9, 10), 46)
                widths[i] = max(widths.get(i, 0), w)
        for i, w in widths.items():
            ws.column_dimensions[get_column_letter(i + 1)].width = w
        ws.freeze_panes = "A2"

        import io as _io
        buf = _io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(
            buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument"
                         ".spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="tracker-{slug}.xlsx"'
        return resp

    done_doc_ids = list(tr.rows.filter(status=TrackerRow.Status.DONE)
                        .values_list("document_id", flat=True))
    field_labels = [f["label"] for f in tr.fields]
    rows = []
    for r in tr.rows.select_related("document").order_by("-document__created_at"):
        cells = [r.values.get(k, "") for k in field_labels]
        proposed = [r.proposed_values.get(k, "") for k in field_labels]
        # 卡片式字段 chip：标签+值成对，跳过空值；待确认行里与登记值不同的
        # 新值高亮（与确认弹窗的 diff 语义一致）
        pairs = []
        for k, v, pv in zip(field_labels, cells, proposed):
            changed = (r.status == TrackerRow.Status.PROPOSED
                       and pv and pv != v)
            pairs.append({"label": k, "value": v or pv, "changed": changed})
        rows.append({
            "id": r.id,
            "doc_name": r.document.original_name,
            "status": r.status,
            "error": r.error,
            "updated_at": r.updated_at,
            "pairs": pairs,
            "stale": r.schema_version < tr.schema_version,
            # 确认弹窗数据（HTML 属性内联 JSON，autoescape 处理引号）
            "payload": _json_dumps({"row": r.id, "doc": r.document.original_name,
                                    "fields": field_labels,
                                    "old": cells, "new": proposed}),
        })
    return render(request, "kb/tracker.html", {
        "kb": kb,
        "tracker": tr,
        "rows": rows,
        "fields_text": "\n".join(f["label"] for f in tr.fields),
        "completed_n": docs_qs.filter(status=Document.Status.COMPLETED).count(),
        "missing_n": docs_qs.filter(status=Document.Status.COMPLETED)
                            .exclude(id__in=done_doc_ids).count(),
    })


@_is_manager
@login_required
def doc_status_api(request, slug):
    """AJAX：返回该 KB 下所有文档的最新状态（供前端轮询）+ 进度百分比。

    文件夹则跨其所有子库扁平返回（用户只看到文档，不感知子库）。
    """
    from . import access as kb_access
    # status → progress 百分比映射
    PROGRESS_MAP = {
        "pending": 0, "ocr": 25, "indexing": 75,
        "completed": 100, "failed": 100,
    }
    kb = get_object_or_404(KnowledgeBase, slug=slug)
    if not kb_access.can_manage_kb(request.user, kb):
        raise Http404("知识库不存在")
    docs_data = [
        {
            "id": str(d.id),
            "name": d.original_name,
            "file_type": d.file_type or "",
            "status": d.status,
            "status_display": d.get_status_display(),
            "stage_detail": d.stage_detail or "",
            "progress": PROGRESS_MAP.get(d.status, 0),
            "chunk_count": d.chunk_count,
            "error": d.error_msg[:100] if d.error_msg else "",
            "desc": d.description or "",
        }
        for d in _scope_docs(kb)
    ]
    kb.refresh_from_db()
    docs_total, chunks_total = kb.aggregate_counts()
    return JsonResponse({
        "docs": docs_data,
        "doc_count": docs_total,
        "chunk_count": chunks_total,
    })


@login_required
def doc_image(request, doc_id, name):
    """文档图片（MinerU 提取的插图）。

    刻意不走 media/ 静态服务：这里做与查看页一致的部门级访问控制，
    防止跨部门文档图片被直接 URL 枚举。文件名是内容哈希，可长缓存。
    """
    import re as _re
    from django.http import FileResponse
    from . import access as kb_access
    from .pipeline import doc_image_dir

    if not _re.fullmatch(r"[0-9a-f]{32}(?:[0-9a-f]{32})?\.(jpg|jpeg|png|webp)", name, _re.IGNORECASE):
        raise Http404("图片不存在")
    doc = get_object_or_404(Document, id=doc_id)
    if not kb_access.kb_accessible(request.user, doc.kb):
        raise Http404("图片不存在")
    p = doc_image_dir(doc_id) / name
    if not p.is_file():
        raise Http404("图片不存在")
    mime = {
        ".png": "image/png", ".webp": "image/webp",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }.get(p.suffix.lower(), "application/octet-stream")
    resp = FileResponse(p.open("rb"), content_type=mime)
    resp["Cache-Control"] = "private, no-store"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@login_required
def document_html(request, doc_id):
    """文档 HTML 查看页：渲染文档正文，支持 ?h= 高亮指定文本片段。

    - 若 html_content 为空但 md_content 有值 → 懒构建（安全网）。
    - ?h=<文本片段>：前端据此高亮（来源出处点击后携带）。
    - 部门过滤：所属库不可访问 → 404（不泄露存在性）。
    """
    from . import access as kb_access
    doc = get_object_or_404(Document, id=doc_id)
    if not kb_access.kb_accessible(request.user, doc.kb):
        raise Http404("文档不存在")
    if not doc.html_content and doc.md_content:
        from .pipeline import build_doc_html
        build_doc_html(doc)
    doc.refresh_from_db()
    # 支持多个 ?h= 参数：每个是待高亮的文本片段（来源出处点击后携带）。
    # 限量防滥用（≤12 个 × ≤160 字符，与前端 slice(0,12) 对齐）；
    # 模板用 |json_script 渲染（XSS 安全，勿改回 |safe + json.dumps）。
    highlights = [h.strip()[:160] for h in request.GET.getlist("h") if h.strip()][:12]
    from .pipeline import rewrite_img_srcs
    return render(request, "kb/document_html.html", {
        "doc": doc,
        "html_body": rewrite_img_srcs(doc.html_content or "", doc.id),
        "highlights": highlights,
    })


# ------------------------------------------------------------------
# 证据面板（chunk 级定位：渲染原 PDF 页 + bbox 红圈圈选）
# ------------------------------------------------------------------
def _evidence_prov(request, chunk_id):
    """取 chunk 溯源行并做部门访问控制（不可见 = 404，不泄露存在性）。"""
    from . import access as kb_access
    from .models import ChunkProvenance

    prov = (ChunkProvenance.objects.select_related("document", "document__kb")
            .filter(chunk_id=chunk_id).first())
    if prov is None or not kb_access.kb_accessible(request.user, prov.document.kb):
        raise Http404("证据不存在")
    return prov


@login_required
def evidence_preview(request, chunk_id):
    """证据预览（JSON）：页码区间 + 归一化 bbox 块列表 + chunk 原文。

    无溯源行（照片直传 / 溯源功能前入库的文档）→ 200 + ok:false，
    前端提示「暂无定位数据」而非报 404 错误。
    """
    from . import access as kb_access
    from .models import ChunkProvenance
    from .provenance import get_chunk_text

    prov = (ChunkProvenance.objects.select_related("document", "document__kb")
            .filter(chunk_id=chunk_id).first())
    if prov is None:
        return JsonResponse({"ok": False, "reason": "no_provenance",
                             "chunk_id": chunk_id})
    doc = prov.document
    if not kb_access.kb_accessible(request.user, doc.kb):
        raise Http404("证据不存在")
    has_pdf = False
    if doc.file_type == "pdf" and doc.file and doc.file.name:
        try:
            has_pdf = Path(doc.file.path).is_file()
        except (NotImplementedError, ValueError):
            has_pdf = False
    return JsonResponse({
        "ok": True,
        "chunk_id": prov.chunk_id,
        "doc_id": str(doc.id),
        "source": doc.original_name,
        "pages": [prov.page_start, prov.page_end],
        "page_label": prov.page_label(),
        "blocks": prov.blocks,
        "source_pages": sorted({b["page"] for b in prov.blocks
                                if isinstance(b, dict) and type(b.get("page")) is int}),
        "location_precision": "region",
        "has_pdf": has_pdf,
        "quote": get_chunk_text(prov.kb_slug, prov.chunk_id)[:800],
    })


@login_required
def evidence_page_png(request, chunk_id):
    """渲染 chunk 所在的原 PDF 页为 PNG（PyMuPDF 2x）。

    ?page=N 指定页（0 基，跨页 chunk 翻页用），默认 chunk 起始页。
    """
    prov = _evidence_prov(request, chunk_id)
    doc = prov.document
    if doc.file_type != "pdf" or not doc.file or not doc.file.name:
        raise Http404("原文不是 PDF 或文件已删除")
    try:
        pdf_path = Path(doc.file.path)
    except (NotImplementedError, ValueError):
        raise Http404("原文不可用")  # noqa: B904
    if not pdf_path.is_file():
        raise Http404("原文文件已删除")
    try:
        try:
            import pymupdf  # PyMuPDF ≥1.28 的推荐导入名
        except ImportError:
            import fitz as pymupdf  # 兼容旧版本导入名
    except ImportError:
        return HttpResponse("服务端未安装 PyMuPDF，无法渲染原页", status=503)

    try:
        page_no = int(request.GET.get("page", prov.page_start))
    except (TypeError, ValueError):
        return HttpResponse("无效页码", status=400)
    source_pages = {b.get("page") for b in prov.blocks if isinstance(b, dict)}
    if not source_pages:
        source_pages = set(range(prov.page_start, prov.page_end + 1))
    if page_no < 0 or page_no not in source_pages:
        raise Http404("该页不属于此证据")
    try:
        with pymupdf.open(pdf_path) as pdf:
            if page_no >= pdf.page_count:
                raise Http404("证据页码与原文不一致，请重新建立定位")
            page = pdf[page_no]
            scale = min(2.0, 2400 / max(page.rect.width, page.rect.height, 1))
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            data = pix.tobytes("png")
    except Http404:
        raise
    except Exception:
        return HttpResponse("原页渲染失败，请检查原文或重新建立定位", status=500)
    resp = HttpResponse(data, content_type="image/png")
    resp["Cache-Control"] = "private, no-store"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@login_required
def document_slices(request, doc_id):
    """引用切片查看页：左侧命中切片列表，右侧切片内容（带高亮）。

    引用点击后先看「检索命中了哪些切片」；一键跳整篇文档（document_html，
    高亮参数原样透传）。切片来自关键词索引（与向量库同源写入，且天然是
    「嵌入用纯文本」）；索引缺失或无命中时回退整篇页。
    """
    import html as html_mod
    import re as re_mod
    from urllib.parse import urlencode

    from django.urls import reverse

    from . import access as kb_access
    from . import keyword_index

    doc = get_object_or_404(Document, id=doc_id)
    if not kb_access.kb_accessible(request.user, doc.kb):
        raise Http404("文档不存在")

    highlights = [h.strip()[:160] for h in request.GET.getlist("h") if len(h.strip()) >= 3][:12]
    full_doc_url = reverse("kb:document_html", args=[doc.id])
    if highlights:
        full_doc_url += "?" + urlencode({"h": highlights}, doseq=True)

    def _fallback():
        return redirect(full_doc_url)

    chunks = keyword_index.get_chunks(doc.kb.slug, doc.original_name)
    if not chunks or not highlights:
        return _fallback()

    # 命中判定：短语（小写比较）出现在切片正文即算命中；命中数多者优先，
    # 同数按文档顺序。≤30 片防止短语过短（如「饮片」）时列表爆炸。
    lows = [h.lower() for h in highlights]
    scored: list[tuple[int, int, dict, list[str]]] = []
    for idx, ch in enumerate(chunks):
        text_l = ch["text"].lower()
        hits = [h for h, lh in zip(highlights, lows) if lh in text_l]
        if hits:
            scored.append((len(hits), idx, ch, hits))
    if not scored:
        return _fallback()
    scored.sort(key=lambda x: (-x[0], x[1]))

    # 单趟标记：所有短语合成一个交替正则（长短语优先，避免短词先吃掉长词），
    # 在「已转义」的文本上一次性替换，杜绝 <mark> 嵌套。
    def _marked(esc_text: str, phrases: list[str]) -> str:
        alts = [re_mod.escape(html_mod.escape(p)) for p in sorted(set(phrases), key=len, reverse=True)]
        pat = re_mod.compile("|".join(alts), re_mod.IGNORECASE)
        return pat.sub(lambda m: '<mark class="cite-hit">' + m.group(0) + "</mark>", esc_text)

    slices = []
    for rank, (_n, _idx, ch, hits) in enumerate(scored[:30], start=1):
        esc = html_mod.escape(ch["text"])
        marked = _marked(esc, hits)
        # 列表摘要：首个命中前后各 ~50 字（含标记），比从头截取更利于挑选
        m = re_mod.search(r"<mark", marked)
        if m:
            start = max(0, m.start() - 50)
            snip = marked[start:start + 160]
            if start > 0:
                snip = "…" + snip
        else:
            snip = marked[:160]
        slices.append({
            "no": rank, "section": ch["section"] or "正文",
            "marked": marked, "snippet": snip, "hit_count": len(hits),
        })
    return render(request, "kb/slices.html", {
        "doc": doc,
        "slices": slices,
        "phrase_count": len(highlights),
        "full_doc_url": full_doc_url,
    })


# ------------------------------------------------------------------
# 问答页（所有登录用户）
# ------------------------------------------------------------------
@login_required
def ask(request):
    """问答主页：提问 + 历史会话列表。

    选库由 agent 自主完成（list_knowledge_bases + kb_search），无需前端手动选择。
    侧边栏会话按部门过滤（KB 已不可访问的旧会话不再显示）。
    """
    from . import access as kb_access
    # 注意：Conversation 上过滤要走 kb__department（kb_q 的裸 department 只适用于 KB 查询集）
    conversations = (
        Conversation.objects.filter(user=request.user)
        .filter(kb__department__in=[kb_access.DEPARTMENT_GENERAL, kb_access.user_department(request.user)])
        .select_related("kb").only("id", "title", "thread_id", "kb__name", "updated_at", "created_at")
    )
    return render(request, "kb/ask.html", {
        "pyodide_index_url": settings.PYODIDE_INDEX_URL,
        "conversations": conversations,
        "active_thread": request.GET.get("conv", ""),
    })


def _route_kb_by_question(message: str, user):
    """问题文本 → 名称最相关的文档库（词面路由，交给 agent 前的确定性预定位）。

    问题里明确出现某个库名或文档名（如「P8」「Dumbo」「药典」）时直接定位到该库，
    避免 agent 在明显无关的库上浪费检索。保守策略：只有唯一强命中才返回，
    歧义（多家同分）或无命中返回 None，由调用方走原有自动挑选。
    """
    from . import access as kb_access

    text = (message or "").lower()
    if not text.strip():
        return None
    scores: list[tuple[int, object]] = []  # (score, kb)
    for kb in KnowledgeBase.objects.filter(kb_access.kb_q(user), is_folder=False):
        names = [kb.name] + [d.original_name for d in kb.documents.all()]
        score = 0
        for name in names:
            n = (name or "").lower().strip()
            if not n:
                continue
            if n in text:  # 完整名称出现 → 强信号
                score = max(score, len(n))
                continue
            stem = re.sub(r"\.[a-z0-9]+$", "", n)
            for tok in re.split(r"[^0-9a-z\u4e00-\u9fff]+", stem):
                if not tok:
                    continue
                if tok.isascii():
                    # 2 字符纯字母 token（dr/en/mm）噪声大（子串误命中）→ 必须含数字（p8/g9）
                    if len(tok) < 2 or (len(tok) == 2 and not any(c.isdigit() for c in tok)):
                        continue
                    if len(tok) >= 6:
                        # 长编号/代号支持前缀提及（sdlspdrtp → sdlspdrtp0003）
                        for cut in range(len(tok), 5, -1):
                            if tok[:cut] in text:
                                score = max(score, min(cut, 8))
                                break
                    elif tok in text:
                        score = max(score, min(len(tok), 8))
                elif len(tok) >= 2 and tok in text:  # CJK 连续段整体出现
                    score = max(score, min(len(tok), 8))
        scores.append((score, kb))
    if not scores:
        return None
    scores.sort(key=lambda t: t[0], reverse=True)
    top1, top2 = scores[0][0], (scores[1][0] if len(scores) > 1 else 0)
    # 唯一强命中（≥2 字符且严格领先）才定位；平分视为歧义，不干预
    return scores[0][1] if top1 >= 2 and top1 > top2 else None


@login_required
@require_http_methods(["POST"])
async def chat_stream(request):
    """SSE 流式问答端点。

    参数: message, thread_id, kb_slug
    仅接受带 CSRF 校验的 POST——此前 csrf_exempt + 允许 GET 时，外站可用
    <img src=...?message=...> 伪造受害者名下的消息并消耗本地 GPU。
    """
    from .agent import run_agent_stream
    from .config import llm_settings, retrieval_settings

    message = (request.POST.get("message") or "").strip()
    thread_id = request.POST.get("thread_id") or ""
    kb_slug = request.POST.get("kb_slug") or ""

    if not message:
        return HttpResponse("missing 'message'", status=400)
    if not thread_id:
        return HttpResponse("missing 'thread_id'", status=400)

    # 在同步上下文里一次性解析配置 + 取/建会话（避免在 async 生成器中访问数据库）
    def _prepare():
        from . import access as kb_access

        user_dept = kb_access.user_department(request.user)
        # kb_slug 可选：未指定时优先选「有向量块的文档库」（文件夹自身无向量），
        # 再退到任意文件夹（可扇出搜索），最后退到任意库——均在用户可见范围内。
        # agent 仍可通过 list_knowledge_bases + kb_search(kb_slug=...) 自主跨库（同受部门过滤）。
        kb = None  # ask 页改版后前端不再传 kb_slug，空值是主路径，必须先初始化
        if kb_slug:
            kb = KnowledgeBase.objects.filter(slug=kb_slug).first()
            if kb and not kb_access.kb_accessible(request.user, kb):
                kb = None  # 指定了不可访问的库 → 视为未指定，走自动挑选
        if not kb:
            # 词面路由：问题明确提到某库/文档名 → 直接定位，避免无关库检索
            kb = _route_kb_by_question(message, request.user)
        if not kb:
            kb = (
                KnowledgeBase.objects.filter(kb_access.kb_q(request.user), is_folder=False, chunk_count__gt=0).first()
                or KnowledgeBase.objects.filter(kb_access.kb_q(request.user), is_folder=False).first()
                or KnowledgeBase.objects.filter(kb_access.kb_q(request.user), is_folder=True).first()
                or KnowledgeBase.objects.filter(kb_access.kb_q(request.user)).first()
            )
        if not kb:
            return None, None, False, "no knowledge base available"
        # 首条消息 → 创建会话；标题取消息前 30 字
        conv, created = Conversation.objects.get_or_create(
            thread_id=thread_id,
            defaults={
                "user": request.user,
                "kb": kb,
                "title": message[:30],
            },
        )
        # 安全：会话必须属于当前用户
        if conv.user_id != request.user.id:
            return None, None, False, "forbidden"
        # 续聊旧会话：若其 KB 因部门调整已不可访问，回退到自动挑选
        if not kb_access.kb_accessible(request.user, conv.kb):
            conv.kb = kb
            conv.save(update_fields=["kb", "updated_at"])
        # Enhanced turns rebuild memory from published messages, never failed checkpoints.
        from .publication import visible_citations
        current_llm = llm_settings()
        # Keep recent completed turns only; never replay accumulated tool outputs
        # or resume a cancelled LangGraph checkpoint on the next question.
        from .history import recent_history
        published_history = recent_history(
            list(conv.messages.order_by("-created_at")[:24])[::-1],
            enhanced=current_llm.get("qa_enhance", False),
            visible=lambda citations: visible_citations(citations, request.user),
        )
        # 记录用户消息
        Message.objects.create(conversation=conv, role=Message.Role.USER, content=message)
        conv.save(update_fields=["updated_at"])  # 刷新排序
        cfg = {
            "llm": current_llm,
            "published_history": published_history,
            "user_id": request.user.id,
            "top_k": retrieval_settings()["top_k"],
            "department": user_dept,
            # _prepare 已解析出的可访问默认库（async 流里不能查库，故在此带回）
            "effective_kb_slug": kb.slug,
        }
        return conv, cfg, created, None

    prepared = await sync_to_async(_prepare)()
    conv, agent_config, _created, prep_err = prepared
    if prep_err == "no knowledge base available":
        return HttpResponse("暂无知识库，请先上传文档。", status=400)
    if conv is None:
        return HttpResponse("forbidden", status=403)

    full_thread = conv.thread_id  # 会话的稳定 thread_id（直接用作 checkpointer key）

    async def event_stream():
        from .streaming import bounded_events
        ai_chunks = []
        turn_citations = []
        turn_verify = None
        failed = False
        effective_kb_slug = agent_config.get("effective_kb_slug") or conv.kb.slug
        try:
            from contextlib import aclosing
            async with aclosing(bounded_events(run_agent_stream(
                message, full_thread, effective_kb_slug, agent_config,
            ), timeout=settings.QA_TURN_TIMEOUT)) as events:
                async for event_type, payload in events:
                    if event_type == "error":
                        failed = True
                    elif event_type == "token":
                        ai_chunks.append(payload.get("text", ""))
                    elif event_type == "citations":
                        turn_citations.extend(payload.get("citations") or [])
                    elif event_type == "verify":
                        turn_verify = payload
                    yield f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            ai_text = "".join(ai_chunks).strip() if not failed else ""
            if agent_config["llm"].get("qa_enhance") and not (turn_verify and turn_verify.get("ok")):
                ai_text = ""
            if ai_text:
                # Persist before done: the browser closes its reader upon done.
                await sync_to_async(Message.objects.create)(
                    conversation=conv, role=Message.Role.AI,
                    content=ai_text, citations=turn_citations,
                    verified=bool(agent_config["llm"].get("qa_enhance") and turn_verify and turn_verify.get("ok")),
                )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Answer stream failed thread=%s", full_thread)
            yield 'event: error\ndata: {"message":"问答服务异常，回答未完成，请重试。"}\n\n'
        yield "event: done\ndata: {}\n\n"

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
def conversation_messages(request, thread_id):
    """返回某会话的历史消息（JSON），供前端打开会话时渲染。

    部门过滤：会话所属 KB 已不可访问时返回 404（与侧边栏不可见一致）。
    """
    from . import access as kb_access
    conv = get_object_or_404(Conversation, thread_id=thread_id, user=request.user)
    if not kb_access.kb_accessible(request.user, conv.kb):
        raise Http404("会话不存在")
    msgs = list(conv.messages.order_by("created_at").values("role", "content", "citations"))
    from .publication import visible_citations
    for msg in msgs:
        if msg["role"] == Message.Role.AI and msg["citations"] and not visible_citations(msg["citations"], request.user):
            msg["content"] = "此回答的来源已不可访问或已变化，请重新检索。"
            msg["citations"] = []
    return JsonResponse({"thread_id": conv.thread_id, "title": conv.title,
                         "kb_slug": conv.kb.slug, "messages": msgs})


@login_required
@require_http_methods(["POST"])
def conversation_delete(request, thread_id):
    """删除某会话（仅元数据 + 消息；checkpointer 数据保留无妨）。

    幂等：会话已不存在时返回 JSON 成功，前端无需报错。
    """
    conv = Conversation.objects.filter(thread_id=thread_id, user=request.user).first()
    if conv is None:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": True, "already_deleted": True,
                                 "message": "会话不存在或已被删除。"})
        messages.info(request, "该会话不存在或已被删除。")
        return redirect("kb:ask")
    conv.delete()
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "thread_id": thread_id})
    messages.success(request, "会话已删除。")
    return redirect("kb:ask")


# ------------------------------------------------------------------
# 搜索（模糊搜索：图纸号 / 部件名称 → 表格化展示）
# ------------------------------------------------------------------
def _semantic_matches(user, query: str, k: int = 8) -> list[dict]:
    """综合搜索的语义命中：与问答页同一条混合检索链路
    （WeMM 向量 + 关键词双路 → RRF 融合 → rerank 精排），跨库用 search_folder 扇出。

    部门过滤与手册路一致（通用 ∪ 本部门）；只检索已建向量的库（chunk_count>0，
    也让测试库天然跳过）；任何失败降级为空列表，不影响页面其它区块。
    """
    import logging as _logging

    from . import access as kb_access
    from .models import DEPARTMENT_GENERAL
    from .retriever import search_folder

    user_dept = kb_access.user_department(user)
    slugs = list(
        KnowledgeBase.objects.filter(
            is_folder=False, chunk_count__gt=0,
            department__in=[DEPARTMENT_GENERAL, user_dept],
        ).values_list("slug", flat=True)
    )
    if not slugs:
        return []
    try:
        results = search_folder(slugs, query, k=k, include_images=True)
    except Exception as e:
        _logging.getLogger(__name__).warning(
            "综合搜索语义检索失败（降级为空）q=%s: %s", query, str(e)[:120])
        return []
    # 溯源标注：命中片段带页码/行号（老文档无溯源则静默跳过）
    try:
        from . import provenance as _prov
        _prov.annotate_results(results)
    except Exception:
        pass
    out = []
    for r in results:
        if not r.get("doc_id"):
            continue
        text = (r.get("text") or "").strip()
        anchor = re.sub(r"^【[^】]*】\s*", "", text)[:16]
        # chunk_id 只在有溯源（页码存在）时下发——无溯源的命中（照片直传/
        # 老入库文档）渲染 📍 按钮会点了 404，退化为普通链接
        has_prov = bool(r.get("page_label"))
        # 分数：文本块用 rerank 分；图片块无 rerank，按向量距离换算图文相似度。
        # WeMM 图文存在模态鸿沟（余弦距离常在 1.2~1.7），把相似度区间
        # [-1,1] 线性归一到 [0,1]（= 1-d/2）：保序、非零有区分度，跨批可比
        score = r.get("rerank_score")
        if score is None and r.get("type") == "image" and r.get("distance") is not None:
            score = round(max(0.0, min(1.0, 1.0 - float(r["distance"]) / 2.0)), 4)
        out.append({
            "doc_id": r["doc_id"],
            "source": r.get("source") or "未知来源",
            "section": r.get("section") or "",
            "text": text,
            "anchor": anchor,
            "via": r.get("via", ""),
            "score": score,
            "is_image": r.get("type") == "image",
            # 整页视觉块：缩略图 = 证据接口按需渲染原 PDF 页；插图块 = 落盘图
            "images": ([f"/kb/evidence/{r['chunk_id']}/page.png"]
                       if r.get("page_block") and r.get("chunk_id")
                       else [f"/kb/doc/{r['doc_id']}/img/{r['image']}"]
                       if r.get("image") else []),
            "page": r.get("page_label") or "",
            "chunk_id": r.get("chunk_id") if has_prov else "",
        })
    return out


@login_required
def search_view(request):
    """综合搜索：结构化跨表关联 + 手册全文搜索 + 语义混合检索（问答同款链路）。

    业务键/手册正文为确定性匹配；语义命中走向量+关键词+rerank（可搜到自然语言
    问题和图片块）。手册与语义均按用户部门过滤（通用 ∪ 本部门）。
    """
    q = (request.GET.get("q") or "").strip()
    from . import access as kb_access
    from .models import StructuredDataset
    from .structured_data import lookup_related

    user_dept = kb_access.user_department(request.user)
    lookup = lookup_related(q, department=user_dept) if q else None
    # 手册原文命中（行级字面匹配）对齐 chunk 溯源：chip 直接开证据面板定位
    if lookup and lookup.get("manual_matches"):
        try:
            from .provenance import locate_snippets
            locate_snippets(lookup["manual_matches"])
        except Exception:
            import logging as _lg
            _lg.getLogger(__name__).warning(
                "手册原文溯源对齐失败（降级为普通链接）", exc_info=True)
    # 语义命中拆两栏展示：文本块（含 rerank 相关度）与图片块（多模态召回道）
    sem_all = _semantic_matches(request.user, q) if q else []
    semantic_matches = [m for m in sem_all if not m.get("is_image")]
    image_matches = [m for m in sem_all if m.get("is_image")]
    datasets = (
        StructuredDataset.objects.filter(active=True)
        .select_related("imported_by").order_by("kind", "-created_at")
    )
    return render(request, "kb/asset_lookup.html", {
        "q": q,
        "lookup": lookup,
        "semantic_matches": semantic_matches,
        "image_matches": image_matches,
        "datasets": datasets,
    })


# ------------------------------------------------------------------
# 图纸号结构化关联（CSV/XLSX；不调用大模型）
# ------------------------------------------------------------------
@login_required
def asset_lookup(request):
    """旧“图纸关联”地址：保留导入兼容，GET 统一跳转到综合搜索。"""
    from .structured_data import (
        FIELD_LABELS, StructuredDataError, import_structured_dataset,
    )

    if request.method == "POST":
        if not request.user.is_staff:
            return HttpResponse("只有管理员可以导入结构化数据。", status=403)
        upload = request.FILES.get("file")
        kind = (request.POST.get("kind") or "").strip()
        if not upload:
            messages.error(request, "请选择 CSV 或 XLSX 文件。")
        else:
            try:
                dataset = import_structured_dataset(upload, kind, request.user)
                mapped = "、".join(
                    label for key, label in FIELD_LABELS.items() if key in dataset.mapping
                ) or "未识别到关联键（仍已保留原始列）"
                messages.success(
                    request,
                    f"已导入 {dataset.source_name}：{dataset.row_count} 行；识别字段：{mapped}。",
                )
            except StructuredDataError as exc:
                messages.error(request, str(exc))
        return redirect("kb:search")

    from urllib.parse import urlencode
    from django.urls import reverse
    q = (request.GET.get("q") or "").strip()
    target = reverse("kb:search")
    if q:
        target += "?" + urlencode({"q": q})
    return redirect(target)


# ------------------------------------------------------------------
# 检查项提取（自动筛选文档中的检查内容）
# ------------------------------------------------------------------
@login_required
def inspection_list(request):
    """检查项总览页：列出所有已完成文档，可选择文档查看提取的检查项。

    GET → 渲染文档列表（按用户部门过滤：通用 ∪ 本部门）。
    GET ?doc=<uuid> → AJAX 返回该文档提取的检查项 JSON（同受部门过滤）。
    """
    from . import access as kb_access
    from .models import Document

    doc_id = request.GET.get("doc", "").strip()
    is_ajax = (request.headers.get("x-requested-with") == "XMLHttpRequest"
               or "application/json" in (request.META.get("HTTP_ACCEPT") or ""))
    docs_qs = kb_access.accessible_docs(request)

    # 指定文档 → 提取检查项并返回
    if doc_id:
        doc = get_object_or_404(docs_qs, id=doc_id)
        from .inspection import extract_inspection_items
        items = extract_inspection_items(doc.md_content or "", doc.original_name)
        if is_ajax:
            return JsonResponse({
                "doc_id": str(doc.id),
                "doc_name": doc.original_name,
                "kb_name": doc.kb.name if doc.kb else "",
                "items": items,
                "total": len(items),
            })
        # 非 AJAX：渲染带数据的页面
        return render(request, "kb/inspection.html", {
            "doc": doc,
            "items": items,
            "total": len(items),
            "docs": docs_qs.only("id", "original_name", "kb__name"),
        })

    # 无指定文档：渲染文档选择页
    docs = (
        docs_qs
        .only("id", "original_name", "kb__name", "chunk_count")
        .order_by("-updated_at")
    )
    return render(request, "kb/inspection.html", {
        "docs": docs,
        "doc": None,
        "items": [],
        "total": 0,
    })


# ------------------------------------------------------------------
# 原骨架 index（保留，重定向到 ask）
# ------------------------------------------------------------------
@login_required
def index(request):
    return redirect("kb:ask")


# ------------------------------------------------------------------
# 站点配置（前端可编辑；仅 staff）
# ------------------------------------------------------------------
# 字段定义：(表单字段名, 模型字段名, 类型, .env 默认占位)
_CONFIG_FIELDS = [
    ("llm_base_url", "llm_base_url", "text", "LLM_BASE_URL"),
    ("llm_api_key", "llm_api_key", "password", "LLM_API_KEY"),
    ("llm_model", "llm_model", "text", "LLM_MODEL"),
    ("llm_temperature", "llm_temperature", "float", "LLM_TEMPERATURE"),
    ("llm_vision", "llm_vision", "bool", "LLM_VISION"),
    ("qa_enhance", "qa_enhance", "bool", "QA_ENHANCE"),
    ("embedding_base_url", "embedding_base_url", "text", "EMBEDDING_BASE_URL"),
    ("embedding_api_key", "embedding_api_key", "password", "EMBEDDING_API_KEY"),
    ("embedding_model", "embedding_model", "text", "EMBEDDING_MODEL"),
    ("embedding_dimensions", "embedding_dimensions", "int", "EMBEDDING_DIMENSIONS"),
    ("kb_chunk_size", "kb_chunk_size", "int", "KB_CHUNK_SIZE"),
    ("kb_chunk_overlap", "kb_chunk_overlap", "int", "KB_CHUNK_OVERLAP"),
    ("kb_top_k", "kb_top_k", "int", "KB_TOP_K"),
    ("mineru_api_base", "mineru_api_base", "text", "MINERU_API_BASE"),
    ("mineru_api_key", "mineru_api_key", "password", "MINERU_API_KEY"),
    ("mineru_backend", "mineru_backend", "text", "MINERU_BACKEND"),
    ("mineru_lang", "mineru_lang", "text", "MINERU_LANG"),
    ("rerank_enabled", "rerank_enabled", "bool", ""),
    ("rerank_base_url", "rerank_base_url", "text", "RERANK_BASE_URL"),
    ("rerank_api_key", "rerank_api_key", "password", "RERANK_API_KEY"),
    ("rerank_model", "rerank_model", "text", "RERANK_MODEL"),
]


@_is_staff
def eval_panel(request):
    """检索质量评估面板（staff）：维护回归问题集，一键跑混合检索看命中。"""
    from .eval import run_eval
    from .models import EvalQuestion

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "add":
            question = (request.POST.get("question") or "").strip()
            if question:
                EvalQuestion.objects.create(
                    question=question,
                    expected_source=(request.POST.get("expected_source") or "").strip(),
                    expected_keyword=(request.POST.get("expected_keyword") or "").strip(),
                )
                messages.success(request, "评估问题已添加。")
            else:
                messages.error(request, "问题不能为空。")
        elif action == "delete":
            try:
                EvalQuestion.objects.filter(pk=request.POST.get("qid")).delete()
            except Exception:
                messages.error(request, "删除失败：无效的问题 ID。")
        return redirect("kb:eval_panel")

    report = None
    if request.GET.get("run") == "1":
        report = run_eval()
        # session 瘦身：只存 summary + 每题命中名次 + 前 3 行明细
        # （全量报告 Q×库×top5 行文本可达 MB 级，会把 django_session 撑爆）
        slim = {
            "summary": report["summary"],
            "questions": [
                {"question": q["question"], "expected": q["expected"],
                 "rank_hit": q["rank_hit"], "rows": q["rows"][:3]}
                for q in report["questions"]
            ],
        }
        request.session["eval_report"] = slim
    else:
        report = request.session.get("eval_report")

    return render(request, "kb/eval.html", {
        "questions": EvalQuestion.objects.all().only(
            "question", "expected_source", "expected_keyword", "created_at"),
        "report": report,
    })


@_is_staff
def site_settings(request):
    """站点配置页：GET 渲染，POST 保存配置 / 保存为预设 / 加载预设 / 删除预设。"""
    from .config import get_config
    from .models import ConfigPreset, PRESET_CATEGORIES
    from django.conf import settings as dj_settings

    cfg = get_config()

    # AJAX 判断：fetch 请求带 X-Requested-With 或 Accept: application/json
    is_ajax = (request.headers.get("x-requested-with") == "XMLHttpRequest"
               or "application/json" in (request.META.get("HTTP_ACCEPT") or ""))

    def _json_response(ok, message, eff=None, extra=None):
        body = {"ok": ok, "message": message}
        if eff is not None:
            body["eff"] = eff
        if extra:
            body.update(extra)
        return JsonResponse(body)

    def _current_eff():
        """重新读取当前生效配置（保存/加载后）。"""
        from . import config as cfg_mod
        return {
            "llm": cfg_mod.llm_settings(),
            "embedding": cfg_mod.embedding_settings(),
            "retrieval": cfg_mod.retrieval_settings(),
            "mineru": cfg_mod.mineru_settings(),
            "rerank": cfg_mod.rerank_settings(),
        }

    if request.method == "POST":
        action = request.POST.get("action", "save")

        # ---- 保存为预设（按分类）：只存该分类的字段 ----
        if action == "preset_save":
            name = (request.POST.get("preset_name") or "").strip()
            category = request.POST.get("category", "")
            if not name:
                msg = "请填写配置名称。"
                if is_ajax: return _json_response(False, msg)
                messages.error(request, msg); return redirect("kb:settings")
            if category not in PRESET_CATEGORIES:
                msg = "分类无效。"
                if is_ajax: return _json_response(False, msg)
                messages.error(request, msg); return redirect("kb:settings")
            fields = PRESET_CATEGORIES[category]
            _apply_form_to_cfg(cfg, request.POST, category)
            cfg.save()
            preset, created = ConfigPreset.objects.update_or_create(
                name=name, category=category,
                defaults={"data": cfg.snapshot(fields)},
            )
            # 当前生效配置即该预设 → 标记为激活
            setattr(cfg, f"active_preset_{category}", name)
            cfg.save(update_fields=[f"active_preset_{category}"])
            msg = f"{dict(ConfigPreset.Category.choices)[category]} 配置「{name}」已{'保存' if created else '更新'}并启用。"
            if is_ajax:
                return _json_response(True, msg, eff=_current_eff(),
                                      extra={"preset_id": preset.id, "preset_name": name,
                                             "category": category, "updated_at": "刚刚"})
            messages.success(request, msg); return redirect("kb:settings")

        # ---- 加载预设：只覆盖该分类字段 ----
        if action == "preset_load":
            pid = request.POST.get("preset_id")
            preset = ConfigPreset.objects.filter(id=pid).first()
            if not preset:
                msg = "配置不存在。"
                if is_ajax: return _json_response(False, msg)
                messages.error(request, msg); return redirect("kb:settings")
            fields = PRESET_CATEGORIES.get(preset.category, [])
            cfg.apply(preset.data, fields)
            setattr(cfg, f"active_preset_{preset.category}", preset.name)
            cfg.save()
            msg = f"已切换到配置「{preset.name}」（下一次请求即生效）。"
            if is_ajax: return _json_response(True, msg, eff=_current_eff())
            messages.success(request, msg); return redirect("kb:settings")

        # ---- 删除预设 ----
        if action == "preset_delete":
            pid = request.POST.get("preset_id")
            preset = ConfigPreset.objects.filter(id=pid).first()
            msg = "配置不存在。"
            if preset:
                name = preset.name
                active_field = f"active_preset_{preset.category}"
                preset.delete()
                if getattr(cfg, active_field, "") == name:
                    setattr(cfg, active_field, "")
                    cfg.save(update_fields=[active_field, "updated_at"])
                msg = f"配置「{name}」已删除。"
            if is_ajax: return _json_response(True, msg, extra={"preset_id": pid})
            messages.success(request, msg); return redirect("kb:settings")

        # ---- 默认：保存当前配置（按弹窗提交的分类，只动该分类字段；
        #      未提交的其它分类保持不变——弹窗化后不再整表提交） ----
        try:
            category = request.POST.get("category", "")
            before = {cat: cfg.snapshot(fields) for cat, fields in PRESET_CATEGORIES.items()}
            _apply_form_to_cfg(cfg, request.POST, category)
            for cat, fields in PRESET_CATEGORIES.items():
                if cfg.snapshot(fields) != before[cat]:
                    setattr(cfg, f"active_preset_{cat}", "")
            cfg.save()
            msg = "配置已保存（下一次请求即生效）。"
            if is_ajax: return _json_response(True, msg, eff=_current_eff())
            messages.success(request, msg)
        except (ValueError, TypeError) as e:
            msg = f"保存失败：{e}"
            if is_ajax: return _json_response(False, msg)
            messages.error(request, msg)
        return redirect("kb:settings")

    # GET：渲染有效值 + .env 默认占位 + 按分类分组的预设
    import json as _json
    from . import config as cfg_mod
    eff = {
        "llm": cfg_mod.llm_settings(),
        "embedding": cfg_mod.embedding_settings(),
        "retrieval": cfg_mod.retrieval_settings(),
        "mineru": cfg_mod.mineru_settings(),
        "rerank": cfg_mod.rerank_settings(),
    }
    placeholders = {env: getattr(dj_settings, env, "") for _f, _m, _t, env in _CONFIG_FIELDS}
    # 按分类分组预设，传给模板
    from collections import defaultdict
    presets_by_cat = defaultdict(list)
    for p in ConfigPreset.objects.all():
        presets_by_cat[p.category].append(p)
    # 每分类当前激活配置的 id（下拉切换 + 删除当前配置按钮用）
    active_pid = {}
    for _cat in ("llm", "embedding", "retrieval", "mineru", "rerank"):
        _name = getattr(cfg, f"active_preset_{_cat}")
        _p = (ConfigPreset.objects.filter(category=_cat, name=_name)
              .values_list("id", flat=True).first()) if _name else None
        active_pid[_cat] = str(_p) if _p else ""
    return render(request, "kb/settings.html", {
        "eff": eff,
        # 弹窗实时数据：生效值 + 每分类当前激活的配置名（编辑弹窗预填用）。
        # 模板用 |json_script 渲染（XSS 安全，勿改回 |safe + json.dumps）
        "eff_payload": {
            "eff": eff,
            "active": {
                "llm": cfg.active_preset_llm,
                "embedding": cfg.active_preset_embedding,
                "retrieval": cfg.active_preset_retrieval,
                "mineru": cfg.active_preset_mineru,
                "rerank": cfg.active_preset_rerank,
            },
        },
        "placeholders": placeholders,
        "cfg": cfg,
        "presets_by_cat": dict(presets_by_cat),
        "active": {
            "llm": cfg.active_preset_llm,
            "embedding": cfg.active_preset_embedding,
            "retrieval": cfg.active_preset_retrieval,
            "mineru": cfg.active_preset_mineru,
            "rerank": cfg.active_preset_rerank,
        },
        "active_pid": active_pid,
    })


def _apply_form_to_cfg(cfg, post, category: str = ""):
    """把 POST 表单值写入 SiteConfig（空值清空以回退 .env）。

    指定 category 时只应用该分类的字段（弹窗按分类提交，未提交的其它分类
    保持原值不被清空）；不指定则应用全部字段（整表提交的旧路径）。
    """
    from .config import normalize_openai_base_url
    from .models import PRESET_CATEGORIES

    allowed = set(PRESET_CATEGORIES[category]) if category in PRESET_CATEGORIES else None

    for form_name, model_field, ftype, _env in _CONFIG_FIELDS:
        if allowed is not None and model_field not in allowed:
            continue
        if ftype == "bool":  # checkbox：勾选=任意真值，未勾选=缺省
            setattr(cfg, model_field, bool((post.get(form_name) or "").strip()))
            continue
        raw = (post.get(form_name) or "").strip()
        if raw == "":
            setattr(cfg, model_field, "" if ftype in ("text", "password") else None)
            continue
        if ftype == "float":
            setattr(cfg, model_field, float(raw))
        elif ftype == "int":
            setattr(cfg, model_field, int(raw))
        else:
            if model_field == "llm_base_url":
                raw = normalize_openai_base_url(raw)
            elif model_field == "embedding_base_url":
                raw = normalize_openai_base_url(raw, ollama=True)
            setattr(cfg, model_field, raw)


@_is_staff
def settings_test(request):
    """使用已保存配置测试；健康探针与模型推理结果分别标明。"""
    import httpx
    from . import config as cfg_mod

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    target = (request.POST.get("target") or "").strip()
    results = {}
    if target not in ("", "all", "llm", "embedding", "mineru", "rerank"):
        return JsonResponse({"error": "未知测试目标"}, status=400)

    def _headers(api_key):
        # Local OpenAI-compatible servers generally ignore this placeholder;
        # supplying it keeps their behaviour aligned with the runtime SDK.
        return {"Authorization": f"Bearer {api_key or 'local-no-key'}"}

    def _ping_llm(base_url, api_key, model):
        """Run a minimal chat completion, not just a shallow /models probe."""
        try:
            if not base_url:
                return {"ok": False, "status": None, "detail": "请填写 LLM Base URL。"}
            if not model:
                return {"ok": False, "status": None, "detail": "请填写 LLM 模型名称。"}
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "只回复 OK"}],
                "temperature": 0,
                "max_tokens": 64,
                "stream": False,
            }
            payload.update(settings.LLM_EXTRA_BODY)
            r = httpx.post(
                base_url.rstrip("/") + "/chat/completions",
                headers=_headers(api_key), json=payload, timeout=60,
            )
            r.raise_for_status()
            body = r.json()
            choices = body.get("choices") or []
            choice = choices[0] if choices else {}
            content = (choice.get("message") or {}).get("content")
            if choice.get("finish_reason") != "stop" or not isinstance(content, str) or content.strip().upper() != "OK":
                return {"ok": False, "status": r.status_code, "detail": "接口有响应，但未完整返回测试答案 OK（可能为空、截断或仅有思考内容）。"}
            actual = body.get("model") or "未报告"
            matched = actual == model or actual.rstrip("/").split("/")[-1] == model.rstrip("/").split("/")[-1]
            return {"ok": True if matched else None, "status": r.status_code,
                    "detail": f"实际推理通过；请求模型：{model}；服务返回模型：{actual}。" + ("" if matched else "模型名不一致或未报告，不能确认指定模型已启动；请核对代理映射。")}
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            return {"ok": False, "status": status, "detail": "调用失败：" + str(e)[:160]}

    def _ping_embedding(base_url, api_key, model):
        """Create one real embedding so URL/model compatibility is verified."""
        from .pipeline import WeMMEmbeddings, is_wemm

        try:
            if not base_url:
                return {"ok": False, "status": None, "detail": "请填写向量模型 Base URL。"}
            if not model:
                return {"ok": False, "status": None, "detail": "请填写向量模型名称。"}
            if is_wemm(model) and not base_url.rstrip("/").endswith("/v1"):
                # 自托管 WeMM 服务：/embed 协议，空闲自动卸载，冷加载需等模型上卡
                vec = WeMMEmbeddings(base_url=base_url).embed_query("连接测试")
                return {"ok": True, "status": 200,
                        "detail": f"真实向量生成成功（模型：{model}，维度：{len(vec)}）"}
            r = httpx.post(
                base_url.rstrip("/") + "/embeddings",
                headers=_headers(api_key),
                json={"model": model, "input": ["连接测试"], **({"dimensions": cfg_mod.embedding_settings()["dimensions"]} if cfg_mod.embedding_settings().get("dimensions") else {})}, timeout=60,
            )
            r.raise_for_status()
            data = r.json().get("data") or []
            vector = data[0].get("embedding") if data else None
            import math
            expected = cfg_mod.embedding_settings().get("dimensions")
            if not isinstance(vector, list) or not vector or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector) or not any(vector):
                return {"ok": False, "status": r.status_code, "detail": "服务没有返回向量。"}
            if expected and len(vector) != expected:
                return {"ok": True, "warning": True, "status": r.status_code, "detail": f"向量服务调用成功；索引兼容性警告：配置 {expected} 维，实际 {len(vector)} 维。当前模型不能直接用于旧索引，须按此模型重建索引或恢复原嵌入模型。"}
            return {"ok": True, "status": r.status_code,
                    "detail": f"真实向量生成成功（模型：{model}，维度：{len(vector)}）"}
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            return {"ok": False, "status": status, "detail": "调用失败：" + str(e)[:160]}

    def _ping_mineru(base_url, api_key):
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            r = httpx.get(base_url.rstrip("/") + "/health", headers=headers, timeout=8)
            return {"ok": r.status_code == 200, "status": r.status_code, "detail": "健康端点可达（按健康检查判定 OK）。" if r.status_code == 200 else "健康端点异常"}
        except Exception as e:
            return {"ok": False, "status": None, "detail": "连接失败：" + str(e)[:100]}

    llm = cfg_mod.llm_settings()
    emb = cfg_mod.embedding_settings()
    mineru = cfg_mod.mineru_settings()

    if target in ("", "all", "llm"):
        results["llm"] = _ping_llm(llm["base_url"], llm["api_key"], llm["model"])
    if target in ("", "all", "embedding"):
        results["embedding"] = _ping_embedding(
            emb["base_url"], emb["api_key"], emb["model"],
        )
    if target in ("", "all", "mineru"):
        results["mineru"] = _ping_mineru(mineru["api_base"], mineru["api_key"])
    if target in ("", "all", "rerank"):
        # 用已保存配置做「相关 vs 无关」对照测试（sanity_check 只读配置）
        from .rerank import sanity_check
        sc = sanity_check()
        results["rerank"] = {
            "ok": bool(sc.get("ok")),
            "status": None,
            "detail": sc.get("detail", ""),
        }
    from django.utils import timezone
    return JsonResponse({"results": results, "tested_at": timezone.now().isoformat()})


def home(request):
    """落地页：品牌 + 一句话说明 + 三项能力 + 真实库数据统计。"""
    from django.db.models import Sum

    from .models import Document, KnowledgeBase
    return render(request, "home.html", {
        "kb_count": KnowledgeBase.objects.filter(is_folder=True).count(),
        "doc_count": Document.objects.filter(status=Document.Status.COMPLETED).count(),
        "chunk_count": KnowledgeBase.objects.filter(is_folder=False)
                       .aggregate(n=Sum("chunk_count"))["n"] or 0,
    })
