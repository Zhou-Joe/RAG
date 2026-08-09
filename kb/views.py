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
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
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


def _recount(kb: KnowledgeBase) -> None:
    """重算一个（子/顶层）库的 doc_count/chunk_count（仅 completed 文档）。"""
    docs = kb.documents.filter(status=Document.Status.COMPLETED)
    kb.doc_count = docs.count()
    kb.chunk_count = sum(d.chunk_count for d in docs)
    kb.save(update_fields=["doc_count", "chunk_count", "updated_at"])
_is_staff = user_passes_test(lambda u: u.is_staff)


# ------------------------------------------------------------------
# 管理页面（仅 staff）
# ------------------------------------------------------------------
@_is_staff
@login_required
def manage_list(request):
    """手册库列表 + 创建。

    用户只需填名称：slug 由系统从名称自动派生（技术细节，不暴露给用户）；
    一律建为顶层库（is_folder=True，可挂多份文档、跨文档检索）。
    """
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if name:
            slug = _derive_unique_slug(name)
            KnowledgeBase.objects.create(
                name=name, slug=slug,
                description="", created_by=request.user,
                is_folder=True, parent=None,
            )
            messages.success(request, f"手册库「{name}」已创建，可以往里上传文档了。")
        return redirect("kb:manage_list")

    # 顶层库（文件夹 + 独立文档库）扁平渲染；子库对用户透明，不单独展示
    kbs = KnowledgeBase.objects.filter(parent__isnull=True).order_by("name")
    return render(request, "kb/manage_list.html", {"kbs": kbs})


@_is_staff
@login_required
def manage_detail(request, slug):
    """知识库详情。

    对用户而言：一个库（文件夹或文档库）就是一个能直接上传/查看文档的地方。
    - 文档库：文档直接挂上来（每库一个 Chroma collection）。
    - 文件夹：上传时系统背后自动为该文档建一个子文档库（每文档独占 collection，
      保持检索隔离），用户只看到文档列表，无需感知「子库」。
    """
    kb = get_object_or_404(KnowledgeBase, slug=slug)

    # ---------- 上传文档（文件夹 & 文档库 统一处理） ----------
    if request.method == "POST" and request.FILES.get("file"):
        upload = request.FILES["file"]
        fname = upload.name
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        file_type = {"pdf": "pdf", "md": "md", "markdown": "md", "txt": "txt"}.get(ext, "pdf")

        # 文件夹：背后自动建一个子文档库挂这份文档（检索隔离）
        if kb.is_folder:
            child = KnowledgeBase.objects.create(
                name=fname[:80],
                slug=_derive_unique_slug(fname),
                description=f"自动创建：{fname}",
                is_folder=False,
                parent=kb,
                created_by=request.user,
            )
            target_kb = child
        else:
            target_kb = kb

        doc = Document.objects.create(
            kb=target_kb,
            original_name=fname,
            file=upload,
            file_type=file_type,
            status=Document.Status.PENDING,
        )
        from .pipeline import process_document_async
        process_document_async(doc.id)
        messages.success(request, f"已上传「{fname}」，正在后台处理…")
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


@_is_staff
@login_required
def manage_delete(request, slug):
    """删除知识库（删 Chroma 目录 + DB 记录）。

    文件夹：级联删除所有子文档库（CASCADE）及其向量目录。
    """
    kb = get_object_or_404(KnowledgeBase, slug=slug)
    if request.method == "POST":
        # 收集要清理向量目录的所有 slug：自身 + （文件夹的）所有子库
        slugs = [kb.slug]
        if kb.is_folder:
            slugs += list(kb.children.values_list("slug", flat=True))
        for s in slugs:
            chroma_dir = Path(settings.CHROMA_ROOT) / s
            if chroma_dir.exists():
                shutil.rmtree(chroma_dir, ignore_errors=True)
            md_dir = Path(settings.MD_ROOT) / s
            if md_dir.exists():
                shutil.rmtree(md_dir, ignore_errors=True)
        name = kb.name
        kb.delete()  # CASCADE 会删掉子库及其文档
        messages.success(request, f"知识库「{name}」已删除。")
    return redirect("kb:manage_list")


@_is_staff
@login_required
@require_http_methods(["POST"])
def kb_rename(request, slug):
    """重命名知识库（仅名称；slug 保持不变，避免迁移向量目录）。

    AJAX 优先：返回 JSON；非 AJAX 降级为重定向。
    """
    kb = get_object_or_404(KnowledgeBase, slug=slug)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return JsonResponse({"ok": False, "message": "名称不能为空"}, status=400)
    kb.name = name
    kb.save(update_fields=["name", "updated_at"])

    is_ajax = (request.headers.get("x-requested-with") == "XMLHttpRequest"
               or "application/json" in (request.META.get("HTTP_ACCEPT") or ""))
    if is_ajax:
        return JsonResponse({"ok": True, "name": kb.name})
    messages.success(request, f"已重命名为「{kb.name}」。")
    return redirect("kb:manage_list")


@_is_staff
@login_required
@require_http_methods(["POST"])
def doc_delete(request, slug, doc_id):
    """删除某知识库下的一份文档（文件夹则跨其所有子库查找）。

    清理：① Chroma 中该文档的向量（按 source=original_name 过滤）；
         ② 上传的原始文件；③ DB 记录；④ 文件夹下自动建的空子库一并清理。
    向量删除失败不阻断 DB 删除（避免垃圾数据残留）。
    """
    kb = get_object_or_404(KnowledgeBase, slug=slug)
    doc = get_object_or_404(Document, id=doc_id, kb__in=_scope_kb_ids(kb))
    name = doc.original_name
    doc_kb = doc.kb  # 文档实际所在的（子）库

    # ① 删 Chroma 向量（按 source 过滤；用文档所在库的 slug）
    try:
        from .retriever import get_kb_vectorstore
        vs = get_kb_vectorstore(doc_kb.slug)
        vs._collection.delete(where={"source": name})  # noqa: SLF001
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


@_is_staff
@login_required
def doc_status_api(request, slug):
    """AJAX：返回该 KB 下所有文档的最新状态（供前端轮询）+ 进度百分比。

    文件夹则跨其所有子库扁平返回（用户只看到文档，不感知子库）。
    """
    # status → progress 百分比映射
    PROGRESS_MAP = {
        "pending": 0, "ocr": 25, "indexing": 75,
        "completed": 100, "failed": 100,
    }
    kb = get_object_or_404(KnowledgeBase, slug=slug)
    docs_data = [
        {
            "id": str(d.id),
            "name": d.original_name,
            "status": d.status,
            "status_display": d.get_status_display(),
            "stage_detail": d.stage_detail or "",
            "progress": PROGRESS_MAP.get(d.status, 0),
            "chunk_count": d.chunk_count,
            "error": d.error_msg[:100] if d.error_msg else "",
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
def document_html(request, doc_id):
    """文档 HTML 查看页：渲染文档正文，支持 ?h= 高亮指定文本片段。

    - 若 html_content 为空但 md_content 有值 → 懒构建（安全网）。
    - ?h=<文本片段>：前端据此高亮（来源出处点击后携带）。
    """
    doc = get_object_or_404(Document, id=doc_id)
    if not doc.html_content and doc.md_content:
        from .pipeline import build_doc_html
        build_doc_html(doc)
    doc.refresh_from_db()
    # 支持多个 ?h= 参数：每个是待高亮的文本片段（来源出处点击后携带）
    highlights = [h.strip() for h in request.GET.getlist("h") if h.strip()]
    return render(request, "kb/document_html.html", {
        "doc": doc,
        "html_body": doc.html_content or "",
        "highlights_json": json.dumps(highlights, ensure_ascii=False),
    })


# ------------------------------------------------------------------
# 问答页（所有登录用户）
# ------------------------------------------------------------------
@login_required
def ask(request):
    """问答主页：提问 + 历史会话列表。

    选库由 agent 自主完成（list_knowledge_bases + kb_search），无需前端手动选择。
    """
    conversations = (
        Conversation.objects.filter(user=request.user)
        .select_related("kb").only("id", "title", "thread_id", "kb__name", "updated_at", "created_at")
    )
    return render(request, "kb/ask.html", {
        "conversations": conversations,
        "active_thread": request.GET.get("conv", ""),
    })


@csrf_exempt
@login_required
async def chat_stream(request):
    """SSE 流式问答端点。

    参数: message, thread_id, kb_slug
    """
    from .agent import run_agent_stream
    from .config import llm_settings, retrieval_settings

    message = (request.POST.get("message") or request.GET.get("message") or "").strip()
    thread_id = request.POST.get("thread_id") or request.GET.get("thread_id") or ""
    kb_slug = request.POST.get("kb_slug") or request.GET.get("kb_slug") or ""

    if not message:
        return HttpResponse("missing 'message'", status=400)
    if not thread_id:
        return HttpResponse("missing 'thread_id'", status=400)

    # 在同步上下文里一次性解析配置 + 取/建会话（避免在 async 生成器中访问数据库）
    def _prepare():
        # kb_slug 可选：未指定时优先选「有向量块的文档库」（文件夹自身无向量），
        # 再退到任意文件夹（可扇出搜索），最后退到任意库。
        # agent 仍可通过 list_knowledge_bases + kb_search(kb_slug=...) 自主跨库。
        if kb_slug:
            kb = KnowledgeBase.objects.filter(slug=kb_slug).first()
        else:
            kb = (
                KnowledgeBase.objects.filter(is_folder=False, chunk_count__gt=0).first()
                or KnowledgeBase.objects.filter(is_folder=False).first()
                or KnowledgeBase.objects.filter(is_folder=True).first()
                or KnowledgeBase.objects.first()
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
        # 记录用户消息
        Message.objects.create(conversation=conv, role=Message.Role.USER, content=message)
        conv.save(update_fields=["updated_at"])  # 刷新排序
        cfg = {"llm": llm_settings(), "top_k": retrieval_settings()["top_k"]}
        return conv, cfg, created, None

    prepared = await sync_to_async(_prepare)()
    conv, agent_config, _created, prep_err = prepared
    if prep_err == "no knowledge base available":
        return HttpResponse("暂无知识库，请先上传文档。", status=400)
    if conv is None:
        return HttpResponse("forbidden", status=403)

    full_thread = conv.thread_id  # 会话的稳定 thread_id（直接用作 checkpointer key）

    async def event_stream():
        # 收集 AI 回复文本 + 来源出处，流结束后落库
        ai_chunks: list[str] = []
        turn_citations: list[dict] = []
        try:
            async for event_type, payload in run_agent_stream(message, full_thread, kb_slug, agent_config):
                if event_type == "token":
                    ai_chunks.append(payload.get("text", ""))
                elif event_type == "citations":
                    turn_citations.extend(payload.get("citations") or [])
                data = json.dumps(payload, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {data}\n\n"
            yield "event: done\ndata: {}\n\n"
        finally:
            ai_text = "".join(ai_chunks).strip()
            if ai_text:
                await sync_to_async(Message.objects.create)(
                    conversation=conv, role=Message.Role.AI,
                    content=ai_text, citations=turn_citations,
                )

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
def conversation_messages(request, thread_id):
    """返回某会话的历史消息（JSON），供前端打开会话时渲染。"""
    conv = get_object_or_404(Conversation, thread_id=thread_id, user=request.user)
    msgs = list(conv.messages.order_by("created_at").values("role", "content", "citations"))
    return JsonResponse({"thread_id": conv.thread_id, "title": conv.title,
                         "kb_slug": conv.kb.slug, "messages": msgs})


@login_required
@require_http_methods(["POST"])
def conversation_delete(request, thread_id):
    """删除某会话（仅元数据 + 消息；checkpointer 数据保留无妨）。"""
    conv = get_object_or_404(Conversation, thread_id=thread_id, user=request.user)
    conv.delete()
    messages.success(request, "会话已删除。")
    return redirect("kb:ask")


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
]


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
        }

    if request.method == "POST":
        action = request.POST.get("action", "save")

        # ---- 保存为预设（按分类）：只存该分类的字段 ----
        if action == "preset_save":
            name = (request.POST.get("preset_name") or "").strip()
            category = request.POST.get("category", "")
            if not name:
                msg = "请填写预设名称。"
                if is_ajax: return _json_response(False, msg)
                messages.error(request, msg); return redirect("kb:settings")
            if category not in PRESET_CATEGORIES:
                msg = "分类无效。"
                if is_ajax: return _json_response(False, msg)
                messages.error(request, msg); return redirect("kb:settings")
            fields = PRESET_CATEGORIES[category]
            _apply_form_to_cfg(cfg, request.POST)
            cfg.save()
            preset, created = ConfigPreset.objects.update_or_create(
                name=name, category=category,
                defaults={"data": cfg.snapshot(fields)},
            )
            msg = f"{dict(ConfigPreset.Category.choices)[category]} 预设「{name}」已{'创建' if created else '更新'}。"
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
                msg = "预设不存在。"
                if is_ajax: return _json_response(False, msg)
                messages.error(request, msg); return redirect("kb:settings")
            fields = PRESET_CATEGORIES.get(preset.category, [])
            cfg.apply(preset.data, fields)
            cfg.save()
            msg = f"已加载预设「{preset.name}」（下一次请求即生效）。"
            if is_ajax: return _json_response(True, msg, eff=_current_eff())
            messages.success(request, msg); return redirect("kb:settings")

        # ---- 删除预设 ----
        if action == "preset_delete":
            pid = request.POST.get("preset_id")
            preset = ConfigPreset.objects.filter(id=pid).first()
            msg = "预设不存在。"
            if preset:
                name = preset.name
                preset.delete()
                msg = f"预设「{name}」已删除。"
            if is_ajax: return _json_response(True, msg, extra={"preset_id": pid})
            messages.success(request, msg); return redirect("kb:settings")

        # ---- 默认：保存当前配置 ----
        try:
            _apply_form_to_cfg(cfg, request.POST)
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
    from . import config as cfg_mod
    eff = {
        "llm": cfg_mod.llm_settings(),
        "embedding": cfg_mod.embedding_settings(),
        "retrieval": cfg_mod.retrieval_settings(),
        "mineru": cfg_mod.mineru_settings(),
    }
    placeholders = {env: getattr(dj_settings, env, "") for _f, _m, _t, env in _CONFIG_FIELDS}
    # 按分类分组预设，传给模板
    from collections import defaultdict
    presets_by_cat = defaultdict(list)
    for p in ConfigPreset.objects.all():
        presets_by_cat[p.category].append(p)
    return render(request, "kb/settings.html", {
        "eff": eff,
        "placeholders": placeholders,
        "cfg": cfg,
        "presets_by_cat": dict(presets_by_cat),
    })


def _apply_form_to_cfg(cfg, post):
    """把 POST 表单值写入 SiteConfig（空值清空以回退 .env）。"""
    for form_name, model_field, ftype, _env in _CONFIG_FIELDS:
        raw = (post.get(form_name) or "").strip()
        if raw == "":
            setattr(cfg, model_field, "" if ftype in ("text", "password") else None)
            continue
        if ftype == "float":
            setattr(cfg, model_field, float(raw))
        elif ftype == "int":
            setattr(cfg, model_field, int(raw))
        else:
            setattr(cfg, model_field, raw)


@_is_staff
def settings_test(request):
    """测试 LLM / Embedding / MinerU 连接（仅 staff）。返回 JSON。"""
    import httpx
    from . import config as cfg_mod

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    target = (request.POST.get("target") or "").strip()
    results = {}

    def _ping_openai(name, base_url, api_key):
        """OpenAI 兼容服务：用 /models 验证连接 + 密钥。

        200 → 列出模型；401/403 → 密钥无效；404 → 可达但无 /models
        （仍视为可达，只提示无列表端点）。
        """
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            r = httpx.get(base_url.rstrip("/") + "/models", headers=headers, timeout=10)
            if r.status_code == 200:
                return {"ok": True, "status": 200, "detail": "连接成功，密钥有效"}
            if r.status_code in (401, 403):
                return {"ok": False, "status": r.status_code, "detail": "密钥无效或无权限：" + r.text[:80]}
            if r.status_code == 404:
                # 可达但无 /models 端点 → 不一定是失败，单独标注
                return {"ok": True, "status": 404, "detail": "可达，但该服务无 /models 列表端点（如自定义网关）；密钥状态未知。"}
            return {"ok": False, "status": r.status_code, "detail": r.text[:120]}
        except Exception as e:
            return {"ok": False, "status": None, "detail": "连接失败：" + str(e)[:100]}

    def _ping_mineru(base_url, api_key):
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            r = httpx.get(base_url.rstrip("/") + "/health", headers=headers, timeout=8)
            return {"ok": 200 <= r.status_code < 400, "status": r.status_code, "detail": r.text[:120]}
        except Exception as e:
            return {"ok": False, "status": None, "detail": "连接失败：" + str(e)[:100]}

    llm = cfg_mod.llm_settings()
    emb = cfg_mod.embedding_settings()
    mineru = cfg_mod.mineru_settings()

    if target in ("", "all", "llm"):
        results["llm"] = _ping_openai("LLM", llm["base_url"], llm["api_key"])
    if target in ("", "all", "embedding"):
        results["embedding"] = _ping_openai("Embedding", emb["base_url"], emb["api_key"])
    if target in ("", "all", "mineru"):
        results["mineru"] = _ping_mineru(mineru["api_base"], mineru["api_key"])
    return JsonResponse({"results": results})
