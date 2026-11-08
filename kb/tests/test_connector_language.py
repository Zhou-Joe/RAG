"""The visible language switch must reach every builder model turn."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from langchain_core.messages import AIMessageChunk, SystemMessage, ToolMessage

from kb import collector_api, connector_sessions
from kb.connector_agent import run
from kb.models import ConnectorDraft, KnowledgeBase, KnowledgeFeed
from kb.tests.test_connector import FakeModel, call, sample, spec


class ConnectorReplyLanguageTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = self.settings(DATA_DIR=Path(directory.name))
        override.enable()
        self.addCleanup(override.disable)
        self.user = get_user_model().objects.create_user('language-builder', is_staff=True)
        self.client.force_login(self.user)
        self.jobs = []
        self.addCleanup(lambda: connector_sessions.discard(self.jobs, self.user.pk))
        self.start = patch('kb.collector_api.start_async').start()
        self.addCleanup(patch.stopall)

    def create(self, **headers):
        response = self.client.post(reverse('kb:connector_create'),
            json.dumps({'prompt': 'Explain how the collection schedule works.'}),
            content_type='application/json', **headers)
        self.assertEqual(response.status_code, 202, response.content)
        job = ConnectorDraft.objects.get(pk=response.json()['job']['id'])
        self.jobs.append(job.pk)
        return job

    def execute(self, job, responses):
        self.jobs.append(job.pk)
        model = FakeModel(responses)
        # Simulate a worker/process running under the default server locale.
        with translation.override('zh-hans'), patch('kb.agent._get_llm', return_value=model) as factory:
            result = run(job.pk)
        return result, model, factory.call_args.args[0]

    def test_create_uses_visible_switch_even_when_cookie_is_stale(self):
        self.client.cookies['fos_language'] = 'zh-hans'
        job = self.create(HTTP_X_INTERFACE_LANGUAGE='en')
        self.assertEqual(job.response_language, 'en')
        self.assertEqual(self.client.get(reverse('kb:connector_status', args=[job.pk])).json()['job']['response_language'], 'en')

    def test_request_cookie_is_fallback_for_clients_without_language_header(self):
        self.client.cookies['fos_language'] = 'en'
        self.assertEqual(self.create().response_language, 'en')

    def test_invalid_header_falls_back_to_supported_chinese_locale(self):
        self.assertEqual(self.create(HTTP_X_INTERFACE_LANGUAGE='<invalid>').response_language, 'zh-hans')

    def test_service_captures_caller_locale_before_starting_worker(self):
        with translation.override('en'):
            job = collector_api.create_draft(user=self.user, requirement='How does this schedule work?', conversation=True)
        self.assertEqual(job.response_language, 'en')
        self.start.assert_called_once_with(job.pk)

    def test_worker_config_and_current_prompt_use_persisted_language(self):
        job = self.create(HTTP_X_INTERFACE_LANGUAGE='en')
        result, model, config = self.execute(job, [AIMessageChunk(content='The schedule controls collection time.')])
        self.assertEqual(result.status, 'answered', result.error)
        self.assertEqual(config['response_language'], 'en')
        self.assertIsInstance(model.messages[0][0], SystemMessage)
        self.assertIn('interface language is English', model.messages[0][0].content)
        self.assertEqual(json.loads(model.messages[0][1].content)['response_language'], 'en')
        self.assertEqual(result.preview['answer'], 'The schedule controls collection time.')

    def test_language_switch_on_followup_preserves_old_history_and_tool_results(self):
        parent = ConnectorDraft.objects.create(user=self.user, prompt='说明当前计划。', sample=sample())
        parent, _, _ = self.execute(parent, [call('get_current_time'), AIMessageChunk(content='原来的中文回答。')])
        self.client.cookies['fos_language'] = 'zh-hans'
        response = self.client.post(reverse('kb:connector_refine', args=[parent.pk]),
            json.dumps({'feedback': 'Now explain the same schedule.'}), content_type='application/json',
            HTTP_X_INTERFACE_LANGUAGE='en')
        self.assertEqual(response.status_code, 202, response.content)
        child = ConnectorDraft.objects.get(pk=response.json()['job']['id'])
        result, model, config = self.execute(child, [AIMessageChunk(content='Here is the same schedule.')])
        self.assertEqual(result.status, 'answered', result.error)
        self.assertEqual(config['response_language'], 'en')
        messages = model.messages[0]
        self.assertIn('interface language is English', messages[0].content)
        self.assertIn('interface language is English', messages[-1].content)
        self.assertIn('原来的中文回答。', repr(messages))
        self.assertIn('A1日报', repr(messages))
        self.assertTrue(any(isinstance(message, ToolMessage) for message in messages))
        self.assertEqual(result.metrics['tools'], 0)
        parent.refresh_from_db()
        self.assertEqual(parent.response_language, 'zh-hans')
        response = self.client.post(reverse('kb:connector_refine', args=[child.pk]),
            json.dumps({'feedback': '继续解释。'}), content_type='application/json', HTTP_X_INTERFACE_LANGUAGE='zh-hans')
        next_job = ConnectorDraft.objects.get(pk=response.json()['job']['id'])
        self.jobs.append(next_job.pk)
        self.assertEqual(next_job.response_language, 'zh-hans')

    def test_restarted_conversation_uses_new_language_from_persisted_turn(self):
        parent = ConnectorDraft.objects.create(user=self.user, prompt='原始问题。')
        self.execute(parent, [AIMessageChunk(content='之前的中文回答。')])
        connector_sessions.discard([parent.pk], self.user.pk)
        child = collector_api.refine_draft(user=self.user, job=ConnectorDraft.objects.get(pk=parent.pk),
            requirement='Continue in the current interface language.', response_language='en')
        result, model, config = self.execute(child, [AIMessageChunk(content='Restored and continuing in English.')])
        self.assertEqual(result.status, 'answered', result.error)
        self.assertEqual(result.metrics['context_source'], 'saved')
        self.assertEqual(config['response_language'], 'en')
        self.assertIn('interface language is English', model.messages[0][0].content)
        self.assertIn('之前的中文回答。', repr(model.messages[0]))

    def test_generated_question_form_gets_same_english_language_instruction(self):
        job = self.create(HTTP_X_INTERFACE_LANGUAGE='en')
        fields = [{'key': 'date_range', 'label': 'Which date range should the report cover?', 'type': 'text'}]
        result, model, config = self.execute(job, [call('ask_user_questions', {'questions': fields})])
        self.assertEqual(result.status, 'needs_info', result.error)
        self.assertEqual(config['response_language'], 'en')
        self.assertEqual(result.preview['question_fields'][0]['label'], fields[0]['label'])
        self.assertIn('interface language is English', model.messages[0][0].content)

    def test_existing_feed_resume_and_context_reset_capture_current_language_without_editing_schedule(self):
        kb = KnowledgeBase.objects.create(name='Existing', slug='language-existing', is_folder=True, created_by=self.user)
        feed = KnowledgeFeed.objects.create(kb=kb, name='Existing feed', created_by=self.user,
            adapter_spec=spec(), url=spec()['url'], cron='0 6 * * *', timezone='Asia/Shanghai')
        original = KnowledgeFeed.objects.filter(pk=feed.pk).values().get()
        response = self.client.post(reverse('kb:connector_resume'), json.dumps({'feed': str(feed.pk)}),
            content_type='application/json', HTTP_X_INTERFACE_LANGUAGE='en')
        self.assertEqual(response.status_code, 200, response.content)
        root = ConnectorDraft.objects.get(pk=response.json()['thread'][-1]['id'])
        self.assertEqual(root.response_language, 'en')
        reset = self.client.post(reverse('kb:connector_clear_context'), json.dumps({'feed': str(feed.pk)}),
            content_type='application/json', HTTP_X_INTERFACE_LANGUAGE='zh-hans')
        self.assertEqual(reset.status_code, 200, reset.content)
        newest = ConnectorDraft.objects.filter(feed=feed).order_by('-created_at').first()
        self.assertEqual(newest.response_language, 'zh-hans')
        self.assertEqual(KnowledgeFeed.objects.filter(pk=feed.pk).values().get(), original)
