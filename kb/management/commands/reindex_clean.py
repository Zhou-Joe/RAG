"""用清洗后的文本重新向量化已处理文档（不重新 OCR）。

背景：嵌入路径改造前，向量是直接拿 MinerU 原始 markdown（含大量 HTML 表格标签）
做 embedding 的。现在 run_indexing 会先把 HTML 表格转成结构化纯文本再切块/向量化，
本命令对已 completed 的文档重跑一遍 run_indexing，让清洗在新向量上生效。

幂等、可重跑。需要联网调 embedding API。
用法：
    python manage.py reindex_clean             # 所有已完成的文档
    python manage.py reindex_clean --kb <slug> # 只重建某个文档库
    python manage.py reindex_clean --dry-run   # 只列出将处理的文档
"""
from django.core.management.base import BaseCommand

from kb.models import Document, KnowledgeBase
from kb.pipeline import run_indexing
from kb.retriever import get_kb_vectorstore, _VS_CACHE


class Command(BaseCommand):
    help = "用清洗后的文本重新向量化已处理文档（HTML 表格 → 结构化文本后再 embedding）"

    def add_arguments(self, parser):
        parser.add_argument("--kb", default="", help="只重建指定 slug 的文档库")
        parser.add_argument("--dry-run", action="store_true", help="只列出待处理文档，不实际重建")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        only_slug = options["kb"]

        qs = Document.objects.filter(status=Document.Status.COMPLETED).exclude(md_content="")
        if only_slug:
            qs = qs.filter(kb__slug=only_slug)
        docs = list(qs)

        if not docs:
            self.stdout.write(self.style.WARNING("没有已完成的文档可重建。"))
            return

        self.stdout.write(f"待重建文档 {len(docs)} 份。")
        if dry:
            for d in docs:
                self.stdout.write(f"  · {d.original_name} (库 {d.kb.slug})")
            self.stdout.write(self.style.WARNING("[dry-run] 不实际重建。"))
            return

        # 按文档库分组，每个库处理完后统一刷新统计 + 缓存
        by_kb: dict[str, list[Document]] = {}
        for d in docs:
            by_kb.setdefault(d.kb.slug, []).append(d)

        total_done = 0
        for kb_slug, lib_docs in by_kb.items():
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== 文档库「{kb_slug}」({len(lib_docs)} 份) ==="))
            kb = lib_docs[0].kb
            new_chunk_total = 0
            for doc in lib_docs:
                # 先删该文档旧向量（清洗前 embedding 的），再重新索引
                try:
                    vs = get_kb_vectorstore(kb_slug)
                    vs._collection.delete(where={"source": doc.original_name})  # noqa: SLF001
                except Exception as e:
                    self.stderr.write(self.style.WARNING(f"  删旧向量失败（{doc.original_name}）: {e}"))
                try:
                    n = run_indexing(doc.md_content, kb_slug, doc.original_name)
                    doc.chunk_count = n
                    doc.save(update_fields=["chunk_count", "updated_at"])
                    new_chunk_total += n
                    total_done += 1
                    self.stdout.write(self.style.SUCCESS(f"  ✓ {doc.original_name} → {n} 块"))
                except Exception as e:
                    self.stderr.write(self.style.ERROR(f"  ✗ {doc.original_name}: {e}"))

            # 刷新 KB 缓存统计
            kb.doc_count = kb.documents.filter(status=Document.Status.COMPLETED).count()
            kb.chunk_count = sum(
                d.chunk_count for d in kb.documents.filter(status=Document.Status.COMPLETED)
            )
            kb.save(update_fields=["doc_count", "chunk_count", "updated_at"])
            # 清掉该库的向量库缓存，让后续检索重新打开（向量已变）
            _VS_CACHE.pop(kb_slug, None)
            self.stdout.write(f"  库统计刷新：{kb.doc_count} 文档 / {kb.chunk_count} 向量块")

        self.stdout.write(self.style.SUCCESS(f"\n完成：{total_done}/{len(docs)} 份文档已用清洗后文本重建索引。"))
