"""Rows from explicit table-query libraries remain complete and queryable."""
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from openpyxl import Workbook

from kb.models import Document, KbTracker, KnowledgeBase, TrackerRow
from kb.spreadsheet_rows import import_spreadsheet_document, read_spreadsheet_rows
from kb.tracker_query import TrackerQuery


class SpreadsheetRowsTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.media = Path(directory.name)
        settings = override_settings(MEDIA_ROOT=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.root = KnowledgeBase.objects.create(
            name='Uploaded tables', slug='uploaded-tables', is_folder=True, table_query=True)
        self.child = KnowledgeBase.objects.create(
            name='Source', slug='uploaded-source', parent=self.root)

    def document(self, payload=b'Name,Value\nA,1\nB,3\n', name='data.csv', child=None):
        doc = Document(kb=child or self.child, original_name=name,
                       file_type=name.rsplit('.', 1)[-1])
        doc.file.save(name, ContentFile(payload), save=False)
        doc.save()
        return doc

    def workbook(self, sheets):
        book = Workbook()
        book.remove(book.active)
        for title, matrix in sheets:
            sheet = book.create_sheet(title)
            for values in matrix:
                sheet.append(values)
        stream = BytesIO()
        book.save(stream)
        book.close()
        return stream.getvalue()

    def test_pipeline_imports_full_cells_and_all_rows_without_model_calls(self):
        from kb.pipeline import process_document
        long_value = '故障说明' * 40000
        doc = self.document(('Description,Value\n' + long_value + ',4\nMiddle,8\n').encode())
        with patch('kb.pipeline.run_ocr_with_images') as ocr, \
             patch('kb.pipeline.run_indexing') as embed, \
             patch('kb.tracker.run_extraction_async') as extract:
            process_document(str(doc.pk))
        ocr.assert_not_called()
        embed.assert_not_called()
        extract.assert_not_called()
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.Status.COMPLETED, doc.error_msg)
        self.assertEqual(doc.chunk_count, 0)
        rows = list(doc.tracker_rows.order_by('source_row'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].values['Description'], long_value)
        self.assertEqual(rows[1].values, {'Description': 'Middle', 'Value': '8'})
        self.assertEqual(rows[0].tracker.kb_id, self.root.pk)
        self.assertEqual((rows[0].source_sheet, rows[0].source_row), ('CSV', 2))
        with doc.file.open('rb') as source:
            self.assertIn(long_value.encode(), source.read())

    def test_all_sheets_source_positions_and_columns_beyond_header_are_preserved(self):
        doc = self.document(self.workbook([
            ('First', [[], ['Name', 'Name', 'Name (2)'], [], ['A', 'B', 'C', 'D']]),
            ('Second', [['Date', 'Amount'], ['2026-11-13', 12.5]]),
            ('Empty', []),
        ]), 'multi.xlsx')
        self.assertEqual(import_spreadsheet_document(doc), 2)
        rows = list(doc.tracker_rows.order_by('pk'))
        self.assertEqual((rows[0].source_sheet, rows[0].source_row), ('First', 4))
        self.assertEqual(rows[0].values,
                         {'Name': 'A', 'Name (2)': 'B', 'Name (2) (2)': 'C', 'Column D': 'D'})
        self.assertEqual((rows[1].source_sheet, rows[1].source_row), ('Second', 2))
        self.assertEqual(rows[1].values['Amount'], 12.5)
        doc.refresh_from_db()
        import json
        manifest = json.loads(doc.md_content)
        self.assertEqual(len(manifest['sheets']), 3)
        self.assertEqual(manifest['sheets'][0]['columns'][1]['original_header'], 'Name')
        self.assertEqual(manifest['sheets'][2]['row_count'], 0)

    def test_excel_dates_booleans_zero_and_formula_source_are_preserved(self):
        doc = self.document(self.workbook([('Values', [
            ['At', 'Amount', 'Enabled', 'Calculation'],
            [datetime(2026, 11, 13, 9, 30), 0, False, '=B2*2'],
        ])]), 'types.xlsx')
        import_spreadsheet_document(doc)
        values = doc.tracker_rows.get().values
        self.assertEqual(values['At'], '2026-11-13T09:30:00')
        self.assertEqual(values['Amount'], 0)
        self.assertIs(values['Enabled'], False)
        self.assertEqual(values['Calculation'], '=B2*2')

    def test_full_row_data_is_available_to_existing_database_aggregations(self):
        doc = self.document()
        import_spreadsheet_document(doc)
        result = TrackerQuery(lambda kb: True).lookup(
            kb_slug=self.root.slug, metrics=[{'op': 'sum', 'field': 'Value', 'as': 'total'}])
        self.assertEqual(result['total_matched'], 2)
        self.assertEqual(result['groups'][0]['metrics']['total'], 4)

    def test_retry_replaces_only_this_document_and_unions_schema(self):
        first = self.document()
        second = self.document(b'Name,Temperature\nZ,16\n', 'second.csv')
        import_spreadsheet_document(first)
        import_spreadsheet_document(second)
        import_spreadsheet_document(first)
        self.assertEqual(TrackerRow.objects.count(), 3)
        self.assertEqual(second.tracker_rows.get().values['Temperature'], '16')
        tracker = KbTracker.objects.get(kb=self.root)
        self.assertEqual([field['label'] for field in tracker.fields], ['Name', 'Value', 'Temperature'])
        self.assertEqual(tracker.schema_version, 2)

    def test_failed_replacement_rolls_back_rows_schema_and_document_state(self):
        doc = self.document()
        import_spreadsheet_document(doc)
        original_ids = list(doc.tracker_rows.order_by('pk').values_list('pk', flat=True))
        Path(doc.file.path).write_bytes(b'NewField\nnew value\n')
        with patch('kb.models.TrackerRow.objects.bulk_create', side_effect=RuntimeError('write failed')):
            with self.assertRaisesMessage(RuntimeError, 'write failed'):
                import_spreadsheet_document(doc)
        self.assertEqual(list(doc.tracker_rows.order_by('pk').values_list('pk', flat=True)), original_ids)
        tracker = KbTracker.objects.get(kb=self.root)
        self.assertNotIn('NewField', [field['label'] for field in tracker.fields])
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.Status.COMPLETED)

    def test_invalid_or_oversized_sheet_does_not_replace_previous_rows(self):
        doc = self.document()
        import_spreadsheet_document(doc)
        with patch('kb.spreadsheet_rows.MAX_ROWS', 2):
            with self.assertRaisesMessage(ValueError, '超过 100000 行'):
                import_spreadsheet_document(doc)
        self.assertEqual(doc.tracker_rows.count(), 2)
        Path(doc.file.path).write_bytes(b'Header only\n')
        with self.assertRaisesMessage(ValueError, '没有可查询的数据行'):
            import_spreadsheet_document(doc)
        self.assertEqual(doc.tracker_rows.count(), 2)

    def test_ai_extraction_never_rewrites_imported_rows(self):
        from kb.tracker import run_extraction
        doc = self.document()
        import_spreadsheet_document(doc)
        with patch('kb.tracker._llm_invoke') as invoke:
            self.assertFalse(run_extraction(str(doc.pk)))
        invoke.assert_not_called()
        self.assertEqual(doc.tracker_rows.count(), 2)

    def test_document_summary_uniqueness_and_multiple_source_rows_coexist(self):
        doc = self.document()
        tracker = KbTracker.objects.create(kb=self.root)
        TrackerRow.objects.create(tracker=tracker, document=doc)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    TrackerRow.objects.create(tracker=tracker, document=doc)
        for number in (2, 3):
            TrackerRow.objects.create(tracker=tracker, document=doc,
                                      source_sheet='Sheet', source_row=number)
        self.assertEqual(doc.tracker_rows.count(), 3)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TrackerRow.objects.create(tracker=tracker, document=doc,
                                          source_sheet='Sheet', source_row=2)

    def test_mode_is_explicit_and_unsupported_files_are_rejected(self):
        ordinary = KnowledgeBase.objects.create(name='Manual', slug='manual-default')
        self.assertFalse(ordinary.table_query)
        doc = self.document(child=ordinary)
        with self.assertRaisesMessage(ValueError, '未启用'):
            import_spreadsheet_document(doc)
        with self.assertRaisesMessage(ValueError, '仅支持'):
            read_spreadsheet_rows(doc.file.path, 'pdf')
        self.assertFalse(KbTracker.objects.exists())

    def test_index_rebuild_commands_skip_table_mode_before_model_preflight(self):
        doc = self.document()
        import_spreadsheet_document(doc)
        for command, state in (('reindex_clean', Document.Status.COMPLETED),
                               ('reindex_failed', Document.Status.FAILED)):
            Document.objects.filter(pk=doc.pk).update(status=state)
            with self.subTest(command=command), \
                 patch('kb.pipeline._embeddings') as embedding, \
                 patch(f'kb.management.commands.{command}.run_indexing') as index:
                io_output = StringIO()
                call_command(command, stdout=io_output)
            embedding.assert_not_called()
            index.assert_not_called()
            self.assertIn('没有', io_output.getvalue())
        self.assertEqual(doc.tracker_rows.count(), 2)

    def test_direct_document_indexing_cannot_embed_table_metadata(self):
        from kb.pipeline import run_indexing
        doc = self.document()
        with patch('kb.pipeline._embeddings') as embedding, patch('kb.pipeline.Chroma') as chroma:
            with self.assertRaisesMessage(ValueError, '不生成向量索引'):
                run_indexing('source', doc.kb.slug, doc.original_name, doc_id=doc.pk)
        embedding.assert_not_called()
        chroma.assert_not_called()
