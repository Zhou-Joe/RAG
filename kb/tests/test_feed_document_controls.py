"""Feed-owned source files cannot be mutated outside their feed versioning flow."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from kb.models import Document, FeedRecord, KnowledgeBase, KnowledgeFeed


class FeedDocumentControlTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('feed-document-manager', is_staff=True)
        self.client.force_login(self.owner)
        self.folder = KnowledgeBase.objects.create(
            name='News feed', slug='feed-document-controls', is_folder=True,
            category='business', created_by=self.owner,
        )
        self.child = KnowledgeBase.objects.create(
            name='Article', slug='feed-document-article', parent=self.folder,
            category='business', created_by=self.owner,
        )
        self.feed = KnowledgeFeed.objects.create(
            kb=self.folder, name=self.folder.name, created_by=self.owner,
            adapter_spec={'ingest_mode': 'documents'},
        )
        self.current = Document.objects.create(
            kb=self.child, original_name='article.md', file_type='md',
            status=Document.Status.COMPLETED, md_content='Article text',
        )
        self.failed = Document.objects.create(
            kb=self.child, original_name='new-article.md', file_type='md',
            status=Document.Status.FAILED,
        )
        FeedRecord.objects.create(
            feed=self.feed, kb=self.child, external_id='article-1',
            document=self.current, pending_document=self.failed,
        )
        self.manual_child = KnowledgeBase.objects.create(
            name='Manual', slug='feed-document-manual', parent=self.folder,
            category='business', created_by=self.owner,
        )
        self.manual = Document.objects.create(
            kb=self.manual_child, original_name='manual.md', file_type='md',
            status=Document.Status.COMPLETED, md_content='Manual text',
        )

    def url(self, action, doc):
        return reverse('kb:' + action, args=[self.folder.slug, doc.pk])

    def test_generated_document_is_readable_but_mutations_require_feed(self):
        page = self.client.get(reverse('kb:manage_detail', args=[self.folder.slug]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'AI 提炼源文档')
        status = self.client.get(reverse('kb:doc_status', args=[self.folder.slug])).json()
        by_id = {row['id']: row for row in status['docs']}
        self.assertTrue(by_id[str(self.current.pk)]['feed_owned'])
        self.assertTrue(by_id[str(self.failed.pk)]['feed_owned'])
        self.assertFalse(by_id[str(self.manual.pk)]['feed_owned'])

        self.assertEqual(self.client.post(self.url('doc_retry', self.failed)).status_code, 409)
        self.assertEqual(self.client.post(
            self.url('doc_delete', self.current), HTTP_X_REQUESTED_WITH='XMLHttpRequest').status_code, 409)
        self.assertEqual(self.client.post(
            self.url('doc_desc_update', self.current), {'description': 'manual edit'}).status_code, 409)
        self.assertEqual(self.client.get(self.url('doc_delete', self.current)).status_code, 405)
        self.assertTrue(Document.objects.filter(pk=self.current.pk).exists())
        self.failed.refresh_from_db()
        self.assertEqual(self.failed.status, Document.Status.FAILED)

        allowed = self.client.post(self.url('doc_desc_update', self.manual), {'description': 'manual edit'})
        self.assertEqual(allowed.status_code, 200)
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.description, 'manual edit')
