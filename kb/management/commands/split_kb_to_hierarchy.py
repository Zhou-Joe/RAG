"""把扁平的多文档知识库拆成「文件夹 → 文档库」层级。

每个含多份文档的文档库会被改造为：
  1. 原 KB 改成文件夹（is_folder=True，保留 name/slug/description）
  2. 每份文档各创建一个子文档库（slug 从文件名派生，如 p8-report）
  3. Document.kb 重指向对应子库
  4. 删旧 data/chroma/<old_slug>/，按文档各自重新向量化到 data/chroma/<child_slug>/
  5. 更新各子库 doc_count/chunk_count

单文档的文档库不拆（无跨文档混杂问题），仅保留为顶层文档库。

幂等、可重跑：
  - 已经是文件夹的 KB 跳过
  - 已存在的子库 slug 复用（按 slug 去重）

需要联网调 embedding API（重新向量化）。
用法：
    python manage.py split_kb_to_hierarchy            # 拆分 + 重建索引
    python manage.py split_kb_to_hierarchy --dry-run  # 只打印将要做的事，不改动
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from kb.models import Document, KnowledgeBase
from kb.pipeline import _kb_persist_dir, run_indexing


def _derive_slug(original_name: str, used: set[str]) -> str:
    """从文件名派生一个合法、唯一、ASCII 的子库 slug。

    策略：去掉扩展名 → 保留字母数字（中文转拼音不可靠，这里直接折成 ASCII）→
    连接符归一 → 截断 → 去重（后缀 -2/-3…）。
    """
    stem = Path(original_name).stem
    # NFKD 拆字后丢弃非 ASCII（中文等会被去掉），保留可读的英文/数字部分
    ascii_part = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    # 取所有连续字母数字段，用连字符拼起来
    tokens = re.findall(r"[A-Za-z0-9]+", ascii_part)
    base = "-".join(t.lower() for t in tokens) if tokens else "doc"
    base = re.sub(r"-{2,}", "-", base).strip("-")
    base = base[:60] or "doc"
    slug = base
    n = 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    used.add(slug)
    return slug


def _derive_name(original_name: str) -> str:
    """从文件名派生子库的展示名。"""
    stem = Path(original_name).stem
    # 去掉常见的尾部版本/编号噪声，保留前 50 字符
    return (stem[:50]).strip() or original_name


class Command(BaseCommand):
    help = "把扁平的多文档知识库拆成「文件夹 → 文档库」层级，并按文档重新向量化"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只打印将要执行的操作，不实际改动数据库或向量库",
        )
        parser.add_argument(
            "--slug",
            default="",
            help="只处理指定 slug 的知识库（默认处理所有需要拆分的库）",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        only_slug = options["slug"]

        # 候选：文档库（is_folder=False）且挂了 >1 份已完成文档
        qs = KnowledgeBase.objects.filter(is_folder=False)
        if only_slug:
            qs = qs.filter(slug=only_slug)

        targets: list[KnowledgeBase] = []
        for kb in qs:
            n_docs = kb.documents.filter(status=Document.Status.COMPLETED).count()
            if n_docs > 1:
                targets.append(kb)
            else:
                self.stdout.write(f"跳过「{kb.name}」（slug={kb.slug}）：已完成文档 ≤ 1，无需拆分。")

        if not targets:
            self.stdout.write(self.style.WARNING("没有需要拆分的多文档知识库。"))
            return

        self.stdout.write(f"发现 {len(targets)} 个待拆分的知识库。")
        if dry:
            self.stdout.write(self.style.WARNING("【dry-run 模式】不会实际改动。"))

        for kb in targets:
            self._split_one(kb, dry=dry)

        self.stdout.write(self.style.SUCCESS("全部处理完成。"))

    def _split_one(self, kb: KnowledgeBase, *, dry: bool) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== 拆分「{kb.name}」（slug={kb.slug}）==="))
        completed = list(kb.documents.filter(status=Document.Status.COMPLETED))
        other = list(kb.documents.exclude(status=Document.Status.COMPLETED))
        self.stdout.write(f"  已完成文档 {len(completed)} 份，未完成 {len(other)} 份。")

        if not completed:
            self.stdout.write(self.style.WARNING("  无已完成文档，跳过。"))
            return

        # 子库 slug 预留：当前已用 slug + 即将派生的
        used_slugs = set(KnowledgeBase.objects.values_list("slug", flat=True))
        child_specs: list[dict] = []  # [{doc, name, slug}]
        for doc in completed:
            slug = _derive_slug(doc.original_name, used_slugs)
            name = _derive_name(doc.original_name)
            child_specs.append({"doc": doc, "name": name, "slug": slug})
            self.stdout.write(f"  → 子库「{name}」slug={slug} ← 文档 {doc.original_name}")

        if dry:
            self.stdout.write(self.style.WARNING("  [dry-run] 到此为止。"))
            return

        # 真正改动：在一个事务里改 DB 结构（KB→文件夹 + 建子库 + 文档重指向）
        old_slug = kb.slug
        old_chroma = Path(settings.CHROMA_ROOT) / old_slug

        with transaction.atomic():
            # ① 原 KB 改成文件夹（保留 name/slug/description，清空自身统计）
            kb.is_folder = True
            kb.doc_count = 0
            kb.chunk_count = 0
            kb.save(update_fields=["is_folder", "doc_count", "chunk_count", "updated_at"])

            # ② 为每份文档建子库 + 重指向 Document.kb
            for spec in child_specs:
                child = KnowledgeBase.objects.create(
                    name=spec["name"],
                    slug=spec["slug"],
                    description=f"自动拆分自「{kb.name}」",
                    parent=kb,
                    is_folder=False,
                    created_by=kb.created_by,
                )
                doc = spec["doc"]
                doc.kb = child
                doc.save(update_fields=["kb", "updated_at"])
                spec["child"] = child

        # ③ 删旧 Chroma 目录（旧 collection 含多文档混杂向量，不再需要）
        if old_chroma.exists():
            self.stdout.write(f"  删除旧向量目录 {old_chroma} …")
            shutil.rmtree(old_chroma, ignore_errors=True)

        # ④ 按文档各自重新向量化到 data/chroma/<child_slug>/
        #    （在事务外执行：调 embedding API，耗时且可能失败，不应回滚 DB 结构）
        for spec in child_specs:
            child = spec["child"]
            doc = spec["doc"]
            self.stdout.write(f"  重新向量化「{doc.original_name}」→ {child.slug} …")
            try:
                n = run_indexing(doc.md_content, child.slug, doc.original_name)
                child.doc_count = 1
                child.chunk_count = n
                child.save(update_fields=["doc_count", "chunk_count", "updated_at"])
                self.stdout.write(self.style.SUCCESS(f"    ✓ {n} 个向量块"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"    ✗ 向量化失败：{e}"))
                self.stderr.write(self.style.ERROR(
                    f"      子库「{child.slug}」已建好但无向量。修复：在该子库详情页重新上传此文档，"
                    f"或解决 embedding 连接后重跑此命令。"
                ))

        # 未完成文档（pending/failed 等）留在原 KB（现已是文件夹）下；
        # 它们没有向量，不影响检索。提示用户按需手动归档。
        if other:
            names = "、".join(d.original_name for d in other)
            self.stdout.write(self.style.WARNING(
                f"  注意：{len(other)} 份未完成文档（{names}）仍挂在文件夹「{kb.name}」下。"
                "文件夹不直接挂文档，请在管理页把它们移动/重新上传到对应子库。"
            ))

        # 刷新进程内向量库缓存，避免后续检索拿到旧的 collection 句柄
        try:
            from kb.retriever import _VS_CACHE
            _VS_CACHE.pop(old_slug, None)
        except Exception:
            pass
