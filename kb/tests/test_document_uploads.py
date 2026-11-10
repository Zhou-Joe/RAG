"""Uploads into a selected KB retain spreadsheet, Word, and mail files as documents."""

from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from kb.models import Document, KnowledgeBase, StructuredDataset


class DocumentUploadTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = override_settings(MEDIA_ROOT=temporary.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.manager = get_user_model().objects.create_user('document-uploader', is_staff=True)
        self.reader = get_user_model().objects.create_user('document-reader')
        self.folder = KnowledgeBase.objects.create(
            name='Existing library', slug='existing-upload-library', is_folder=True,
            created_by=self.manager,
        )
        self.url = reverse('kb:manage_detail', args=[self.folder.slug])
        self.client.force_login(self.manager)

    def upload(self, name, payload, *, url=None):
        with patch('kb.pipeline.process_document_async') as process:
            response = self.client.post(
                url or self.url, {'file': SimpleUploadedFile(name, payload)},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            )
        return response, process

    def assert_stored_in_folder(self, name, payload):
        top_level_count = KnowledgeBase.objects.filter(parent__isnull=True).count()
        dataset_count = StructuredDataset.objects.count()
        response, process = self.upload(name, payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['ok'], response.content)
        self.assertIn('doc_id', response.json())
        doc = Document.objects.get(pk=response.json()['doc_id'])
        self.assertEqual(doc.original_name, name)
        self.assertEqual(doc.kb.parent_id, self.folder.pk)
        self.assertEqual(doc.status, Document.Status.PENDING)
        with doc.file.open('rb') as saved:
            self.assertEqual(saved.read(), payload)
        self.assertEqual(KnowledgeBase.objects.filter(parent__isnull=True).count(), top_level_count)
        self.assertEqual(StructuredDataset.objects.count(), dataset_count)
        process.assert_called_once_with(doc.pk)

    def test_csv_is_a_document_in_selected_library(self):
        self.assert_stored_in_folder('operating-hours.csv',
                                     '景点,开园时间\n加勒比海盗,08:30\n'.encode('utf-8-sig'))

    def test_excel_is_a_document_in_selected_library(self):
        workbook = Workbook()
        workbook.active.append(['Attraction', 'Operating hours'])
        workbook.active.append(['Pirates', 12.5])
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        self.assert_stored_in_folder('operating-hours.xlsx', stream.getvalue())

    def test_word_and_email_uploads_retain_original_files(self):
        # Parsing is queued separately; this test verifies routing and storage.
        for name, payload in (
            ('maintenance.docx', b'word-document-fixture'),
            ('maintenance.eml', b'Subject: Maintenance\r\n\r\nInspection completed.'),
            ('maintenance.msg', b'outlook-message-fixture'),
        ):
            with self.subTest(name=name):
                self.assert_stored_in_folder(name, payload)

    def test_legacy_nonfolder_library_keeps_file_directly(self):
        library = KnowledgeBase.objects.create(
            name='Standalone library', slug='standalone-upload-library',
            created_by=self.manager,
        )
        before = KnowledgeBase.objects.count()
        response, process = self.upload(
            'table.csv', b'Name,Value\nA,4\n',
            url=reverse('kb:manage_detail', args=[library.slug]),
        )
        self.assertEqual(response.status_code, 200, response.content)
        doc = Document.objects.get(pk=response.json()['doc_id'])
        self.assertEqual(doc.kb_id, library.pk)
        self.assertEqual(KnowledgeBase.objects.count(), before)
        self.assertFalse(StructuredDataset.objects.exists())
        process.assert_called_once_with(doc.pk)

    def test_upload_center_selected_library_uses_same_document_route(self):
        with patch('kb.pipeline.process_document_async') as process:
            response = self.client.post(reverse('kb:manage_list'), {
                'action': 'upload_manual', 'kb_slug': self.folder.slug,
                'file': SimpleUploadedFile('selected-table.csv', b'Name,Value\nA,8\n'),
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 302)
        doc = Document.objects.get(original_name='selected-table.csv')
        self.assertEqual(doc.kb.parent_id, self.folder.pk)
        self.assertEqual(KnowledgeBase.objects.filter(parent__isnull=True).count(), 1)
        self.assertFalse(StructuredDataset.objects.exists())
        process.assert_called_once_with(doc.pk)

    def test_read_only_upload_is_rejected_without_new_file_or_library(self):
        self.client.force_login(self.reader)
        before = KnowledgeBase.objects.count()
        response, process = self.upload('private.xlsx', b'excel-fixture')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()['ok'])
        self.assertFalse(Document.objects.exists())
        self.assertEqual(KnowledgeBase.objects.count(), before)
        process.assert_not_called()

    def test_unsupported_upload_does_not_leave_orphan_child_library(self):
        before = KnowledgeBase.objects.count()
        response, process = self.upload('program.exe', b'not-a-document')
        self.assertIn(response.status_code, (200, 400))
        self.assertFalse(response.json()['ok'])
        self.assertFalse(Document.objects.exists())
        self.assertEqual(KnowledgeBase.objects.count(), before)
        process.assert_not_called()
