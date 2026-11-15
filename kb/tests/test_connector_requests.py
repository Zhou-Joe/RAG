"""GET/POST requests replay identically in preview, cron and exported collectors."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
import zipfile
import httpx

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from langchain_core.messages import AIMessageChunk, ToolMessage

from kb import collector_api
from kb.connector_agent import run
from kb.connector_runtime import request_options, request_url, validate_spec
from kb.connector_tools import redact
from kb.connector_views import package
from kb.feed_http import fetch_batch, read_url
from kb.tests.test_connector import FakeModel, call, sample, spec

URL = 'https://api.example.test/reports'


class RequestConfigurationTests(SimpleTestCase):
    def test_legacy_get_and_post_json_form_requests_preserve_their_parameters(self):
        self.assertEqual(request_options(spec()), {})
        self.assertEqual(parse_qs(urlsplit(request_url(spec(), 'next')).query)['after'], ['next'])
        body = {'range':{'start':'2026-11-01'}, 'limit':0, 'after':''}
        config = spec(method='POST', json_body=body, query={'format':'full'}, cursor_location='body')
        self.assertIs(validate_spec(config), config)
        options = request_options(config, 'committed')
        self.assertEqual(options['method'], 'POST')
        self.assertEqual(options['json_body']['after'], 'committed')
        self.assertEqual(body['after'], '')
        self.assertNotIn('after', parse_qs(urlsplit(request_url(config, 'committed')).query))
        form = spec(method='POST', form_body={'ride':['one', 'two'], 'date':'2026-11-14'})
        self.assertEqual(request_options(form)['form_body'], form['form_body'])
        self.assertEqual(request_options(spec(method='POST', json_body=[]))['json_body'], [])

    def test_invalid_methods_bodies_and_plaintext_credentials_are_rejected(self):
        for extra in (
            {'method':'DELETE'}, {'json_body':{}}, {'method':'POST', 'json_body':{}, 'form_body':{}},
            {'method':'POST', 'json_body':'encoded'}, {'method':'POST', 'form_body':{'nested':{'x':1}}},
            {'method':'POST', 'json_body':{'bad':float('nan')}},
            {'method':'POST', 'json_body':{'password':'do-not-store-plaintext'}},
            {'method':'POST', 'form_body':{'password':'[REDACTED]'}},
            {'method':'POST', 'json_body':{'value':'[SOURCE_SECRET:' + 'a' * 32 + ']'}},
            {'cursor_location':'body'}, {'method':'POST', 'json_body':['row'], 'cursor_location':'body'},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):validate_spec(spec(**extra))

    def test_body_credential_references_are_resolved_for_the_matching_service_only(self):
        profiles = [{'name':'portal', 'url':URL, 'login':{'username':'demo-user', 'password':'synthetic-body-password'}}]
        body = {'username':{'credential':'portal.username'}, 'password':{'credential':'portal.password'}}
        config = spec(method='POST', json_body=body)
        self.assertEqual(request_options(config, profiles=profiles)['json_body'],
            {'username':'demo-user', 'password':'synthetic-body-password'})
        self.assertEqual(redact(config, ['synthetic-body-password'])['json_body'], body)
        with self.assertRaises(ValueError):request_options(config)
        with self.assertRaises(ValueError):request_options({**config, 'url':'https://other.example.test'}, profiles=profiles)

    def test_exported_standard_library_collector_replays_post_json_and_form_bodies(self):
        for field, body in [('json_body', {'range':{'days':3}, 'after':''}),
                            ('form_body', {'ride':['one', 'two'], 'after':''})]:
            config = spec(method='POST', cursor_location='body', **{field:body})
            with self.subTest(field=field), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                with zipfile.ZipFile(io.BytesIO(package(config))) as archive:
                    self.assertIn('POST', archive.read('README.md').decode())
                    self.assertEqual(json.loads(archive.read('connector.json'))['method'], 'POST')
                    for name in archive.namelist():self.assertTrue((root/name).resolve().is_relative_to(root))
                    archive.extractall(root)
                module_spec = importlib.util.spec_from_file_location('portable_post_runtime', root/'connector_runtime.py')
                runtime = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(runtime)
                state = root/'state.json';state.write_text(json.dumps({'cursor':'committed-previous'}))
                response = Mock();response.read.return_value = json.dumps(sample()).encode()
                opener = Mock();opener.open.return_value.__enter__ = Mock(return_value=response)
                opener.open.return_value.__exit__ = Mock(return_value=False)
                with patch.dict(os.environ, {}, clear=True), patch.object(sys,'argv', ['connector.py','--preview','--state-file',str(state)]), \
                        patch.object(runtime,'build_opener',return_value=opener), patch('sys.stdout',new_callable=io.StringIO):
                    runtime.main(config)
                request = opener.open.call_args.args[0]
                self.assertEqual(request.get_method(), 'POST')
                self.assertNotIn('after', parse_qs(urlsplit(request.full_url).query))
                actual = json.loads(request.data) if field=='json_body' else parse_qs(request.data.decode())
                self.assertEqual(actual['after'], 'committed-previous' if field=='json_body' else ['committed-previous'])
                self.assertEqual(json.loads(state.read_text()), {'cursor':'committed-previous'})


class RequestTransportTests(SimpleTestCase):
    def test_http_transport_encodes_json_and_form_and_preserves_the_pinned_host(self):
        original_client=httpx.Client
        requests=[]
        def respond(request):
            requests.append(request)
            return httpx.Response(200,headers={'content-type':'application/json'},stream=httpx.ByteStream(b'{"records":[]}'))
        transport=httpx.MockTransport(respond)
        def client(**kwargs):return original_client(transport=transport,**kwargs)
        with patch('kb.feed_http.resolve_target',return_value=('api.example.test','93.184.216.34')), \
                patch('kb.feed_http.httpx.Client',side_effect=client):
            self.assertEqual(read_url(URL,method='POST',json_body={'days':7})[0],200)
            self.assertEqual(read_url(URL,method='POST',form_body={'ride':['one','two'],'date':'2026-11-14'})[0],200)
            self.assertEqual(read_url(URL)[0],200)
        self.assertEqual(requests[0].method,'POST')
        self.assertEqual(json.loads(requests[0].content),{'days':7})
        self.assertEqual(requests[0].headers['content-type'],'application/json')
        self.assertEqual(parse_qs(requests[1].content.decode()),{'ride':['one','two'],'date':['2026-11-14']})
        self.assertEqual(requests[1].headers['content-type'],'application/x-www-form-urlencoded')
        self.assertEqual(requests[2].method,'GET');self.assertEqual(requests[2].content,b'')
        for request in requests:
            self.assertEqual(request.url.host,'93.184.216.34')
            self.assertEqual(request.headers['host'],'api.example.test')
            self.assertEqual(request.extensions['sni_hostname'],'api.example.test')


class BuilderRequestTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory();self.addCleanup(temporary.cleanup)
        config = override_settings(DATA_DIR=Path(temporary.name));config.enable();self.addCleanup(config.disable)
        self.owner = get_user_model().objects.create_user('post-builder-owner', is_staff=True)
        starter = patch('kb.collector_api.start_async');starter.start();self.addCleanup(starter.stop)

    def build(self, config, *, authentication=None, actual_probe=None):
        job = collector_api.create_draft(user=self.owner, requirement='Read records from '+URL,
            api_url=URL, authentication=authentication or {'type':'none'})
        config = {**config, 'header_env':job.header_env}
        probe = actual_probe or {'url':URL, 'method':config.get('method','GET'),
            **{key:config[key] for key in ('query','json_body','form_body') if key in config}}
        model = FakeModel([call('probe_api', probe), call('show_data_preview', {'spec':config}, 'preview'),
                           AIMessageChunk(content='Please check the request configuration.')])
        with patch('kb.agent._get_llm',return_value=model), patch('kb.config.llm_settings',return_value={}), \
                patch('kb.connector_agent.read_url',return_value=(200,{'content-type':'application/json'},json.dumps(sample()).encode())) as read:
            result = run(job.pk)
        return result, model, read

    def test_post_preview_approval_and_cron_use_the_same_method_and_body(self):
        body = {'range':{'days':7}, 'limit':10, 'after':''}
        result, _, read = self.build(spec(method='POST', json_body=body, cursor_location='body'),
            authentication={'type':'bearer','token':'synthetic-post-key'},
            actual_probe={'url':URL,'method':'POST','json_body':json.dumps(body)})
        self.assertEqual(result.status, 'ready', result.error)
        self.assertEqual(read.call_args.kwargs['method'], 'POST')
        self.assertEqual(read.call_args.kwargs['json_body'], body)
        self.assertEqual(read.call_args.kwargs['headers']['Authorization'], 'Bearer synthetic-post-key')
        feed = collector_api.approve(user=self.owner,job=result,run_now=False)
        self.assertEqual(feed.adapter_spec['json_body'], body)
        self.assertEqual(feed.adapter_spec['method'], 'POST')
        feed.cursor = 'previous-committed'
        with patch('kb.feed_http.read_url',return_value=(200,{'content-type':'application/json'},json.dumps(sample()).encode())) as scheduled:
            batch, _ = fetch_batch(feed)
        self.assertEqual(len(batch['items']), 1)
        self.assertEqual(scheduled.call_args.kwargs['method'], 'POST')
        self.assertEqual(scheduled.call_args.kwargs['json_body']['after'], 'previous-committed')
        self.assertNotIn('after', parse_qs(urlsplit(scheduled.call_args.args[0]).query))
        self.assertEqual(feed.adapter_spec['json_body']['after'], '')

    def test_get_form_or_changed_json_cannot_reuse_a_different_post_sample(self):
        for config in (spec(), spec(method='POST', json_body={'days':2}),
                       spec(method='POST', form_body={'days':'1'})):
            result, model, read = self.build(config,
                actual_probe={'url':URL, 'method':'POST', 'json_body':{'days':1}})
            self.assertNotEqual(result.status, 'ready')
            errors = [message.content for messages in model.messages for message in messages if isinstance(message,ToolMessage)]
            self.assertTrue(any('请求方式或请求体' in message for message in errors))
            self.assertEqual(read.call_count, 1)

    def test_saved_login_values_reach_post_body_without_entering_model_or_export(self):
        password = 'synthetic-post-password'
        body = {'username':{'credential':'username'}, 'password':{'credential':'password'}}
        result, model, read = self.build(spec(method='POST',json_body=body),
            authentication={'type':'login','username':'demo-user','password':password})
        self.assertEqual(result.status, 'ready', result.error)
        self.assertEqual(read.call_args.kwargs['json_body'], {'username':'demo-user','password':password})
        self.assertNotIn(password, str(model.messages))
        self.assertEqual(result.spec['json_body'], body)
        feed=collector_api.approve(user=self.owner,job=result,run_now=False)
        with patch('kb.feed_http.read_url',return_value=(200,{'content-type':'application/json'},json.dumps(sample()).encode())) as scheduled:
            fetch_batch(feed)
        self.assertEqual(scheduled.call_args.kwargs['json_body'],{'username':'demo-user','password':password})
        with zipfile.ZipFile(io.BytesIO(package(result.spec,result))) as archive:
            self.assertIn('connector_credentials.json', archive.namelist())
            for name in archive.namelist():self.assertNotIn(password.encode(), archive.read(name))

    def test_post_form_preview_and_managed_collection_preserve_form_encoding(self):
        body = {'ride':['one','two'], 'date':'2026-11-14'}
        result, _, read = self.build(spec(method='POST',form_body=body))
        self.assertEqual(result.status, 'ready', result.error)
        self.assertEqual(read.call_args.kwargs['form_body'], body)
        resource = SimpleNamespace(adapter_spec=result.spec, cursor='', credential=None, header_env={}, allow_private=False)
        with patch('kb.feed_http.read_url',return_value=(200,{'content-type':'application/json'},json.dumps(sample()).encode())) as scheduled:
            fetch_batch(resource)
        self.assertEqual(scheduled.call_args.kwargs['form_body'], body)
