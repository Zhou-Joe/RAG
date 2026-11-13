"""Normal EnGenie tools cannot bypass business-field selection through raw documents."""
from __future__ import annotations

from contextlib import ExitStack
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from kb.agent import _build_agent
from kb.models import Document, KbTracker, KnowledgeBase, TrackerRow


class HiddenFieldAgentToolTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('hidden-tools-reader', is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='Business Library', slug='business-tool-library',
            category='business', is_folder=True, active_fields=['Name', 'Count'])
        # Uploads and feed source documents inherit the parent's business category.
        self.child = KnowledgeBase.objects.create(name='Business Source', slug='business-source-doc',
            category='business', parent=self.kb)
        self.legacy_child = KnowledgeBase.objects.create(name='Legacy Source', slug='legacy-source-doc',
            category='manual', parent=self.kb)
        self.text = 'Visible Ride has Count 7. InternalNote is HIDDEN_SOURCE_SENTINEL.'
        self.doc = Document.objects.create(kb=self.child, original_name='Business Source.txt',
            status='completed', md_content=self.text)
        self.tracker = KbTracker.objects.create(kb=self.kb, enabled=True,
            fields=[{'label': label} for label in ('Name', 'Count', 'InternalNote')])
        TrackerRow.objects.create(tracker=self.tracker, status='done',
            values={'Name': 'Visible Ride', 'Count': 7, 'InternalNote': 'HIDDEN_SAMPLE_SENTINEL'})

    def tools(self, **extra):
        with patch('langchain.agents.create_agent', side_effect=lambda **kwargs: kwargs['tools']), \
             patch('kb.agent._get_llm', return_value=object()):
            functions = _build_agent('', 'hidden-field-tools', {}, 5, None,
                relationship_user_id=self.user.pk, **extra)
        return {function.__name__: function for function in functions}

    def raw_requests(self, slug):
        return [
            ('kb_search', {'query': 'Visible Ride', 'kb_slug': slug}),
            ('kb_fetch_doc', {'source': self.doc.original_name, 'kb_slug': slug}),
            ('kb_image_search', {'query': 'source table image', 'kb_slug': slug}),
            ('kb_graph_search', {'query': 'source data', 'kb_slug': slug}),
            ('kb_neighbor_chunk', {'source': self.doc.original_name,
                'anchor': 'Visible Ride has Count', 'kb_slug': slug}),
        ]

    def test_restricted_library_and_actual_document_children_redirect_all_raw_tools_to_selected_queries(self):
        tools = self.tools()
        backends = ['kb.retriever.search', 'kb.retriever.search_folder', 'kb.agent._image_search_core',
                    'kb.knowledge_graph.lookup_facts', 'kb.keyword_index.get_chunks']
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch(name, side_effect=AssertionError('Raw backend must not run')))
                     for name in backends]
            for kb in (self.kb, self.child, self.legacy_child):
                for tool, arguments in self.raw_requests(kb.slug):
                    with self.subTest(kb=kb.slug, tool=tool):
                        result = json.loads(tools[tool](**arguments))
                        self.assertEqual(result['query_mode'], 'table')
                        self.assertEqual(result['kb_slug'], self.kb.slug)
                        self.assertNotIn('HIDDEN_SOURCE_SENTINEL', json.dumps(result))
            for backend in mocks:
                backend.assert_not_called()

    def test_table_query_library_also_redirects_raw_document_tools(self):
        self.kb.category = 'manual'
        self.kb.table_query = True
        self.kb.save(update_fields=['category', 'table_query'])
        self.child.category = 'manual'
        self.child.save(update_fields=['category'])
        tools = self.tools()
        for tool, arguments in self.raw_requests(self.child.slug):
            with self.subTest(tool=tool):
                result = json.loads(tools[tool](**arguments))
                self.assertEqual(result['query_mode'], 'table')
                self.assertEqual(result['kb_slug'], self.kb.slug)

    def test_agent_tracker_preview_and_lookup_only_return_selected_schema_and_samples(self):
        tools = self.tools()
        for kb in (self.kb, self.child, self.legacy_child):
            with self.subTest(kb=kb.slug):
                preview = tools['tracker_preview'](kb_slug=kb.slug)
                self.assertNotIn('InternalNote', preview)
                self.assertNotIn('HIDDEN_SAMPLE_SENTINEL', preview)
                fields = json.loads(preview)['tables'][0]['fields']
                self.assertEqual([field['name'] for field in fields], ['Name', 'Count'])
                result = tools['tracker_lookup'](kb_slug=kb.slug)
                self.assertNotIn('InternalNote', result)
                self.assertNotIn('HIDDEN_SAMPLE_SENTINEL', result)
                self.assertEqual(json.loads(result)['rows'][0]['values'], {'Name': 'Visible Ride', 'Count': 7})

    def test_unhiding_restores_document_search_and_rehiding_blocks_previously_discovered_chunks(self):
        self.kb.active_fields = None
        self.kb.save(update_fields=['active_fields'])
        state = {}
        tools = self.tools(retrieval_state=state)
        rows = [{'doc_id': str(self.doc.pk), 'source': self.doc.original_name,
            'kb_slug': self.child.slug, 'chunk_id': 'source-chunk', 'type': 'text', 'text': self.text}]
        with patch('kb.retriever.search', return_value=rows) as retrieve, \
             patch('kb.provenance.annotate_results'):
            result = json.loads(tools['kb_search'](query='Visible Ride', kb_slug=self.child.slug))
        retrieve.assert_called_once()
        self.assertNotIn('query_mode', result)
        self.assertIn('HIDDEN_SOURCE_SENTINEL', json.dumps(result))
        candidate = result['candidates'][0]['candidate_id']
        self.kb.active_fields = ['Name', 'Count']
        self.kb.save(update_fields=['active_fields'])
        blocked = tools['kb_read_chunks']([candidate])
        self.assertNotIn('HIDDEN_SOURCE_SENTINEL', str(blocked))
        self.assertNotIn(candidate, state['session'].loaded)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.md_content, self.text)
