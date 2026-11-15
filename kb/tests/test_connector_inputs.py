"""Credential inputs survive question forms, masking and resumed builder turns."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from langchain_core.messages import AIMessageChunk

from kb import collector_api, connector_sessions
from kb.connector_agent import run
from kb.connector_inputs import SECRET_REF, protect_inputs, resolve_authentication
from kb.connector_storage import input_secrets, load
from kb.connector_tools import redact
from kb.credential_routing import login_value
from kb.feed_credentials import credential_profiles, request_headers
from kb.feed_reports import question_fields
from kb.models import ConnectorDraft
from kb.tests.test_connector import FakeModel, call, sample, spec

URL = 'https://api.example.test/reports'
TOKEN = 'synthetic-builder-token-12345'
PASSWORD = '  synthetic password with spaces  '


class InputProtectionTests(SimpleTestCase):
    def test_bearer_input_is_replaced_with_a_usable_reference_before_masking(self):
        text, values = protect_inputs('Collect ' + URL + ' using Authorization: Bearer ' + TOKEN)
        self.assertNotIn(TOKEN, text)
        ref = SECRET_REF.findall(text)[0]
        self.assertEqual(values[ref]['value'], TOKEN)
        self.assertIn(ref, redact(text, [TOKEN]))

    def test_english_chinese_json_and_quoted_values_remain_distinct_and_intact(self):
        for text in ('账号：portal-user\n密码："  synthetic password with spaces  "',
                     '{"username":"portal-user","password":"  synthetic password with spaces  "}'):
            safe, values = protect_inputs(text, urls=[URL])
            self.assertEqual({item['value'] for item in values.values()}, {'portal-user', PASSWORD})
            self.assertNotIn('portal-user', safe)
            self.assertNotIn(PASSWORD, safe)
            again, new_values = protect_inputs(safe, urls=[URL])
            self.assertEqual(again, safe)
            self.assertEqual(new_values, {})

    def test_short_secrets_do_not_destroy_references_during_redaction(self):
        safe, values = protect_inputs('username: a\npassword: b', urls=[URL])
        references = SECRET_REF.findall(safe)
        self.assertEqual(len(references), 2)
        cleaned = redact(safe, [entry['value'] for entry in values.values()])
        self.assertEqual(SECRET_REF.findall(cleaned), references)

    def test_json_escaped_password_is_preserved_as_the_actual_input_value(self):
        password='synthetic-quote"-and-backslash\\-password'
        safe, values=protect_inputs(json.dumps({'password':password}),urls=[URL])
        self.assertEqual(values[SECRET_REF.findall(safe)[0]]['value'],password)
        self.assertEqual(json.loads(safe)['password'],SECRET_REF.findall(safe)[0])

    def test_structured_short_username_does_not_corrupt_the_source_url_or_other_references(self):
        fields=question_fields([{'key':'username','label':'Username'},{'key':'password','label':'Password'}])
        safe, values=protect_inputs('Source: '+URL+'\nUsername: a\nPassword: b',fields=fields,
            answers=[{'key':'username','value':'a'},{'key':'password','value':'b'}])
        self.assertIn(URL,safe)
        self.assertEqual(set(SECRET_REF.findall(safe)),set(values))

    def test_a_business_username_filter_is_not_treated_as_a_login_secret(self):
        text='Find records where username: alice'
        self.assertEqual(protect_inputs(text), (text,{}))

    def test_bearer_keyword_inside_a_token_answer_is_not_saved_as_the_token(self):
        for text in ('token: Bearer ' + TOKEN, '{"token":"Bearer ' + TOKEN + '"}'):
            safe, values = protect_inputs(text, urls=[URL])
            refs = SECRET_REF.findall(safe)
            self.assertEqual(len(refs), 1)
            self.assertIn(values[refs[0]]['value'], (TOKEN, 'Bearer ' + TOKEN))
            self.assertNotIn(TOKEN, safe)

    def test_password_and_api_key_questions_are_masked_inputs(self):
        fields = question_fields([
            {'key':'username', 'label':'Username'},
            {'key':'password', 'label':'Password'},
            {'key':'api_key', 'label':'API Key'},
            {'key':'source', 'label':'Source URL', 'type':'url'},
        ])
        self.assertEqual(fields[0]['type'], 'text')
        self.assertTrue(fields[0]['secret'])
        self.assertEqual([field['type'] for field in fields[1:3]], ['password', 'password'])
        self.assertNotIn('secret', fields[3])
        self.assertEqual(question_fields([{'key':'access', 'label':'Secret', 'type':'password'}])[0]['type'], 'password')


class BuilderCredentialInputTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = override_settings(DATA_DIR=Path(temporary.name))
        settings.enable()
        self.addCleanup(settings.disable)
        self.owner = get_user_model().objects.create_user('builder-input-owner', is_staff=True)
        self.client.force_login(self.owner)
        starter = patch('kb.collector_api.start_async')
        starter.start()
        self.addCleanup(starter.stop)

    def create(self, text, **extra):
        return collector_api.create_draft(user=self.owner, requirement=text, conversation=True, **extra)

    def model_run(self, job, responses):
        model = FakeModel(responses)
        with patch('kb.agent._get_llm', return_value=model), patch('kb.config.llm_settings', return_value={}):
            result = run(job.pk)
        return result, model

    def test_chat_bearer_reaches_the_real_request_but_not_model_or_status(self):
        job = self.create('Collect ' + URL + ' using Authorization: Bearer ' + TOKEN)
        ref = SECRET_REF.findall(job.prompt)[0]
        self.assertIsNone(load(job.pk, self.owner.pk))  # Input-only storage is not a model checkpoint.
        with patch('kb.connector_agent.read_url', return_value=(200, {'content-type':'application/json'}, json.dumps(sample()).encode())) as read:
            result, model = self.model_run(job, [
                call('set_source_access', {'url':URL, 'authentication':{'type':'bearer', 'token':ref}}),
                call('probe_api', {'url':URL}, 'probe'), AIMessageChunk(content='Access is ready.'),
            ])
        self.assertEqual(result.status, 'answered', result.error)
        self.assertEqual(read.call_args.kwargs['headers']['Authorization'], 'Bearer ' + TOKEN)
        self.assertNotIn(TOKEN, str(model.messages))
        self.assertNotIn(TOKEN, result.context_snapshot)
        response = self.client.get(reverse('kb:connector_status', args=[result.pk]))
        self.assertNotIn(TOKEN, response.content.decode())
        self.assertEqual(input_secrets(result)[ref]['value'], TOKEN)

    def test_question_form_preserves_exact_password_and_can_bind_it_on_the_next_turn(self):
        parent = self.create('Read the login page at ' + URL)
        parent.status = 'needs_info'
        parent.preview = {'question_fields':question_fields([
            {'key':'username', 'label':'Username'}, {'key':'password', 'label':'Password', 'type':'password'},
        ])}
        parent.save()
        child = collector_api.refine_draft(user=self.owner, job=parent,
            requirement='Username: [Access credentials provided]\nPassword: [Access credentials provided]',
            question_answers=[{'key':'username', 'value':'portal-user'}, {'key':'password', 'value':PASSWORD}])
        values = input_secrets(child)
        refs = {item['field']:ref for ref, item in values.items()}
        result, model = self.model_run(child, [call('set_source_access', {'url':URL,
            'authentication':{'type':'login', 'username':refs['username'], 'password':refs['password']}}),
            AIMessageChunk(content='The saved login is ready.')])
        self.assertEqual(result.status, 'answered', result.error)
        self.assertEqual(login_value(credential_profiles(result), URL, 'password'), PASSWORD)
        self.assertNotIn(PASSWORD, child.feedback)
        self.assertNotIn(PASSWORD, str(model.messages))

    def test_resumed_turn_refreshes_saved_access_and_can_reuse_an_ancestor_input_reference(self):
        parent = self.create('Collect ' + URL + ' using Bearer ' + TOKEN)
        ref = SECRET_REF.findall(parent.prompt)[0]
        parent, _ = self.model_run(parent, [call('set_source_access', {'url':URL,
            'authentication':{'type':'bearer', 'token':ref}}), AIMessageChunk(content='Saved.')])
        connector_sessions.take(parent.pk, self.owner.pk)
        child = collector_api.refine_draft(user=self.owner, job=parent, requirement='Continue using my saved API access.')
        result, model = self.model_run(child, [call('set_source_access', {'url':URL,
            'authentication':{'type':'bearer', 'token':ref}}), AIMessageChunk(content='The saved access is available.')])
        self.assertEqual(result.status, 'answered', result.error)
        self.assertEqual(result.metrics['context_source'], 'saved')
        latest = model.messages[0][-1].content
        self.assertIn('"configured": true', latest)
        self.assertIn('"Authorization"', latest)
        self.assertEqual(request_headers(result, URL)['Authorization'], 'Bearer ' + TOKEN)
        self.assertNotIn(TOKEN, str(model.messages))

    def test_secret_references_are_scoped_to_conversation_owner_and_service(self):
        job = self.create('Collect ' + URL + ' using Bearer ' + TOKEN)
        ref = SECRET_REF.findall(job.prompt)[0]
        auth = {'type':'bearer', 'token':ref}
        with self.assertRaises(ValueError):resolve_authentication(job, 'https://other.example.test/', auth)
        job.status='answered';job.save(update_fields=['status'])
        unrelated = self.create('Another source at ' + URL)
        with self.assertRaises(ValueError):resolve_authentication(unrelated, URL, auth)
        attacker = get_user_model().objects.create_user('builder-input-other', is_staff=True)
        child = ConnectorDraft.objects.create(user=attacker, parent=job, prompt=ref)
        with self.assertRaises(ValueError):resolve_authentication(child, URL, auth)
