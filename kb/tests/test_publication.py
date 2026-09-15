"""Synthetic SSE regression: rejected drafts must never cross the wire."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase, TestCase
from django.contrib.auth import get_user_model

from kb.agent import run_agent_stream
from kb.qa_steps import _parse_verdict, verify_answer
from kb.publication import bind_evidence, validate_sources
from kb.models import Document, KnowledgeBase


class PublicationStreamTests(SimpleTestCase):
    async def collect(self, verdict='pass', *, enhance=True, truncated=False, revoked=False):
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
            return [item async for item in run_agent_stream('value?', 'client-id', 'synthetic', {'llm': {'qa_enhance':enhance}, 'top_k':5, 'user_id':1})]

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


class PublishedHistoryTests(TestCase):
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
            response = self.client.post(reverse('kb:stream'), {'message':'new question', 'thread_id':conv.thread_id})
            async def consume():
                return [item async for item in response.streaming_content]
            asyncio.run(consume())
        self.assertNotIn('LEGACY PRIVATE DRAFT', repr(captured['published_history']))
        self.assertEqual(Message.objects.filter(conversation=conv, role='ai').count(), 1)


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
