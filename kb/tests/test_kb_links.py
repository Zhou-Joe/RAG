from __future__ import annotations

"""知识库关联的管理权限、预算与跨库取证回归。"""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from accounts.models import UserProfile
from kb.agent import _build_agent
from kb.kb_links import RelationshipScope, attach_relationships, relationship_context
from kb.models import Conversation, Document, KbTracker, KnowledgeBase, KnowledgeBaseLink, Message, TrackerRow
from kb.rag_selection import MAX_RESPONSE_CHARS, RagSelection
from kb.tracker_query import RESPONSE_CHARS, TrackerQuery, validate_snapshot


def connect(a, b, summary='通过设备编号关联，手册解释参数，台账提供测量值。'):
    """创建规范顺序的无向边。"""
    left, right = sorted([a, b], key=lambda kb: kb.pk)
    return KnowledgeBaseLink.objects.create(left=left, right=right, summary=summary)


class LinksFixture:
    def setUp(self):
        self.user = get_user_model().objects.create_user('links-admin', is_staff=True)
        self.manual = KnowledgeBase.objects.create(name='设备维护手册', slug='links-manual', is_folder=True)
        self.data = KnowledgeBase.objects.create(name='运行台账', slug='links-data', is_folder=True, category='business')
        self.other = KnowledgeBase.objects.create(name='维修记录', slug='links-other', is_folder=True)
        self.child = KnowledgeBase.objects.create(name='手册.pdf', slug='links-document', parent=self.manual)
        self.doc = Document.objects.create(kb=self.child, original_name='手册.pdf', status='completed', md_content='设备限值原文')
        self.row = {'doc_id': str(self.doc.pk), 'source': self.doc.original_name, 'chunk_id': 'links-chunk',
                    'page_label': '第1页', 'text': '设备限值为30。', 'section': '限值'}
        self.tracker = KbTracker.objects.create(kb=self.data, fields=[{'label': '温度'}], enabled=True)
        TrackerRow.objects.create(tracker=self.tracker, status='done', values={'温度': 29})


class KnowledgeLinkViewsTests(LinksFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse('kb:kb_connections')

    def post(self, body):
        return self.client.post(self.url, json.dumps(body), content_type='application/json')

    def test_create_reverse_pair_updates_summary_and_one_to_many_is_allowed(self):
        first = self.post({'action': 'save', 'left': self.manual.slug, 'right': self.data.slug, 'summary': '关联设备编号'})
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()['links'][0]['summary'], '关联设备编号')
        creator = KnowledgeBaseLink.objects.get().created_by_id
        self.post({'action': 'save', 'left': self.data.slug, 'right': self.manual.slug, 'summary': '更新关系说明'})
        self.assertEqual(KnowledgeBaseLink.objects.count(), 1)
        self.assertEqual(KnowledgeBaseLink.objects.get().summary, '更新关系说明')
        self.assertEqual(KnowledgeBaseLink.objects.get().created_by_id, creator)
        self.post({'action': 'save', 'left': self.manual.slug, 'right': self.other.slug, 'summary': '检修明细'})
        self.assertEqual(len(self.client.get(self.url).json()['links']), 2)
        self.assertEqual({n['category'] for n in first.json()['nodes']}, {'manual', 'business'})

    def test_long_invalid_self_link_and_child_connections_are_rejected(self):
        for extra in ({'summary': None}, {'summary': '文'*1001},
                      {'left': self.data.slug}, {'right': 7}):
            result = self.post({'action': 'save', 'left': self.manual.slug, 'right': self.data.slug, 'summary': '说明', **extra})
            self.assertEqual(result.status_code, 400, result.content)
        self.assertEqual(self.post({'action': 'save', 'left': self.child.slug, 'right': self.data.slug, 'summary': '说明'}).status_code, 404)
        self.assertEqual(KnowledgeBaseLink.objects.count(), 0)
        self.assertEqual(self.client.post(self.url, '[]', content_type='application/json').status_code, 400)

    def test_comments_are_optional_and_blank_is_not_replaced_with_invented_semantics(self):
        result = self.post({'action': 'save', 'left': self.manual.slug, 'right': self.data.slug})
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(KnowledgeBaseLink.objects.get().summary, '')
        self.assertEqual(relationship_context([self.manual], lambda kb: True)['links'][0]['summary'], '')
        self.post({'action': 'save', 'left': self.data.slug, 'right': self.manual.slug, 'summary': '   '})
        self.assertEqual(KnowledgeBaseLink.objects.count(), 1)
        self.assertEqual(KnowledgeBaseLink.objects.get().summary, '')

    def test_canvas_counts_completed_documents_and_records_across_child_libraries(self):
        Document.objects.create(kb=self.manual, original_name='第二份手册.pdf', status='completed')
        Document.objects.create(kb=self.child, original_name='未完成.pdf', status='pending')
        child = KnowledgeBase.objects.create(name='子数据', slug='links-child-data', parent=self.data, category='business')
        tracker = KbTracker.objects.create(kb=child, fields=[{'label': '温度'}])
        for status in ('done', 'done', 'pending'):
            TrackerRow.objects.create(tracker=tracker, status=status, values={'温度': 12})
        nodes = {node['slug']: node for node in self.client.get(self.url).json()['nodes']}
        self.assertEqual(nodes[self.manual.slug]['document_count'], 2)
        self.assertEqual(nodes[self.data.slug]['record_count'], 3)
        self.assertNotIn(child.slug, nodes)

    def test_database_enforces_distinct_canonical_unique_pair(self):
        link = connect(self.manual, self.data)
        for left, right in ((link.left, link.right), (link.right, link.left), (self.manual, self.manual)):
            with self.assertRaises(IntegrityError), transaction.atomic():
                KnowledgeBaseLink.objects.create(left=left, right=right, summary='重复或错误的边')

    def test_delete_only_removes_edge_and_kb_delete_cascades_edges(self):
        link = connect(self.manual, self.data)
        self.assertEqual(self.post({'action': 'delete', 'id': link.pk}).status_code, 200)
        self.assertTrue(KnowledgeBase.objects.filter(pk=self.manual.pk).exists())
        connect(self.manual, self.data)
        self.data.delete()
        self.assertFalse(KnowledgeBaseLink.objects.exists())
        self.assertTrue(KnowledgeBase.objects.filter(pk=self.manual.pk).exists())

    def test_department_manager_cannot_read_write_or_delete_foreign_connections(self):
        manager = get_user_model().objects.create_user('link-department')
        UserProfile.objects.update_or_create(user=manager, defaults={'department': '机械', 'role': UserProfile.Role.DEPT_ADMIN})
        self.manual.department = '机械'; self.manual.save(update_fields=['department'])
        self.data.department = '电气'; self.data.save(update_fields=['department'])
        link = connect(self.manual, self.data, 'foreign private summary')
        self.client.force_login(manager)
        body = self.client.get(self.url).json()
        self.assertEqual([n['slug'] for n in body['nodes']], [self.manual.slug])
        self.assertEqual(body['links'], [])
        self.assertNotIn('foreign private', str(body))
        self.assertEqual(self.post({'action': 'save', 'left': self.manual.slug, 'right': self.data.slug, 'summary': '越权'}).status_code, 404)
        self.assertEqual(self.post({'action': 'delete', 'id': link.pk}).status_code, 404)

    def test_authentication_csrf_and_management_view_controls(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        reader = get_user_model().objects.create_user('link-reader')
        self.client.force_login(reader)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.client.get(self.url).json()['nodes'],[])
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.user)
        self.assertEqual(csrf.post(self.url, '{}', content_type='application/json').status_code, 403)
        self.client.force_login(self.user)
        page = self.client.get(reverse('kb:manage_list'))
        self.assertContains(page, 'data-kb-view="category" aria-pressed="true"')
        self.assertContains(page, 'data-kb-view="cluster"')
        self.assertContains(page, 'id="klSummary" maxlength="1000"')
        self.assertNotContains(page, 'id="klSummary" required')
        self.assertContains(page, 'id="klCanvas"')


class KnowledgeLinkNavigationTests(LinksFixture, TestCase):
    def test_direct_links_reciprocal_child_inheritance_and_no_transitive_expansion(self):
        connect(self.manual, self.data)
        connect(self.data, self.other)
        result = relationship_context([self.child], lambda kb: True)
        self.assertEqual([link['kb_slug'] for link in result['links']], [self.data.slug])
        self.assertEqual(result['links'][0]['from_kb_slug'], self.manual.slug)
        self.assertTrue(result['links'][0]['has_tracker'])
        reverse = relationship_context([self.data], lambda kb: True)
        self.assertEqual({link['kb_slug'] for link in reverse['links']}, {self.manual.slug, self.other.slug})

    def test_visibility_and_explicit_scope_filter_both_endpoints(self):
        connect(self.manual, self.data, 'secret relationship')
        self.data.department = '电气'; self.data.save(update_fields=['department'])
        result = relationship_context([self.child], lambda kb: kb.department == '通用')
        self.assertEqual(result['links'], [])
        self.assertNotIn('secret relationship', str(result))
        self.assertFalse(result['has_more'])
        self.assertEqual(relationship_context([self.child], lambda kb: kb.slug == self.child.slug)['links'], [])

    def test_paged_relationships_are_bounded_and_all_accessible_links_can_be_read(self):
        expected = set()
        for index in range(11):
            kb = KnowledgeBase.objects.create(name=f'关联{index}', slug=f'link-many-{index}')
            expected.add(kb.slug); connect(self.manual, kb, '关系说明'*200)
        seen = set(); offset = 0
        while True:
            result = relationship_context([self.manual], lambda kb: True, offset=offset, cap=4000, summary_chars=1000, limit=8)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))), 4000)
            self.assertFalse(seen & {link['kb_slug'] for link in result['links']})
            seen.update(link['kb_slug'] for link in result['links'])
            if not result['has_more']: break
            self.assertGreater(result['next_offset'], offset)
            offset = result['next_offset']
        self.assertEqual(seen, expected)
        for bad in (-1, True, '0'):
            with self.assertRaises(ValueError): relationship_context([self.manual], lambda kb: True, offset=bad)

    def test_rag_read_exposes_navigation_without_reading_linked_content_or_changing_citations(self):
        connect(self.manual, self.data, '运行台账通过设备编号补充实时温度')
        evidence = []; citations = []
        session = RagSelection(lambda kb: True, evidence, citations)
        discovered = json.loads(session.discover([self.row], [self.child.slug]))
        self.assertNotIn('kb_relationships', discovered)
        with patch('kb.retriever.search') as search:
            read = json.loads(session.read(['C1']))
            search.assert_not_called()
        self.assertEqual(read['kb_relationships']['links'][0]['kb_slug'], self.data.slug)
        self.assertNotIn('29', read['chunks'][0]['text'])
        self.assertEqual(evidence, [])
        session.select(['C1'])
        self.assertEqual([item['kb_slug'] for item in evidence], [self.child.slug])
        self.assertNotIn('kb_relationships', str(evidence))
        self.assertEqual(len(citations), 1)

    def test_hints_do_not_displace_large_requested_chunk_and_respect_response_budget(self):
        connect(self.manual, self.data, '说明'*500)
        session = RagSelection(lambda kb: True, [], [])
        session.discover([{**self.row, 'text': '文'*9000}], [self.child.slug])
        text = session.read(['C1']); result = json.loads(text)
        self.assertEqual(result['chunks'][0]['text'], '文'*9000)
        self.assertLessEqual(len(text), MAX_RESPONSE_CHARS)
        self.assertIn('kb_relationships', result)
        full = {'text': '文'*9800}
        bounded = attach_relationships(full, [self.manual], lambda kb: True, cap=10000)
        self.assertEqual(bounded['text'], full['text'])
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False, separators=(',', ':'))), 10000)

    def test_tracker_hints_are_not_facts_and_relation_edits_do_not_invalidate_data(self):
        link = connect(self.manual, self.data)
        service = TrackerQuery(lambda kb: True)
        result = json.loads(service.call('lookup', kb_slug=self.data.slug, columns=['温度']))
        self.assertEqual(result['rows'][0]['values']['温度'], 29)
        self.assertEqual(result['kb_relationships']['links'][0]['kb_slug'], self.manual.slug)
        self.assertNotIn('kb_relationships', service.evidence[0]['text'])
        self.assertTrue(validate_snapshot(service.evidence[0], self.user))
        link.summary = '新的导航说明';link.save(update_fields=['summary', 'updated_at'])
        self.assertTrue(validate_snapshot(service.evidence[0], self.user))
        text = service.call('preview', kb_slug=self.data.slug)
        self.assertIn('新的导航说明', text)
        self.assertLessEqual(len(text), RESPONSE_CHARS)

    def test_new_tool_obeys_same_equipment_scope_as_search_tools(self):
        connect(self.manual, self.data)
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'):
            _build_agent(self.manual.slug, 'link-scope', {}, 5, None,
                         allowed_kb_slugs=[self.manual.slug, self.child.slug])
        tools = {tool.__name__: tool for tool in create.call_args.kwargs['tools']}
        self.assertEqual(json.loads(tools['kb_relationships']())['links'], [])
        self.assertIn('不存在', tools['kb_relationships'](kb_slug=self.data.slug))


class PlannedLinkedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class KnowledgeLinkConversationScopeTests(LinksFixture, TestCase):
    def setUp(self):
        super().setUp()
        connect(self.manual, self.data, '第一跳')
        connect(self.data, self.other, '不可自动传入的第二跳说明')

    def read(self, scope):
        session = RagSelection(lambda kb: True, [], [], relationship_scope=scope)
        session.discover([self.row], [self.child.slug])
        return json.loads(session.read(['C1']))

    def conversation(self, thread='links-once', kb=None):
        return Conversation.objects.create(user=self.user, kb=kb or self.manual, thread_id=thread)

    def test_first_read_shows_direct_links_but_related_tracker_never_exposes_next_hop(self):
        scope = RelationshipScope(lambda kb: True)
        self.assertEqual(scope.describe(self.manual)['links'], [])
        self.assertIsNone(scope.origin_id)
        read = self.read(scope)
        self.assertEqual(scope.origin_id, self.manual.pk)
        self.assertEqual([link['kb_slug'] for link in read['kb_relationships']['links']], [self.data.slug])
        tracker = TrackerQuery(lambda kb: True, relationship_scope=scope)
        self.assertNotIn('kb_relationships', json.loads(tracker.call('preview', kb_slug=self.data.slug)))
        result = tracker.call('lookup', kb_slug=self.data.slug)
        self.assertNotIn('kb_relationships', json.loads(result))
        self.assertNotIn('不可自动传入', result)
        self.assertEqual(scope.describe(self.data)['links'], [])
        self.assertEqual([link['kb_slug'] for link in scope.describe()['links']], [self.data.slug])

    def test_preview_and_empty_queries_do_not_claim_origin_but_successful_tracker_query_does(self):
        scope = RelationshipScope(lambda kb: True)
        tracker = TrackerQuery(lambda kb: True, relationship_scope=scope)
        self.assertNotIn('kb_relationships', json.loads(tracker.call('preview', kb_slug=self.data.slug)))
        tracker.call('lookup', kb_slug=self.data.slug, filters=[{'field': '温度', 'op': 'gt', 'type': 'number', 'value': 100}])
        self.assertIsNone(scope.origin_id)
        result = json.loads(tracker.call('lookup', kb_slug=self.data.slug))
        self.assertEqual(scope.origin_id, self.data.pk)
        self.assertIn('kb_relationships', result)
        self.assertNotIn('kb_relationships', self.read(scope))

    def test_origin_persists_across_turns_and_new_conversation_has_independent_origin(self):
        conv = self.conversation()
        first = RelationshipScope(lambda kb: True, conv.pk, self.user.pk)
        self.read(first)
        conv.refresh_from_db();self.assertEqual(conv.relationship_origin, self.manual.pk)
        second = RelationshipScope(lambda kb: True, conv.pk, self.user.pk)
        second.establish([self.data])
        self.assertEqual(second.origin_id, self.manual.pk)
        self.assertEqual(second.describe(self.data)['links'], [])
        other = self.conversation('links-another', kb=self.data)
        new_scope = RelationshipScope(lambda kb: True, other.pk, self.user.pk)
        new_scope.establish([self.data])
        self.assertEqual({link['kb_slug'] for link in new_scope.describe()['links']}, {self.manual.slug, self.other.slug})

    def test_existing_conversation_uses_earliest_published_source_not_latest_default_kb(self):
        conv = self.conversation(kb=self.data)
        Message.objects.create(conversation=conv, role='ai', content='以前的答复', citations=[{
            'doc_id': str(self.doc.pk), 'source': self.doc.original_name}])
        scope = RelationshipScope(lambda kb: True, conv.pk, self.user.pk)
        self.assertEqual(scope.origin_id, self.manual.pk)
        conv.refresh_from_db();self.assertEqual(conv.relationship_origin, self.manual.pk)

    def test_foreign_conversation_cannot_be_claimed_and_removed_origin_is_not_replaced(self):
        conv = self.conversation(kb=self.data)
        other_user = get_user_model().objects.create_user('foreign-links-user')
        foreign = RelationshipScope(lambda kb: True, conv.pk, other_user.pk)
        foreign.establish([self.manual]);self.assertIsNone(foreign.origin_id)
        conv.refresh_from_db();self.assertIsNone(conv.relationship_origin)
        scope = RelationshipScope(lambda kb: True, conv.pk, self.user.pk)
        self.read(scope);original = self.manual.pk
        self.manual.delete()
        followup = RelationshipScope(lambda kb: True, conv.pk, self.user.pk)
        followup.establish([self.data])
        self.assertEqual(followup.origin_id, original)
        self.assertEqual(followup.describe()['links'], [])


class KnowledgeLinkAgentTests(LinksFixture, TransactionTestCase):
    def test_agent_searches_linked_kb_via_candidate_directory_then_reads_and_selects(self):
        connect(self.manual, self.other, '维修记录可补充限值适用情况')
        connect(self.other, self.data, '第二层台账不应继续展开')
        child = KnowledgeBase.objects.create(name='记录.pdf', slug='links-second-doc', parent=self.other)
        doc = Document.objects.create(kb=child, original_name='记录.pdf', status='completed', md_content='维修记录原文')
        other_row = {'doc_id': str(doc.pk), 'source': doc.original_name, 'chunk_id': 'other-chunk',
                     'text': '记录'*200+'实际维修记录：已核对参数。', 'page_label': '第2页'}
        sequence = [
            ('kb_search', {'query': '限值', 'kb_slug': self.manual.slug}), ('kb_read_chunks', {'candidate_ids': ['C1']}),
            ('kb_select_evidence', {'candidate_ids': ['C1']}),
            ('kb_search', {'query': '维修记录', 'kb_slug': self.other.slug}),
            ('kb_read_chunks', {'candidate_ids': ['C2']}),
            ('kb_select_evidence', {'candidate_ids': ['C1', 'C2']}),
        ]
        model = PlannedLinkedModel(responses=[AIMessage(content='', tool_calls=[{'id': str(i), 'name': name, 'args': args}])
            for i, (name, args) in enumerate(sequence)] + [AIMessage(content='已结合手册和维修记录回答。')])
        evidence = []; citations = []
        with patch('kb.agent._get_llm', return_value=model), patch('kb.provenance.annotate_results'), \
                patch('kb.retriever.search_folder', side_effect=[[self.row], [other_row]]) as searches:
            agent = _build_agent(self.manual.slug, 'linked-agent', {}, 5, None, evidence=evidence, citations=citations)
            response = agent.invoke({'messages': [{'role': 'user', 'content': '结合有关资料分析限值'}]})
        self.assertEqual(searches.call_args_list[1].args[0], [child.slug])
        results = [message for message in response['messages'] if isinstance(message, ToolMessage)]
        self.assertIn('维修记录可补充限值适用情况', str(results[1].content))
        linked_candidates = json.loads(results[3].content)
        self.assertEqual(linked_candidates['documents'][0]['doc_id'], str(doc.pk))
        self.assertNotIn(other_row['text'], str(linked_candidates))
        self.assertNotIn('kb_relationships', json.loads(results[4].content))
        self.assertEqual({item['doc_id'] for item in citations}, {str(self.doc.pk), str(doc.pk)})
        self.assertEqual(len(evidence), 2)
