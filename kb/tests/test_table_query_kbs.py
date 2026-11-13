"""Explicit table-query creation and querying retain ordinary library behavior."""
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from kb.models import Document, KbTracker, KnowledgeBase, TrackerRow
from kb.spreadsheet_rows import import_spreadsheet_document
from kb.tracker_query import TrackerQuery


class TableQueryLibraryTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        media = override_settings(MEDIA_ROOT=temporary.name)
        media.enable()
        self.addCleanup(media.disable)
        self.owner = get_user_model().objects.create_user('table-query-owner', is_staff=True)
        self.reader = get_user_model().objects.create_user('table-query-reader')
        self.client.force_login(self.owner)

    def create(self, **extra):
        response = self.client.post(reverse('kb:manage_list'), {
            'action': 'create_kb', 'name': 'Monthly Operations',
            'access_level': 'individual', **extra,
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['ok'], response.content)
        return KnowledgeBase.objects.get(slug=response.json()['slug'])

    def import_csv(self, library, content='Date,Equipment,Value\n2026-01-01,A,0\n2026-02-01,A,5\n2026-02-02,B,7\n'):
        with patch('kb.pipeline.process_document_async'):
            response = self.client.post(reverse('kb:manage_detail', args=[library.slug]), {
                'file': SimpleUploadedFile('operations.csv', content.encode('utf-8')),
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertTrue(response.json()['ok'], response.content)
        document = Document.objects.get(pk=response.json()['doc_id'])
        import_spreadsheet_document(document)
        return document

    def test_switch_defaults_off_and_stays_in_manual_library_category(self):
        ordinary = self.create()
        table = self.create(table_query='on')
        self.assertFalse(ordinary.table_query)
        self.assertTrue(table.table_query)
        self.assertEqual(table.category, 'manual')

    def test_upload_inherits_mode_and_queries_every_data_row_with_provenance(self):
        library = self.create(table_query='on')
        document = self.import_csv(library)
        self.assertTrue(document.kb.table_query)
        self.assertEqual(document.kb.parent_id, library.pk)
        service = TrackerQuery(lambda kb: kb.pk == library.pk)
        result = service.lookup(kb_slug=library.slug, columns=['Date', 'Value'],
            filters=[{'field': 'Date', 'op': 'between', 'type': 'date',
                      'value': ['2026-02-01', '2026-02-28']}])
        self.assertEqual(result['total_matched'], 2)
        self.assertEqual({row['source_row'] for row in result['rows']}, {3, 4})
        self.assertEqual({row['source_sheet'] for row in result['rows']}, {'CSV'})
        summary = service.lookup(kb_slug=library.slug,
            metrics=[{'op': 'sum', 'field': 'Value', 'as': 'Total'}],
            group_by=[{'field': 'Date', 'period': 'month'}])
        self.assertEqual(summary['total_matched'], 3)
        self.assertEqual(len(summary['groups']), 2)
        preview = service.preview(kb_slug=library.slug)
        self.assertEqual(preview['tables'][0]['query_mode'], 'table')

    def test_table_mode_rejects_non_spreadsheets_without_an_orphan_child(self):
        library = self.create(table_query='on')
        before = KnowledgeBase.objects.count()
        with patch('kb.pipeline.process_document_async') as process:
            response = self.client.post(reverse('kb:manage_detail', args=[library.slug]), {
                'file': SimpleUploadedFile('manual.pdf', b'PDF'),
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertFalse(response.json()['ok'])
        self.assertEqual(KnowledgeBase.objects.count(), before)
        self.assertFalse(Document.objects.exists())
        process.assert_not_called()

    def test_ai_controls_cannot_disable_or_rewrite_imported_rows(self):
        library = self.create(table_query='on')
        document = self.import_csv(library)
        tracker = KbTracker.objects.get(kb=library)
        row = tracker.rows.first()
        for action in ('disable', 'config_save', 'backfill', 'row_retry'):
            with self.subTest(action=action):
                response = self.client.post(reverse('kb:tracker', args=[library.slug]), {
                    'action': action, 'row_id': row.pk, 'fields_text': 'changed',
                }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                self.assertEqual(response.status_code, 409)
        tracker.refresh_from_db()
        self.assertTrue(tracker.enabled)
        self.assertEqual(TrackerRow.objects.filter(document=document, status='done').count(), 3)

    def test_full_long_cell_is_saved_and_cannot_be_overwritten_by_preview(self):
        library = self.create(table_query='on')
        long_text = 'x' * 5000
        self.import_csv(library, 'Name,Notes\nA,' + long_text + '\n')
        row = TrackerRow.objects.get(tracker__kb=library)
        response = self.client.post(reverse('kb:tracker', args=[library.slug]), {
            'action': 'cell_edit', 'row_id': row.pk, 'field': 'Notes', 'value': 'short preview',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 409)
        row.refresh_from_db()
        self.assertEqual(row.values['Notes'], long_text)

    def test_table_rows_obey_private_library_access(self):
        library = self.create(table_query='on')
        self.import_csv(library)
        self.client.force_login(self.reader)
        response = self.client.get(reverse('kb:tracker_kb_data', args=[library.slug]))
        self.assertEqual(response.status_code, 404)

    def test_table_export_has_sheet_and_original_row(self):
        library = self.create(table_query='on')
        self.import_csv(library)
        response = self.client.get(reverse('kb:tracker', args=[library.slug]), {'xlsx': '1'})
        self.assertEqual(response.status_code, 200)
        book = load_workbook(BytesIO(response.content), read_only=True)
        try:
            rows = list(book.active.values)
        finally:
            book.close()
        self.assertEqual(rows[0][1:3], ('工作表', '原始行号'))
        self.assertEqual({row[2] for row in rows[1:]}, {2, 3, 4})

    def test_table_mode_allows_active_field_selection(self):
        library = self.create(table_query='on')
        self.import_csv(library)
        response = self.client.post(reverse('kb:kb_rename', args=[library.slug]), {
            'name': library.name, 'active_fields_mode': 'selected', 'active_field': ['Value'],
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200, response.content)
        library.refresh_from_db()
        self.assertEqual(library.active_fields, ['Value'])

    def test_table_export_refuses_to_silently_shorten_long_cells(self):
        library = self.create(table_query='on')
        self.import_csv(library, 'Notes\n' + 'x' * 40000 + '\n')
        response = self.client.get(reverse('kb:tracker', args=[library.slug]), {'xlsx': '1'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('32767', response.json()['message'])
        self.assertEqual(len(TrackerRow.objects.get(tracker__kb=library).values['Notes']), 40000)

    def test_table_uses_primary_record_browser_and_export_preserves_zero(self):
        library = self.create(table_query='on')
        self.import_csv(library)
        url = reverse('kb:tracker', args=[library.slug])
        self.assertRedirects(self.client.get(url), reverse('kb:manage_detail', args=[library.slug]))
        response = self.client.get(url, {'xlsx': '1'})
        book = load_workbook(BytesIO(response.content), read_only=True)
        try:
            rows = list(book.active.values)
        finally:
            book.close()
        value_index = rows[0].index('Value')
        self.assertIn('0', [row[value_index] for row in rows[1:]])

