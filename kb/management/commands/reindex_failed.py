"""用已保存的 OCR 结果重新索引处理失败的文档（不重新 OCR）。

适用：OCR 已完成、md_content 已落库，但切块/向量化阶段失败的文档
（如表格解析规则修复后）。只处理独占一个文档库的失败文档：先清掉该库
可能残留的半成品向量与关键词索引，再从 md_content 重建，不影响其它文档。

用法：
    python manage.py reindex_failed             # 所有可恢复的失败文档
    python manage.py reindex_failed --doc <id>  # 只处理指定文档
    python manage.py reindex_failed --dry-run   # 只列出将处理的文档
"""

from django.core.management.base import BaseCommand

from kb.models import Document
from kb.pipeline import _kb_persist_dir, run_indexing
from kb.retriever import reset_kb_collection


class Command(BaseCommand):
    help = "从已保存的 OCR 结果重新索引失败文档（不重新 OCR，仅限独占文档库的文档）"

    def add_arguments(self, parser):
        parser.add_argument("--doc", default="", help="只处理指定文档 ID")
        parser.add_argument("--dry-run", action="store_true", help="只列出待处理文档，不实际重建")

    def handle(self, *args, **options):
        qs = Document.objects.filter(status=Document.Status.FAILED).exclude(md_content="")
        if options["doc"]:
            qs = qs.filter(id=options["doc"])
        docs = []
        for doc in qs.select_related("kb"):
            if doc.kb.documents.exclude(id=doc.id).exists():
                self.stderr.write(self.style.WARNING(
                    f"  跳过 {doc.original_name}：所在文档库还有其它文档，请删除后重新上传"))
                continue
            docs.append(doc)

        if not docs:
            self.stdout.write(self.style.WARNING("没有可从 OCR 结果恢复的失败文档。"))
            return
        self.stdout.write(f"待恢复文档 {len(docs)} 份。")
        if options["dry_run"]:
            for d in docs:
                self.stdout.write(f"  · {d.original_name} (库 {d.kb.slug})：{d.error_msg[:80]}")
            self.stdout.write(self.style.WARNING("[dry-run] 不实际重建。"))
            return

        # 先删后建：端点不可用时不动任何数据。
        try:
            from kb.pipeline import _embeddings
            _embeddings().embed_query("重建预检")
        except Exception as e:
            self.stderr.write(self.style.ERROR(
                f"embedding 端点不可用，中止（未修改任何数据）: {str(e)[:160]}"))
            return

        from kb import keyword_index
        from kb import provenance as _prov
        done = 0
        for doc in docs:
            kb = doc.kb
            reset_kb_collection(kb.slug)
            keyword_index.delete_kb(kb.slug)
            try:
                n = run_indexing(doc.md_content, kb.slug, doc.original_name, doc_id=doc.id,
                                 content_list=_prov.load_content_list(doc.id))
            except Exception as e:
                doc.error_msg = str(e)[:2000]
                doc.save(update_fields=["error_msg", "updated_at"])
                self.stderr.write(self.style.ERROR(f"  ✗ {doc.original_name}: {e}"))
                continue
            doc.status = Document.Status.COMPLETED
            doc.chunk_count = n
            doc.error_msg = ""
            doc.stage_detail = f"已完成 · {n} 个片段"
            doc.save(update_fields=["status", "chunk_count", "error_msg", "stage_detail", "updated_at"])
            kb.doc_count = kb.documents.filter(status=Document.Status.COMPLETED).count()
            kb.chunk_count = sum(
                d.chunk_count for d in kb.documents.filter(status=Document.Status.COMPLETED))
            kb.save(update_fields=["doc_count", "chunk_count", "updated_at"])
            try:
                from kb import tracker as _tracker
                _tracker.run_extraction(doc.id)
            except Exception as e:
                self.stderr.write(self.style.WARNING(f"  追踪表抽取失败（不影响入库）: {e}"))
            done += 1
            self.stdout.write(self.style.SUCCESS(f"  ✓ {doc.original_name} → {n} 块"))

        self.stdout.write(self.style.SUCCESS(f"\n完成：{done}/{len(docs)} 份文档已恢复。"))
        self.stdout.write(self.style.WARNING("提醒：重建期间请暂停该库检索；完成后重启 Web 以刷新其他进程缓存。"))
