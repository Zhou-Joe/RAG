import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase, TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import Field

from kb.citation_numbers import (CitationGuide, annotate_citations, citation_marker_issues,
                                 numbered_evidence, source_map, visible_kb_names)
from kb.models import Conversation, KnowledgeBase, Message
from kb.publication import visible_citations


def tracker(slug='operations', version='one'):
    snapshot = {'kind': 'tracker', 'query': {'kb_slug': slug}, 'result_digest': version, 'text': 'value 42'}
    return {'kind': 'tracker', 'kb_slug': slug, 'result_digest': version, 'tracker_snapshot': snapshot,
            'source': 'Tracker Query'}


class CitationNumberTests(SimpleTestCase):
    def test_tracker_queries_share_a_display_number_without_losing_any_snapshot(self):
        rows = [tracker(version='one'), tracker(version='two'), tracker('other')]
        snapshots = [row['tracker_snapshot'] for row in rows]
        original = deepcopy(snapshots)
        annotate_citations(rows, kb_names={'operations': 'Operations Knowledge Base', 'other': 'Other'})
        self.assertEqual([row['citation_number'] for row in rows], [1, 1, 2])
        self.assertEqual(rows[0]['kb_name'], 'Operations Knowledge Base')
        self.assertEqual([row['tracker_snapshot'] for row in rows], original)
        self.assertTrue(all(row['tracker_snapshot'] is snapshot for row, snapshot in zip(rows, snapshots)))
        self.assertEqual(source_map(rows)[0]['query_snapshots'], 2)

    def test_document_and_graph_identities_do_not_collapse_with_trackers(self):
        rows = [{'doc_id': 'doc-1', 'source': 'Manual.pdf'}, tracker(),
                {'doc_id': 'doc-1', 'source': 'Manual.pdf'},
                {'kind': 'graph', 'relation_id': 'r1', 'fact_digest': 'v1'},
                {'kind': 'graph', 'relation_id': 'r1', 'fact_digest': 'v2'}]
        annotate_citations(rows)
        self.assertEqual([row['citation_number'] for row in rows], [1, 2, 1, 3, 4])

    def test_numbers_survive_reselection_history_and_new_sources(self):
        registry = {}
        rows = [tracker('first'), tracker('second')]
        annotate_citations(rows, registry=registry)
        current = [deepcopy(rows[1]), tracker('third')]
        annotate_citations(current, registry=registry)
        self.assertEqual([row['citation_number'] for row in current], [2, 3])
        annotate_citations(current)
        self.assertEqual([row['citation_number'] for row in current], [2, 3])
        restored = [tracker('first'), *current]
        annotate_citations(restored, registry=registry)
        self.assertEqual([row['citation_number'] for row in restored], [1, 2, 3])

    def test_legacy_or_colliding_metadata_gets_deterministic_distinct_numbers(self):
        rows = [dict(tracker('first'), citation_number=4), dict(tracker('second'), citation_number=4),
                dict(tracker('third'), citation_number=True)]
        annotate_citations(rows)
        self.assertEqual([row['citation_number'] for row in rows], [4, 5, 6])
        self.assertEqual([row['citation_id'] for row in rows], ['tracker:first', 'tracker:second', 'tracker:third'])

    def test_history_display_number_does_not_claim_legacy_inline_marker_was_model_mapped(self):
        rows = [tracker(), dict(tracker('previously-mapped'), citation_number=7)]
        annotate_citations(rows, display_only=True)
        self.assertFalse(rows[0]['citation_inline'])
        self.assertNotIn('citation_inline', rows[1])
        request = ModelRequest(model=None, messages=[HumanMessage(content='Follow up')], tools=[],
                               system_message=SystemMessage(content='Base rules'))
        prepared = CitationGuide(rows).prepare(request)
        self.assertIn('AVAILABLE CITATION SOURCES', prepared.system_message.content)
        self.assertTrue(all(row['citation_inline'] for row in rows))

    def test_deduped_display_still_checks_every_underlying_tracker_snapshot(self):
        rows = [tracker(version='one'), tracker(version='two')]
        annotate_citations(rows)
        with patch('kb.tracker_query.validate_snapshot', side_effect=[True, False]) as validate:
            self.assertFalse(visible_citations(rows, SimpleNamespace(is_active=True)))
        self.assertEqual(validate.call_count, 2)

    def test_unknown_markers_fail_but_code_arrays_and_markdown_links_are_not_references(self):
        citations = annotate_citations([tracker()])
        valid = ('Value 42 [1]. Code `values[999]` and ``example [999]``. '
                 '\n```python\nexample = [999]\n```\n~~~\n[999]\n~~~\n'
                 '[999](https://example.test)\n[999]: https://example.test\narray[999] and \\[999]')
        self.assertEqual(citation_marker_issues(valid, citations), [])
        self.assertEqual(citation_marker_issues('Claim [2] and [0].', citations),
                         ['Unknown citation marker [0].', 'Unknown citation marker [2].'])
        self.assertEqual(citation_marker_issues('General array example [999].', []), [])
        self.assertEqual(citation_marker_issues('正常[2]，数值42[3]。', citations),
                         ['Unknown citation marker [2].', 'Unknown citation marker [3].'])

    def test_verifier_numbering_copies_provenance_instead_of_mutating_it(self):
        rows = annotate_citations([tracker()])
        evidence = [rows[0]['tracker_snapshot'], {'kind': 'chat_attachment', 'text': 'user upload'}]
        result = numbered_evidence(evidence, rows)
        self.assertEqual(result[0]['citation_number'], 1)
        self.assertNotIn('citation_number', evidence[0])
        self.assertIs(result[1], evidence[1])
        from kb.qa_steps import _format_evidence
        text = _format_evidence(result)
        self.assertIn('[1]', text)
        self.assertIn('[Evidence 2]', text)

    def test_sync_and_async_model_requests_receive_current_source_map_without_database_access(self):
        rows = []
        guide = CitationGuide(rows, {'operations': 'Operations Knowledge Base'})
        request = ModelRequest(model=None, messages=[HumanMessage(content='value?')], tools=[],
                               system_message=SystemMessage(content='Base rules'))
        self.assertIn('No numbered source is available yet', guide.wrap_model_call(request, lambda item: item).system_message.content)
        rows.extend([tracker(), tracker(version='two')])
        prepared = guide.wrap_model_call(request, lambda item: item)
        self.assertIn('Base rules', prepared.system_message.content)
        self.assertIn('Operations Knowledge Base', prepared.system_message.content)
        self.assertIn('"number": 1', prepared.system_message.content)
        self.assertIn('"query_snapshots": 2', prepared.system_message.content)
        async def handler(item):
            return item
        actual = asyncio.run(guide.awrap_model_call(request, handler))
        self.assertEqual(actual.system_message.content, prepared.system_message.content)

    def test_real_agent_tool_round_injects_map_into_the_next_model_call(self):
        rows = []
        @tool
        def load_value() -> str:
            """Load the actual value from the operations tracker."""
            rows.append(tracker())
            return 'Operations tracker value: 42'

        class ProbeModel(BaseChatModel):
            calls: int = 0
            seen: list = Field(default_factory=list)

            @property
            def _llm_type(self):
                return 'citation-probe'

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                self.calls += 1
                self.seen.append(messages[0].content)
                output = (AIMessage(content='', tool_calls=[{'name': 'load_value', 'args': {}, 'id': 'load-1'}])
                          if self.calls == 1 else AIMessage(content='The value is 42 [1].'))
                return ChatResult(generations=[ChatGeneration(message=output)])

        model = ProbeModel()
        graph = create_agent(model, tools=[load_value], middleware=[CitationGuide(rows, {'operations': 'Operations Knowledge Base'})])
        result = graph.invoke({'messages': [HumanMessage(content='What is the tracker value?')]})
        self.assertEqual(result['messages'][-1].content, 'The value is 42 [1].')
        self.assertEqual(model.calls, 2)
        self.assertIn('No numbered source is available yet', model.seen[0])
        self.assertIn('Operations Knowledge Base', model.seen[1])
        self.assertIn('"number": 1', model.seen[1])
        self.assertEqual(rows[0]['citation_number'], 1)

    def test_actual_verifier_request_keeps_display_numbers_bound_to_specific_sources(self):
        from kb.qa_steps import verify_answer
        citations = annotate_citations([tracker(), {'doc_id': 'manual', 'source': 'Manual.pdf'}])
        evidence = [citations[0]['tracker_snapshot'], {'doc_id': 'manual', 'source': 'Manual.pdf', 'text': 'manual value 99'}]
        invoke = AsyncMock(return_value=SimpleNamespace(content='{"verdict":"pass","issues":[]}'))
        with patch('kb.qa_steps.build_llm', return_value=SimpleNamespace(ainvoke=invoke)):
            verdict, _ = asyncio.run(verify_answer({}, 'values?', 'Tracker 42 [1], manual 99 [2].', numbered_evidence(evidence, citations)))
        self.assertEqual(verdict['verdict'], 'pass')
        prompt = invoke.call_args.args[0][0]['content']
        self.assertIn('[1]', prompt)
        self.assertIn('[2] Manual.pdf', prompt)
        self.assertIn('same [N] marker', prompt)
        self.assertNotIn('citation_number', evidence[0])

    def test_enhanced_stream_refuses_invented_marker_before_draft_release(self):
        from kb.agent import run_agent_stream
        class FakeAgent:
            async def astream_events(self, *args, **kwargs):
                yield {'event': 'on_chat_model_start', 'data': {}}
                yield {'event': 'on_chat_model_stream', 'data': {'chunk': SimpleNamespace(content='Unsupported source number [99].')}}
                yield {'event': 'on_chat_model_end', 'data': {'output': SimpleNamespace(
                    content='Unsupported source number [99].', tool_calls=[], response_metadata={'finish_reason': 'stop'})}}
        def build(*args, **kwargs):
            citation = tracker()
            kwargs['citations'].append(citation)
            kwargs['evidence'].append(citation['tracker_snapshot'])
            return FakeAgent()
        async def collect():
            return [event async for event in run_agent_stream('value?', 'citations', '',
                    {'llm': {'qa_enhance': True}, 'top_k': 5, 'user_id': 1})]
        with patch('kb.agent._build_agent', side_effect=build), patch('kb.publication.validate_sources', return_value=True), patch(
            'kb.qa_steps.plan_query', new_callable=AsyncMock, return_value=(None, {'input_tokens': 0, 'output_tokens': 0})
        ), patch('kb.qa_steps.verify_answer', new_callable=AsyncMock) as verify:
            events = asyncio.run(collect())
        self.assertFalse(any(kind == 'token' for kind, _ in events))
        self.assertTrue(any(kind == 'verify' and not body['ok'] for kind, body in events))
        verify.assert_not_called()


class CitationHistoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='citation-reader', password='test')
        self.kb = KnowledgeBase.objects.create(name='Operations Knowledge Base', slug='operations')
        self.private = KnowledgeBase.objects.create(name='Private Secret Name', slug='private-secret', visibility='individual')

    def test_name_resolution_never_reveals_inaccessible_knowledge_base_names(self):
        names = visible_kb_names([tracker(), tracker('private-secret')], self.user)
        self.assertEqual(names, {'operations': 'Operations Knowledge Base'})

    def test_history_adds_metadata_after_permission_checks_without_replacing_raw_snapshots(self):
        conversation = Conversation.objects.create(user=self.user, thread_id='numbered-citations-history', title='Tracker values')
        rows = [tracker(version='one'), tracker(version='two')]
        Message.objects.create(conversation=conversation, role=Message.Role.AI, content='Legacy content containing [1].', citations=rows)
        self.client.force_login(self.user)
        with patch('kb.publication.visible_citations', return_value=True):
            response = self.client.get(reverse('kb:conv_messages', args=['numbered-citations-history']))
        self.assertEqual(response.status_code, 200)
        actual = response.json()['messages'][0]['citations']
        self.assertEqual(len(actual), 2)
        self.assertEqual([row['citation_number'] for row in actual], [1, 1])
        self.assertEqual(actual[0]['kb_name'], 'Operations Knowledge Base')
        self.assertTrue(all(row['citation_inline'] is False for row in actual))
        self.assertEqual(response.json()['messages'][0]['content'], 'Legacy content containing [1].')
        self.assertEqual([row['tracker_snapshot'] for row in actual], [row['tracker_snapshot'] for row in rows])

    def test_revoked_history_never_exposes_numbers_or_knowledge_base_names(self):
        conversation = Conversation.objects.create(user=self.user, thread_id='revoked-numbered-history', title='Tracker values')
        Message.objects.create(conversation=conversation, role=Message.Role.AI, content='Private old answer [1].', citations=[tracker()])
        self.client.force_login(self.user)
        with patch('kb.publication.visible_citations', return_value=False):
            response = self.client.get(reverse('kb:conv_messages', args=['revoked-numbered-history']))
        self.assertEqual(response.status_code, 200)
        actual = response.json()['messages'][0]
        self.assertEqual(actual['citations'], [])
        self.assertNotIn('Private old answer', actual['content'])
