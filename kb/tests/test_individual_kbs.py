"""Owner-only isolation across KB management, retrieval and download surfaces."""
import json
from datetime import timedelta
from urllib.parse import urlsplit
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from kb import access
from kb.agent import _build_agent
from kb.feed_reports import download_link
from kb.kb_links import management_state, relationship_context
from kb.models import (Conversation, Document, FeedReportDelivery, FeedReportPlan,
                       KnowledgeBase, KnowledgeBaseLink, KnowledgeFeed, StructuredDataset)
from kb.structured_data import import_structured_dataset, lookup_related
from kb.views import _semantic_matches


class IndividualKnowledgeBaseTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('private-owner')
        self.peer = User.objects.create_user('private-peer')
        self.staff = User.objects.create_superuser('private-admin', 'admin@example.test', 'test-only')
        for user in (self.owner, self.peer):
            UserProfile.objects.update_or_create(user=user, defaults={'department': '机械'})
        self.private = KnowledgeBase.objects.create(name='Owner only', slug='individual-root',
            is_folder=True, visibility='individual', department='机械', created_by=self.owner)
        self.child = KnowledgeBase.objects.create(name='Private manual', slug='individual-child',
            parent=self.private, chunk_count=1, created_by=self.staff)
        self.doc = Document.objects.create(kb=self.child, original_name='private-secret.txt',
            status='completed', chunk_count=1, md_content='PRIVATE-123 private source content')
        self.shared = KnowledgeBase.objects.create(name='Shared manual', slug='shared-public',
            is_folder=True, created_by=self.staff)
        self.client.force_login(self.owner)

    def create(self, **extra):
        return self.client.post(reverse('kb:manage_list'), {'name': 'My new KB', **extra},
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def rename(self, kb=None, **extra):
        return self.client.post(reverse('kb:kb_rename', args=[(kb or self.private).slug]),
            {'name': 'Renamed KB', **extra}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_regular_user_creation_defaults_to_individual_and_rejects_shared(self):
        response = self.create()
        self.assertEqual(response.status_code, 200, response.content)
        kb = KnowledgeBase.objects.get(slug=response.json()['slug'])
        self.assertEqual((kb.visibility, kb.created_by), ('individual', self.owner))
        for level in ('general', 'department', 'invalid'):
            self.assertEqual(self.create(access_level=level, department='机械').status_code, 400)
        self.assertEqual(KnowledgeBase.objects.filter(created_by=self.owner).count(), 2)

    def test_individual_owner_access_survives_department_change_but_no_admin_bypass(self):
        self.owner.profile.department = '电气'
        self.owner.profile.save()
        for kb in (self.private, self.child):
            self.assertTrue(access.kb_accessible(self.owner, kb))
            self.assertTrue(access.can_manage_kb(self.owner, kb))
            for user in (self.peer, self.staff, AnonymousUser()):
                self.assertFalse(access.kb_accessible(user, kb))
                self.assertFalse(access.can_manage_kb(user, kb))
                self.assertFalse(KnowledgeBase.objects.filter(access.kb_q(user), pk=kb.pk).exists())
                self.assertFalse(access.managed_kbs(user).filter(pk=kb.pk).exists())
        self.assertFalse(access.can_manage_kb(self.owner, self.shared))

    def test_child_inherits_owner_scope_even_when_created_by_other_user(self):
        self.assertEqual(self.child.visibility, 'individual')
        self.assertEqual(self.child.department, self.private.department)
        self.assertEqual(self.child.created_by, self.staff)
        self.assertIn(self.child.slug, access.accessible_doc_slugs(self.owner))
        self.assertNotIn(self.child.slug, access.accessible_doc_slugs(self.staff))

    def test_deleted_owner_is_inaccessible_and_cannot_be_claimed_by_admin(self):
        self.owner.delete()
        self.private.refresh_from_db()
        self.assertIsNone(self.private.created_by)
        self.assertFalse(access.kb_accessible(self.staff, self.private))
        self.assertFalse(access.managed_kbs(self.staff).filter(pk=self.child.pk).exists())

    def test_owner_can_open_rename_and_upload_private_document(self):
        detail = reverse('kb:manage_detail', args=[self.private.slug])
        self.assertContains(self.client.get(detail), self.doc.original_name)
        self.assertEqual(self.rename().status_code, 200)
        upload = SimpleUploadedFile('my-note.txt', b'owner note', content_type='text/plain')
        with patch('kb.pipeline.process_document_async'):
            response = self.client.post(detail, {'file': upload}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertTrue(response.json()['ok'], response.content)
        document = Document.objects.get(pk=response.json()['doc_id'])
        self.assertEqual(document.kb.visibility, 'individual')
        self.assertEqual(document.kb.parent, self.private)
        self.assertFalse(access.kb_accessible(self.staff, document.kb))

    def test_other_users_and_admins_cannot_read_or_modify_private_endpoints(self):
        paths = [reverse('kb:manage_detail', args=[self.private.slug]),
                 reverse('kb:doc_status', args=[self.private.slug]),
                 reverse('kb:document_html', args=[self.doc.pk]),
                 reverse('kb:document_slices', args=[self.doc.pk]),
                 reverse('kb:tracker', args=[self.private.slug])]
        for user in (self.peer, self.staff):
            self.client.force_login(user)
            for path in paths:
                self.assertEqual(self.client.get(path).status_code, 404, path)
            self.assertEqual(self.rename().status_code, 404)
            self.assertEqual(self.client.post(reverse('kb:manage_delete', args=[self.private.slug])).status_code, 404)
            self.assertNotContains(self.client.get(reverse('kb:manage_list')), self.private.name)
        self.private.refresh_from_db()
        self.assertEqual(self.private.name, 'Owner only')

    def test_inspection_page_lists_private_documents_only_for_owner(self):
        path=reverse('kb:inspection')
        self.assertContains(self.client.get(path), self.doc.original_name)
        for user in (self.peer, self.staff):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(path), self.doc.original_name)
            self.assertEqual(self.client.get(path, {'doc': str(self.doc.pk)}).status_code, 404)

    def test_regular_owner_cannot_publish_or_modify_shared_kb(self):
        for level in ('department', 'general'):
            self.assertEqual(self.rename(access_level=level, department='机械').status_code, 400)
        self.assertEqual(self.rename(self.shared).status_code, 404)
        self.private.refresh_from_db()
        self.assertEqual((self.private.visibility, self.private.name), ('individual', 'Owner only'))

    def test_shared_creator_can_make_kb_private_with_children_atomically(self):
        child = KnowledgeBase.objects.create(name='Shared child', slug='shared-child', parent=self.shared)
        self.client.force_login(self.staff)
        response = self.rename(self.shared, access_level='individual')
        self.assertEqual(response.status_code, 200, response.content)
        child.refresh_from_db()
        self.assertEqual(child.visibility, 'individual')
        self.assertFalse(access.kb_accessible(self.owner, child))
        self.assertTrue(access.kb_accessible(self.staff, child))

    def test_non_creator_manager_cannot_privatize_shared_kb(self):
        self.shared.created_by = self.peer
        self.shared.save(update_fields=['created_by'])
        self.client.force_login(self.staff)
        self.assertEqual(self.rename(self.shared, access_level='individual').status_code, 400)
        self.shared.refresh_from_db()
        self.assertEqual((self.shared.name, self.shared.visibility), ('Shared manual', 'shared'))

    def test_renaming_shared_parent_preserves_stricter_child_department(self):
        child = KnowledgeBase.objects.create(name='Restricted child', slug='restricted-child',
            parent=self.shared, department='机密')
        self.client.force_login(self.staff)
        self.rename(self.shared)
        child.refresh_from_db()
        self.assertEqual(child.department, '机密')
        self.assertFalse(access.kb_accessible(self.owner, child))
        self.assertNotIn(child.slug, access.accessible_doc_slugs(self.owner))

    def test_structured_and_manual_search_only_returns_owner_private_content(self):
        csv = SimpleUploadedFile('private.csv', b'Part No,Part Name\nPRIVATE-123,Private bearing\n')
        import_structured_dataset(csv, StructuredDataset.Kind.PARTS, self.owner, kb=self.private)
        owner = lookup_related('PRIVATE-123', user=self.owner)
        self.assertEqual(len(owner['records']), 1)
        self.assertEqual(len(owner['manual_matches']), 1)
        for user in (self.peer, self.staff, None):
            result = lookup_related('PRIVATE-123', user=user)
            self.assertEqual(result['records'], [])
            self.assertEqual(result['manual_matches'], [])

    def test_semantic_search_never_passes_private_slug_for_other_users(self):
        for user, expected in ((self.owner, True), (self.peer, False), (self.staff, False)):
            with patch('kb.retriever.search_folder', return_value=[]) as search:
                _semantic_matches(user, 'private')
            passed = search.call_args.args[0] if search.called else []
            self.assertEqual(self.child.slug in passed, expected)

    def test_agent_catalog_and_tools_enforce_current_owner_even_with_supplied_slug(self):
        for user, expected in ((self.owner, True), (self.peer, False), (self.staff, False)):
            with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'):
                _build_agent(self.private.slug, 'private-tool-test', {}, 5, None,
                    relationship_user_id=user.pk,
                    allowed_kb_slugs=[self.private.slug, self.child.slug, self.shared.slug])
            tools = {tool.__name__: tool for tool in create.call_args.kwargs['tools']}
            self.assertEqual(self.private.name in tools['list_knowledge_bases'](), expected)
            if not expected:
                self.assertIn('不存在', tools['kb_fetch_doc'](kb_slug=self.child.slug))

    def test_internal_agent_without_user_cannot_list_individual_library_by_supplied_slug(self):
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'):
            _build_agent(self.private.slug, 'anonymous-tool-test', {}, 5, None,
                allowed_kb_slugs=[self.private.slug, self.child.slug])
        tools={tool.__name__: tool for tool in create.call_args.kwargs['tools']}
        self.assertNotIn(self.private.name, tools['list_knowledge_bases']())
        self.assertNotIn(self.child.name, tools['list_knowledge_bases']())
        self.assertIn('不存在', tools['kb_fetch_doc'](kb_slug=self.child.slug))

    def test_graph_document_selectors_and_links_never_expose_private_kb(self):
        from kb.hierarchy_views import libraries
        from kb.graph_views import _libraries
        from vehicle_tracker.documents import visible_documents
        left, right = sorted((self.private, self.shared), key=lambda kb: kb.pk)
        KnowledgeBaseLink.objects.create(left=left, right=right, summary='Secret relationship')
        for user in (self.peer, self.staff):
            self.assertFalse(libraries(user).filter(pk=self.child.pk).exists())
            self.assertFalse(_libraries(user).filter(pk=self.private.pk).exists())
            self.assertFalse(visible_documents(user).filter(pk=self.doc.pk).exists())
            state = management_state(user)
            self.assertNotIn(self.private.slug, str(state))
            hints = relationship_context([self.shared], lambda kb: access.kb_accessible(user, kb))
            self.assertEqual(hints['links'], [])

    def test_admin_queryset_and_choices_do_not_expose_another_owners_library(self):
        request = RequestFactory().get('/admin/')
        request.user = self.staff
        for model, key in ((KnowledgeBase, self.private.pk), (Document, self.doc.pk)):
            self.assertFalse(admin.site._registry[model].get_queryset(request).filter(pk=key).exists())
        choices = admin.site._registry[Document].formfield_for_foreignkey(Document._meta.get_field('kb'), request)
        self.assertFalse(choices.queryset.filter(pk=self.child.pk).exists())

    def test_retrieval_evaluation_excludes_private_sources_for_staff_and_internal_calls(self):
        from kb.eval import _doc_libs
        for user, expected in ((self.owner, True), (self.staff, False), (None, False)):
            self.assertEqual(self.child.slug in [kb.slug for kb in _doc_libs(user)], expected)

    def test_evaluation_does_not_reuse_cache_after_source_access_changes(self):
        self.client.force_login(self.staff)
        session = self.client.session
        session['eval_report'] = {'summary': {}, 'questions': [{'rows': [{'text': 'cached private secret'}]}]}
        session['eval_report_scope'] = [self.shared.slug, self.private.slug]
        session.save()
        self.assertNotContains(self.client.get(reverse('kb:eval_panel')), 'cached private secret')

    def test_private_feed_defaults_and_settings_are_owner_only(self):
        response = self.client.post(reverse('kb:feed_action'), json.dumps({'action': 'create',
            'kb_name': 'Private automatic library', 'name': 'Private feed', 'mode': 'push'}),
            content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200, response.content)
        feed = KnowledgeFeed.objects.get(pk=response.json()['feed']['id'])
        self.assertEqual(feed.kb.visibility, 'individual')
        self.assertTrue(access.can_edit_feed(self.owner, feed))
        for user in (self.peer, self.staff):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('kb:feed_state')).json()['feeds'], [])
            self.assertFalse(access.can_edit_feed(user, feed))

    def test_private_report_download_requires_owner_even_with_valid_link(self):
        feed = KnowledgeFeed.objects.create(kb=self.private, name='Private feed', created_by=self.owner)
        plan = FeedReportPlan.objects.create(feed=feed, created_by=self.owner)
        delivery = FeedReportDelivery.objects.create(plan=plan, request_key='test-private', content=b'excel-private',
            expires_at=timezone.now()+timedelta(days=1))
        path = urlsplit(download_link(delivery, 'https://example.test')).path
        self.assertEqual(self.client.get(path).content, b'excel-private')
        for user in (self.peer, self.staff):
            self.client.force_login(user)
            self.assertEqual(self.client.get(path).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_main_page_has_home_links_and_only_bubble_connect_button(self):
        business = KnowledgeBase.objects.create(name='Private data', slug='private-data', is_folder=True,
            category='business', visibility='individual', created_by=self.owner)
        html = self.client.get(reverse('kb:manage_list')).content.decode()
        for kb in (self.private, business):
            self.assertIn('href="'+reverse('kb:manage_detail', args=[kb.slug])+'">知识库主页</a>', html)
        self.assertEqual(html.count('data-kb-connect'), 1)
        self.assertIn('data-bubble-only hidden', html)
