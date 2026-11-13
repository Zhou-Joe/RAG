"""Global-admin read queries agree with shared-library endpoint permissions."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile, assign_departments
from kb import access
from kb.agent import _build_agent
from kb.models import DEPARTMENT_GENERAL, Document, KnowledgeBase
from kb.views import _semantic_matches


class GlobalAdminKnowledgeBaseAccessTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user('all-library-staff', is_staff=True)
        self.superuser = User.objects.create_user('all-library-superuser', is_superuser=True)
        for user in (self.admin, self.superuser):
            assign_departments(user, [DEPARTMENT_GENERAL])
        self.department_admin = User.objects.create_user('rse-library-manager')
        UserProfile.objects.create(user=self.department_admin, department='RSE',
                                   role=UserProfile.Role.DEPT_ADMIN)
        self.reader = User.objects.create_user('general-library-reader')
        assign_departments(self.reader, [DEPARTMENT_GENERAL])
        self.owner = User.objects.create_user('private-library-owner')
        self.roots = []
        self.children = []
        self.documents = []
        for index, department in enumerate((DEPARTMENT_GENERAL, 'RSE', 'AFE')):
            root = KnowledgeBase.objects.create(name=f'{department} Shared Library', slug=f'admin-scope-{index}',
                is_folder=True, department=department, created_by=self.department_admin)
            child = KnowledgeBase.objects.create(name=f'{department} Document', slug=f'admin-scope-child-{index}',
                parent=root, department=department, chunk_count=1, created_by=self.department_admin)
            document = Document.objects.create(kb=child, original_name=f'{department}.txt',
                status='completed', chunk_count=1, md_content=f'{department} shared document content')
            self.roots.append(root)
            self.children.append(child)
            self.documents.append(document)
        self.private = KnowledgeBase.objects.create(name='Owner Private', slug='admin-scope-private',
            is_folder=True, visibility='individual', department='AFE', created_by=self.owner)
        self.private_child = KnowledgeBase.objects.create(name='Private Document', slug='admin-scope-private-child',
            parent=self.private, department='AFE', created_by=self.owner, chunk_count=1)
        Document.objects.create(kb=self.private_child, original_name='private.txt', status='completed', chunk_count=1)

    def test_staff_and_superuser_default_read_queries_include_all_shared_departments(self):
        expected = {node.slug for node in self.roots + self.children}
        for user in (self.admin, self.superuser):
            with self.subTest(user=user.username):
                self.assertEqual(access.accessible_doc_slugs(user), expected)
                self.assertEqual(set(access.accessible_kbs(user)), set(self.roots))
                self.assertEqual(set(access.accessible_docs(user)), set(self.documents))
                for node in self.roots + self.children:
                    self.assertTrue(access.kb_accessible(user, node))

    def test_explicit_department_view_and_department_admin_remain_scoped(self):
        general = {self.roots[0].slug, self.children[0].slug}
        self.assertEqual(set(KnowledgeBase.objects.filter(access.kb_q(self.admin, admin_all=False))
                             .values_list('slug', flat=True)), general)
        expected = general | {self.roots[1].slug, self.children[1].slug}
        self.assertEqual(access.accessible_doc_slugs(self.department_admin), expected)
        self.assertEqual(access.accessible_doc_slugs(self.reader), general)

    def test_admin_library_and_document_endpoints_can_read_department_sources(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse('kb:manage_list'))
        for root, document in zip(self.roots, self.documents):
            self.assertContains(page, root.name)
            self.assertContains(self.client.get(reverse('kb:manage_detail', args=[root.slug])), document.original_name)
            self.assertEqual(self.client.get(reverse('kb:document_html', args=[document.pk])).status_code, 200)

    def test_global_read_fix_does_not_open_other_users_private_libraries(self):
        for user in (self.admin, self.superuser):
            with self.subTest(user=user.username):
                self.assertFalse(access.kb_accessible(user, self.private))
                self.assertNotIn(self.private_child.slug, access.accessible_doc_slugs(user))
                self.client.force_login(user)
                self.assertEqual(self.client.get(reverse('kb:manage_detail', args=[self.private.slug])).status_code, 404)
        self.assertIn(self.private_child.slug, access.accessible_doc_slugs(self.owner))

    def test_semantic_search_receives_all_shared_department_slugs(self):
        with patch('kb.retriever.search_folder', return_value=[]) as search:
            _semantic_matches(self.admin, 'inspection')
        self.assertEqual(set(search.call_args.args[0]), {child.slug for child in self.children})

    def test_engenie_catalog_uses_the_global_admin_read_allowlist(self):
        allowed = sorted(access.accessible_doc_slugs(self.admin))
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'):
            _build_agent('', 'admin-catalog', {}, 5, None, allowed_kb_slugs=allowed,
                         relationship_user_id=self.admin.pk)
        tools = {getattr(tool, 'name', getattr(tool, '__name__', '')): tool
                 for tool in create.call_args.kwargs['tools']}
        catalog = tools['list_knowledge_bases']()
        for root in self.roots:
            self.assertIn(root.slug, catalog)
        self.assertNotIn(self.private.slug, catalog)

