from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from kb.models import Document, KnowledgeBase


class ReindexFailedTests(TestCase):
    def setUp(self):
        self.lib = KnowledgeBase.objects.create(name="FL", slug="fl-lib")
        self.doc = Document.objects.create(
            kb=self.lib, original_name="FL.pdf", status=Document.Status.FAILED,
            md_content="## 电气\n<table><tr><td>45</td><td>JB2</td></table>",
            error_msg="表格 HTML 不完整",
        )

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        command = "kb.management.commands.reindex_failed"
        with patch("kb.pipeline._embeddings"), \
                patch(f"{command}.reset_kb_collection") as self.rmtree, \
                patch("kb.keyword_index.delete_kb"), \
                patch(f"{command}.run_indexing", return_value=7) as index:
            call_command("reindex_failed", *args, stdout=out, stderr=err)
        return index, out.getvalue() + err.getvalue()

    def test_recovers_failed_document_without_ocr(self):
        index, _ = self.run_command()
        index.assert_called_once()
        self.rmtree.assert_called_once()
        self.assertEqual(index.call_args.args[:3], (self.doc.md_content, "fl-lib", "FL.pdf"))
        self.doc.refresh_from_db()
        self.lib.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.COMPLETED)
        self.assertEqual((self.doc.chunk_count, self.doc.error_msg), (7, ""))
        self.assertEqual((self.lib.doc_count, self.lib.chunk_count), (1, 7))

    def test_shared_library_is_never_cleared(self):
        Document.objects.create(kb=self.lib, original_name="other.pdf",
                                status=Document.Status.COMPLETED, chunk_count=5)
        index, output = self.run_command()
        index.assert_not_called()
        self.rmtree.assert_not_called()
        self.assertIn("跳过 FL.pdf", output)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.FAILED)

    def test_dry_run_changes_nothing(self):
        index, output = self.run_command("--dry-run")
        index.assert_not_called()
        self.assertIn("表格 HTML 不完整", output)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.FAILED)
