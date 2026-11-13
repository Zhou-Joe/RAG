"""Synthetic SSE regression: rejected drafts must never cross the wire."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.contrib.auth import get_user_model

from kb.agent import run_agent_stream
from kb.qa_steps import _parse_verdict, verify_answer
from kb.publication import bind_evidence, validate_sources
from kb.models import Document, FeedRecord, KnowledgeBase, KnowledgeFeed


class PublicationStreamTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        # These streams use synthetic sources and user_id, not persisted KBs.
        # Keep history's visibility lookup outside this publication fixture;
        # source revocation and answer-verification boundaries stay exercised.
        visibility = patch('kb.history.field_visibility_scope', return_value={})
        visibility.start()
        self.addCleanup(visibility.stop)

    def test_graph_lookup_respects_the_selected_verification_mode(self):
        class FakeAgent:
            async def astream_events(self, *args, **kwargs):
                yield {'event': 'on_tool_start', 'name': 'kb_graph_search', 'data': {}}
                yield {'event': 'on_chat_model_start', 'data': {}}
                yield {'event': 'on_chat_model_stream', 'data': {'chunk': SimpleNamespace(content='CONFIRMED GRAPH FACT')}}
                yield {'event': 'on_chat_model_end', 'data': {'output': SimpleNamespace(
                    content='CONFIRMED GRAPH FACT', tool_calls=[], response_metadata={'finish_reason': 'stop'})}}
        def build(*args, **kwargs):
            kwargs['evidence'].append({'kind': 'graph', 'text': 'source'})
            return FakeAgent()
        async def collect(enhance):
            return [ev async for ev in run_agent_stream('E-1?', 'graph', 'graph',
                {'llm': {'qa_enhance': enhance}, 'top_k': 5, 'user_id': 1})]
        for enhance in (False, True):
            with self.subTest(enhance=enhance), patch('kb.agent._build_agent', side_effect=build), patch(
                'kb.publication.validate_sources', return_value=False,
            ) as source_check, patch('kb.qa_steps.verify_answer',new_callable=AsyncMock) as verify, patch(
                'kb.qa_steps.plan_query',new_callable=AsyncMock,return_value=(None,{'input_tokens':0,'output_tokens':0}),
            ):
                events = asyncio.run(collect(enhance))
            if enhance:
                self.assertNotIn('CONFIRMED GRAPH FACT', repr(events))
                self.assertTrue(any(kind == 'verify' and not body['ok'] for kind, body in events))
            else:
                self.assertTrue(any(kind == 'token' and body['text']=='CONFIRMED GRAPH FACT' for kind,body in events))
                self.assertFalse(any(kind == 'verify' for kind,body in events))
                source_check.assert_not_called()
            verify.assert_not_called()

    async def collect(self, verdict='pass', *, enhance=True, truncated=False, revoked=False, question='value?'):
        class FakeAgent:
            async def astream_events(self, *args, **kwargs):
                yield {'event': 'on_chat_model_stream', 'data': {'chunk': SimpleNamespace(content='PRIVATE PREAMBLE')}}
                yield {'event': 'on_chat_model_end', 'data': {'output': SimpleNamespace(content='PRIVATE PREAMBLE', tool_calls=[{'name':'kb_search','args':{}}])}}
                yield {'event': 'on_tool_end', 'name': 'kb_search', 'data': {'output': 'PRIVATE TOOL PREVIEW'}}
                yield {'event': 'on_tool_start', 'name': 'write_analysis', 'run_id': 'export', 'data': {'input': {'code':'PRIVATE CODE'}}}
                yield {'event': 'on_tool_end', 'name': 'write_analysis', 'run_id': 'export', 'data': {}}
                yield {'event': 'on_chat_model_start', 'data': {}}
                yield {'event': 'on_chat_model_stream', 'data': {'chunk': SimpleNamespace(content='DRAFT 42')}}
                yield {'event': 'on_chat_model_end', 'data': {'output': SimpleNamespace(content='DRAFT 42', tool_calls=[], response_metadata={'finish_reason': 'length' if truncated else 'stop'})}}
        async def verify(*args):
            result = None if verdict is None else {'verdict': verdict, 'issues': []}
            return result, {'input_tokens': 1, 'output_tokens': 1}
        def build(*args, **kwargs):
            kwargs['evidence'].append({'source': 'synthetic.txt', 'text': 'value 42'})
            return FakeAgent()
        with patch('kb.agent._build_agent', side_effect=build), patch('kb.agent._get_checkpointer', new=AsyncMock(return_value=None)), patch('kb.qa_steps.plan_query', new=AsyncMock(return_value=(None, {'input_tokens':0,'output_tokens':0}))), patch('kb.qa_steps.verify_answer', side_effect=verify), patch('kb.publication.validate_sources', side_effect=[True, not revoked, not revoked]):
            return [item async for item in run_agent_stream(question, 'client-id', 'synthetic', {'llm': {'qa_enhance':enhance}, 'top_k':5, 'user_id':1})]

    def test_manual_overview_requires_verification_even_when_enhance_disabled(self):
        with patch('kb.manual_overview.outline_answer', return_value=''):
            events = asyncio.run(self.collect(verdict='fail', enhance=False, question='漂流手册有啥，给我总结一下'))
        self.assertFalse(any(kind == 'token' for kind, _ in events))
        self.assertTrue(any(kind == 'verify' and not data['ok'] for kind, data in events))

    def test_failed_overview_only_publishes_source_extraction(self):
        with patch('kb.manual_overview.outline_answer', return_value='原文章节目录'):
            events = asyncio.run(self.collect(verdict='fail', enhance=False, question='漂流手册有啥，给我总结一下'))
        self.assertEqual([data['text'] for kind,data in events if kind == 'token'],['原文章节目录'])
        self.assertNotIn('DRAFT',repr(events))

    def test_fail_warn_timeout_truncation_and_revocation_do_not_publish(self):
        for kwargs in ({'verdict':'fail'}, {'verdict':'warn'}, {'verdict':None}, {'truncated':True}, {'revoked':True}):
            events = asyncio.run(self.collect(**kwargs))
            wire = repr(events)
            self.assertNotIn('DRAFT', wire)
            self.assertNotIn('PRIVATE', wire)
            self.assertFalse(any(kind in ('token', 'code_run', 'reasoning') for kind, _ in events))
            self.assertTrue(any(kind == 'verify' and not body['ok'] for kind, body in events))

    def test_pass_releases_only_final_answer_after_verify(self):
        events = asyncio.run(self.collect())
        self.assertNotIn('PRIVATE', repr(events))
        kinds = [kind for kind, _ in events]
        self.assertLess(kinds.index('verify'), kinds.index('token'))
        self.assertEqual([body['text'] for kind, body in events if kind == 'token'], ['DRAFT 42'])

    def test_switch_off_preserves_streaming(self):
        events = asyncio.run(self.collect(enhance=False))
        self.assertTrue(any(kind == 'token' for kind, _ in events))
        self.assertTrue(any(kind == 'code_run' for kind, _ in events))
        self.assertFalse(any(kind == 'verify' for kind, _ in events))

    def test_verdict_does_not_accept_bare_or_inconsistent_pass(self):
        for value in ('pass', 'not true', '{"verdict":"pass", "issues":["wrong value"]}', '{"verdict":"pass", "issues":"invalid"}'):
            self.assertIsNone(_parse_verdict(value))

    def test_verifier_does_not_silently_truncate_evidence(self):
        with patch('kb.qa_steps.build_llm') as llm:
            verdict, _ = asyncio.run(verify_answer({}, 'question', 'answer', [{'text':'x' * 24001}]))
            self.assertIsNone(verdict)
            llm.assert_not_called()


class SourceSnapshotTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('reader')
        self.kb = KnowledgeBase.objects.create(name='public', slug='synthetic')
        self.doc = Document.objects.create(kb=self.kb, original_name='manual.txt', md_content='valve interval daily', status='completed')
        self.row = {'source':'manual.txt', 'chunk_id':'chunk-synthetic', 'text':'valve interval daily'}
        self.evidence = bind_evidence([self.row], self.kb.slug)

    def test_source_update_deletion_and_access_change_revoke_publication(self):
        with patch('kb.keyword_index.get_chunks', return_value=[self.row]):
            self.assertTrue(validate_sources(self.evidence, self.user.pk))
            self.doc.md_content = 'valve interval weekly'
            self.doc.save()
            self.assertFalse(validate_sources(self.evidence, self.user.pk))
            self.doc.md_content = 'valve interval daily'
            self.doc.save()
            self.kb.department = 'restricted'
            self.kb.save()
            self.assertFalse(validate_sources(self.evidence, self.user.pk))
            self.doc.delete()
            self.assertFalse(validate_sources(self.evidence, self.user.pk))

    def test_chunk_change_is_rejected(self):
        with patch('kb.keyword_index.get_chunks', return_value=[dict(self.row, text='different')]):
            self.assertFalse(validate_sources(self.evidence, self.user.pk))


class UnembeddedFeedSourceSnapshotTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('feed-source-reader')
        self.kb = KnowledgeBase.objects.create(
            name='Fetched document', slug='feed-source-snapshot', category='business',
        )
        self.feed = KnowledgeFeed.objects.create(
            kb=self.kb, name='Fetched source', created_by=self.user,
            adapter_spec={'ingest_mode': 'documents'},
        )
        self.doc = Document.objects.create(
            kb=self.kb, original_name='generated.md', status=Document.Status.COMPLETED,
            md_content='# Report\n\nA verified source paragraph.',
        )
        self.record = FeedRecord.objects.create(
            feed=self.feed, kb=self.kb, external_id='report', document=self.doc,
        )
        self.evidence = bind_evidence([{
            'source': self.doc.original_name, 'doc_id': str(self.doc.pk),
            'kb_slug': self.kb.slug, 'chunk_id': '',
            'text': 'A verified source paragraph.',
        }], self.kb.slug)

    def test_current_raw_source_is_verified_without_keyword_chunks(self):
        with patch('kb.keyword_index.get_chunks') as keyword:
            self.assertTrue(validate_sources(self.evidence, self.user.pk))
        keyword.assert_not_called()

    def test_raw_source_survives_candidate_read_and_selection(self):
        import json
        from kb.rag_selection import RagSelection

        evidence, citations = [], []
        selection = RagSelection(lambda library: library.pk == self.kb.pk,
                                 evidence, citations)
        row = {'source': self.doc.original_name, 'doc_id': str(self.doc.pk),
               'kb_slug': self.kb.slug, 'chunk_id': '',
               'text': 'A verified source paragraph.'}
        candidates = json.loads(selection.discover([row], [self.kb.slug]))
        selected = candidates['candidates'][0]['candidate_id']
        self.assertIn('A verified source paragraph.',
                      json.loads(selection.read([selected]))['chunks'][0]['text'])
        self.assertEqual(json.loads(selection.select([selected]))['selected_chunks'], 1)
        self.assertTrue(validate_sources(evidence, self.user.pk))

    def test_tampered_raw_text_or_digest_is_rejected(self):
        original = self.evidence[0]
        for changed in (
            {'text': 'An invented source paragraph.'},
            {'text': 'A verified source paragraph.', 'chunk_digest': 'wrong'},
            {'source': 'other.md'},
            {'kb_slug': 'other-library'},
        ):
            self.assertFalse(validate_sources([{**original, **changed}], self.user.pk))

    def test_replaced_or_deleted_feed_source_is_rejected(self):
        replacement = Document.objects.create(
            kb=self.kb, original_name='replacement.md', status=Document.Status.COMPLETED,
            md_content='Replacement source',
        )
        self.record.document = replacement
        self.record.save(update_fields=['document'])
        self.assertFalse(validate_sources(self.evidence, self.user.pk))
        self.record.document = self.doc
        self.record.deleted = True
        self.record.save(update_fields=['document', 'deleted'])
        self.assertFalse(validate_sources(self.evidence, self.user.pk))

    def test_manual_upload_without_chunk_id_is_rejected(self):
        manual = Document.objects.create(
            kb=self.kb, original_name='manual.md', status=Document.Status.COMPLETED,
            md_content='Manual upload body',
        )
        evidence = bind_evidence([{
            'source': manual.original_name, 'doc_id': str(manual.pk),
            'kb_slug': self.kb.slug, 'chunk_id': '', 'text': 'Manual upload body',
        }], self.kb.slug)
        self.assertFalse(validate_sources(evidence, self.user.pk))


class PublishedHistoryTests(TransactionTestCase):
    def test_legacy_draft_is_not_passed_to_enhanced_agent(self):
        from kb.models import Conversation, Message, SiteConfig
        user = get_user_model().objects.create_user('history-reader')
        kb = KnowledgeBase.objects.create(name='history', slug='history')
        conv = Conversation.objects.create(user=user, kb=kb, thread_id='reused-client-id')
        Message.objects.create(conversation=conv, role='ai', content='LEGACY PRIVATE DRAFT')
        cfg = SiteConfig.get()
        cfg.qa_enhance = True
        cfg.save()
        self.client.force_login(user)
        captured = {}
        async def fake_stream(message, thread, slug, config):
            captured.update(config)
            yield 'verify', {'ok':False, 'issues':[]}
        with patch('kb.agent.run_agent_stream', side_effect=fake_stream):
            from django.urls import reverse
            async def consume():
                self.async_client.cookies = self.client.cookies
                response = await self.async_client.post(reverse('kb:stream'), {'message':'new question', 'thread_id':conv.thread_id})
                return [item async for item in response.streaming_content]
            asyncio.run(consume())
        self.assertNotIn('LEGACY PRIVATE DRAFT', repr(captured['published_history']))
        self.assertEqual(Message.objects.filter(conversation=conv, role='ai', completion_status='complete').count(), 1)


class FixedExportTests(SimpleTestCase):
    def test_only_single_verified_table_becomes_csv(self):
        from kb.publication import verified_table_export
        table = '| Part | Interval |\n| --- | --- |\n| Valve | Daily |'
        export = verified_table_export(table, '../../result.xlsx')
        self.assertEqual(export['filename'], 'result.csv')
        self.assertIn("['Valve', 'Daily']", export['code'])
        self.assertIsNone(verified_table_export('No table'))
        self.assertIsNone(verified_table_export(table + '\n\n' + table))

    def test_formula_values_are_escaped_as_data(self):
        from kb.publication import verified_table_export
        export = verified_table_export('| Item |\n| --- |\n| =1+1 |')
        self.assertIn("'=1+1", export['code'])
        self.assertNotIn('eval(', export['code'])
