"""回填/重建 Document 的 html_content。

用法：
    python manage.py build_doc_html          # 只补建 md_content 有但 html 为空的文档
    python manage.py build_doc_html --force  # 强制重建全部（含已有 html 的）

从已存的 md_content 生成 HTML，不重新 OCR、不重新向量化。
"""
from django.core.management.base import BaseCommand

from kb.models import Document
from kb.pipeline import build_doc_html


class Command(BaseCommand):
    help = "根据 md_content 生成/重建 Document 的 html_content（供文档查看页）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="强制重建全部文档的 HTML（默认只补建空缺的）",
        )

    def handle(self, *args, **options):
        force = options["force"]
        qs = Document.objects.exclude(md_content="")
        if not force:
            qs = qs.filter(html_content="")
        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("没有需要处理的文档。"))
            return

        self.stdout.write(f"开始{'强制重建' if force else '补建'} {total} 份文档的 HTML…")
        done = 0
        for doc in qs.only("id", "md_content", "html_content"):
            try:
                build_doc_html(doc)
                done += 1
                self.stdout.write(f"  ✓ {doc.original_name}")
            except Exception as e:  # 单份失败不中断整体
                self.stderr.write(self.style.ERROR(f"  ✗ {doc.original_name}: {e}"))
        self.stdout.write(self.style.SUCCESS(f"完成：{done}/{total} 份文档 HTML 已生成。"))
