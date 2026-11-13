import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from langchain_core.messages import AIMessageChunk

from kb.browser_runtime import BrowserSession, validate_plan, origin
from kb.connector_agent import run
from kb.connector_tools import api_origins
from kb.connector_views import package
from kb.credential_routing import environment_profiles, login_value, scoped_headers
from kb.feed_credentials import (authentication_headers, bindings, credential_profiles, export_profiles,
                                 request_headers, secret_values, store, summary, upsert_access)
from kb.feed_http import fetch_batch
from kb.models import ConnectorDraft, KnowledgeBase, KnowledgeFeed
from kb.tests.test_connector import FakeModel, call, sample, spec


API = 'https://api.example.test/reports'
PORTAL = 'https://portal.example.test/login'
KEY = 'synthetic-api-key-multiple-123'
PASSWORD = 'synthetic-portal-password-456'


def bundle():
    return {'type': 'multiple', 'profiles': [
        {'name': 'api', 'url': API, 'authentication': {'type': 'bearer', 'token': KEY}},
        {'name': 'portal', 'url': PORTAL, 'authentication': {'type': 'login', 'username': 'portal-user', 'password': PASSWORD}},
    ]}


def finish(coroutine):
    """These mocks complete immediately, so no real Windows socket loop is needed."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('Unexpected async suspension')


class CredentialRoutingTests(SimpleTestCase):
    def test_same_origin_layers_headers_but_rejects_conflicting_values(self):
        profiles = [{'name': 'one', 'url': API, 'headers': {'X-Key': 'one'}},
                    {'name': 'two', 'url': API, 'headers': {'X-App': 'two', 'x-key': 'one'}}]
        self.assertEqual(scoped_headers(profiles, API), {'X-Key': 'one', 'X-App': 'two'})
        for target in ('http://api.example.test', 'https://api.example.test:444', PORTAL):
            self.assertEqual(scoped_headers(profiles, target), {})
        profiles[1]['headers']['x-key'] = 'different'
        with self.assertRaises(ValueError):
            scoped_headers(profiles, API)

    def test_named_logins_are_scoped_and_unqualified_ambiguity_is_rejected(self):
        profiles = [{'name': name, 'url': PORTAL, 'login': {'username': name, 'password': name + '-password'}} for name in ['staff', 'admin']]
        self.assertEqual(login_value(profiles, PORTAL, 'staff.username'), 'staff')
        for target, reference in [(API, 'staff.password'), (PORTAL, 'password'), (PORTAL, 'missing.password')]:
            with self.assertRaises(ValueError):
                login_value(profiles, target, reference)
        plan = {'actions': [{'action': 'fill', 'selector': '#password', 'credential': 'staff.password'}],
                'output': {'kind': 'network', 'url_contains': '/data', 'path': 'items'}}
        self.assertEqual(validate_plan(plan)['actions'][0]['credential'], 'staff.password')

    def test_portable_environment_profiles_resolve_by_origin_without_saved_secrets(self):
        config = [{'name': 'api', 'url': API, 'header_env': {'Authorization': 'TEST_KEY'}, 'login_env': {}},
                  {'name': 'portal', 'url': PORTAL, 'header_env': {}, 'login_env': {'username': 'TEST_USER', 'password': 'TEST_PASS'}}]
        with patch.dict(os.environ, {'TEST_KEY': KEY, 'TEST_USER': 'user', 'TEST_PASS': PASSWORD}):
            profiles = environment_profiles(config)
        self.assertEqual(scoped_headers(profiles, API), {'Authorization': KEY})
        self.assertEqual(login_value(profiles, PORTAL, 'portal.password'), PASSWORD)
        with self.assertRaises(ValueError):
            environment_profiles([{'name': 123, 'url': API}])
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'Missing credential'):
            environment_profiles(config)

    def test_browser_routes_credentials_for_each_service_and_strips_forwarded_credentials(self):
        profiles = [{'name': 'api', 'url': API, 'headers': {'Authorization': 'Bearer ' + KEY}},
                    {'name': 'portal', 'url': PORTAL, 'headers': {'X-Portal': PASSWORD}}]
        browser = BrowserSession.__new__(BrowserSession)
        browser.credential_profiles = profiles
        browser.approved = {origin(API), origin(PORTAL)}
        browser.private_origins = frozenset(browser.approved)
        browser.headers = {'Authorization': 'Bearer ' + KEY}
        browser.credential_origin = origin(API)
        browser.allow_private = False
        browser.document_only = False
        browser.requests = browser.bytes = browser.pending_requests = browser.skipped_requests = 0
        browser.assets = {};browser.blocked = [];browser.asset_bytes = 0
        browser.fetcher = Mock(return_value=(200, {}, b'{}', []))
        async def immediate(function, *args, **kwargs):
            return function(*args, **kwargs)
        for target, expected in [(API, {'Authorization': 'Bearer ' + KEY}),
                                 (PORTAL, {'X-Portal': PASSWORD}), ('https://cdn.example.test/data', {})]:
            request = SimpleNamespace(url=target, method='GET', resource_type='fetch', post_data_buffer=None,
                                      all_headers=AsyncMock(return_value={'authorization': 'leaked', 'x-portal': 'leaked'}))
            route = SimpleNamespace(fulfill=AsyncMock(), abort=AsyncMock())
            with patch('kb.browser_runtime.asyncio.to_thread', immediate):
                finish(browser._route(route, request))
            self.assertEqual(browser.fetcher.call_args.args[2], expected)
            route.fulfill.assert_awaited_once()
            route.abort.assert_not_awaited()


class MultipleCredentialTests(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = override_settings(DATA_DIR=Path(folder.name))
        config.enable();self.addCleanup(config.disable)
        self.user = get_user_model().objects.create_user('multi-credential', is_staff=True)
        self.client.force_login(self.user)

    def resource(self, authentication=None):
        credential = store(self.user, API, authentication or bundle())
        return ConnectorDraft.objects.create(user=self.user, api_url=API, credential=credential,
                                             header_env=bindings(credential), prompt='Use ' + API + ' and ' + PORTAL)

    def test_storage_metadata_and_export_never_disclose_any_secret(self):
        job = self.resource()
        self.assertEqual(job.credential.kind, 'multiple')
        self.assertEqual(request_headers(job, API), {'Authorization': 'Bearer ' + KEY})
        self.assertEqual(request_headers(job, PORTAL), {})
        with self.assertRaises(ValueError):
            request_headers(job, 'https://unrelated.example.test')
        public = json.dumps([summary(job), export_profiles(job)])
        self.assertEqual(summary(job)['profile_count'], 2)
        for secret in (KEY, PASSWORD, 'portal-user'):
            self.assertNotIn(secret, public)
            self.assertNotIn(secret, job.credential.encrypted_value)
            self.assertIn(secret, secret_values(job))
        self.assertEqual(api_origins(job), {origin(API), origin(PORTAL)})
        job.header_env = {'Authorization': 'FOS_FEED_FAKE'}
        with self.assertRaises(ValueError):
            request_headers(job, API)

    def test_saved_profile_rotation_preserves_other_services_and_named_logins(self):
        job = self.resource()
        auth = bundle()
        auth['profiles'][0]['authentication'] = {'type': 'saved'}
        auth['profiles'][1]['authentication']['password'] = 'replacement-password'
        replacement = store(self.user, API, auth, previous=job.credential)
        job.credential = replacement;job.header_env = bindings(replacement)
        self.assertEqual(request_headers(job, API)['Authorization'], 'Bearer ' + KEY)
        self.assertEqual(login_value(credential_profiles(job), PORTAL, 'portal.password'), 'replacement-password')
        auth['profiles'][0]['url'] = PORTAL
        with self.assertRaises(ValueError):
            store(self.user, API, auth, previous=replacement)
        other = get_user_model().objects.create_user('multi-other', is_staff=True)
        with self.assertRaises(ValueError):
            store(other, API, bundle(), previous=replacement)

    def test_adding_a_service_preserves_legacy_credential_and_primary_url(self):
        job = self.resource({'type': 'bearer', 'token': KEY})
        job.credential = upsert_access(job, self.user, PORTAL, {'type': 'login', 'username': 'portal-user', 'password': PASSWORD}, name='portal')
        job.header_env = bindings(job.credential)
        self.assertEqual(job.api_url, API)
        self.assertEqual(request_headers(job, API), {'Authorization': 'Bearer ' + KEY})
        self.assertEqual(login_value(credential_profiles(job), PORTAL, 'portal.username'), 'portal-user')
        job.credential = upsert_access(job, self.user, PORTAL, {'type': 'none'}, name='portal')
        job.header_env = bindings(job.credential)
        self.assertEqual(len(credential_profiles(job)), 1)
        self.assertEqual(request_headers(job, API)['Authorization'], 'Bearer ' + KEY)
        self.assertIsNone(upsert_access(SimpleNamespace(credential=None), self.user, API, {'type': 'none'}, name='unused'))

    def test_conflicts_duplicates_nested_bundles_and_profile_limit_are_rejected(self):
        variants = []
        auth = bundle();auth['profiles'][1] = {'name': 'other', 'url': API, 'authentication': {'type': 'bearer', 'token': PASSWORD}};variants.append(auth)
        auth = bundle();auth['profiles'][1]['name'] = 'api';variants.append(auth)
        auth = bundle();auth['profiles'][0]['authentication'] = bundle();variants.append(auth)
        variants.extend([{'type': 'multiple', 'profiles': []}, {'type': 'multiple', 'profiles': bundle()['profiles'] * 5}])
        for auth in variants:
            with self.assertRaises(ValueError):
                store(self.user, API, auth)

    def test_rotation_endpoint_updates_scheduled_feed_and_reuses_other_saved_profile(self):
        job = self.resource()
        kb = KnowledgeBase.objects.create(name='Multiple', slug='multiple', is_folder=True, created_by=self.user)
        feed = KnowledgeFeed.objects.create(kb=kb, name='Multiple', created_by=self.user, url=API,
                                            credential=job.credential, header_env=job.header_env, adapter_spec=spec(header_env=job.header_env))
        job.feed = feed;job.kb = kb;job.status = 'enabled';job.spec = spec(header_env=job.header_env);job.save()
        auth = bundle();auth['profiles'][0]['authentication']['token'] = 'rotated-api-key'
        auth['profiles'][1]['authentication'] = {'type': 'saved'}
        response = self.client.post(reverse('kb:connector_credentials', args=[job.pk]),
                                    json.dumps({'api_url': API, 'authentication': auth}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        feed.refresh_from_db();job.refresh_from_db()
        self.assertEqual(feed.credential_id, job.credential_id)
        with patch('kb.feed_http.read_url', return_value=(200, {'content-type': 'application/json'}, json.dumps(sample()).encode())) as read:
            fetch_batch(feed)
        self.assertEqual(read.call_args.kwargs['headers'], {'Authorization': 'Bearer rotated-api-key'})
        self.assertEqual(login_value(credential_profiles(feed), PORTAL, 'portal.password'), PASSWORD)
        self.assertNotIn(PASSWORD, response.content.decode())

    def test_exported_archive_uses_per_profile_environment_references(self):
        job = self.resource()
        with zipfile.ZipFile(io.BytesIO(package(spec(header_env=job.header_env), job))) as archive:
            self.assertIn('credential_routing.py', archive.namelist())
            metadata = json.loads(archive.read('connector_credentials.json'))
            self.assertEqual([item['name'] for item in metadata], ['api', 'portal'])
            for name in archive.namelist():
                for secret in (KEY, PASSWORD):
                    self.assertNotIn(secret.encode(), archive.read(name))
            variables = {variable: 'value-' + str(i) for i, variable in enumerate(
                variable for item in metadata for key in ('header_env', 'login_env') for variable in item[key].values())}
            with patch.dict(os.environ, variables):
                profiles = environment_profiles(metadata)
            self.assertEqual(len(profiles), 2)
            self.assertEqual(scoped_headers(profiles, PORTAL), {})

    def test_exported_api_runtime_loads_its_profile_file_and_only_injects_the_api_key(self):
        job = self.resource()
        with tempfile.TemporaryDirectory() as directory:
            with zipfile.ZipFile(io.BytesIO(package(spec(header_env=job.header_env), job))) as archive:
                archive.extractall(directory)
            module_spec = importlib.util.spec_from_file_location('exported_profile_runtime', Path(directory)/'connector_runtime.py')
            runtime = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(runtime)
            metadata = json.loads((Path(directory)/'connector_credentials.json').read_text(encoding='utf-8'))
            environment = {}
            for profile in metadata:
                for header, variable in profile['header_env'].items():
                    environment[variable] = 'Bearer portable-api-key'
                for field, variable in profile['login_env'].items():
                    environment[variable] = 'portable-' + field
            response = Mock()
            response.read.return_value = json.dumps(sample()).encode()
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=response)
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch.dict(os.environ, environment), patch.dict(sys.modules), \
                    patch.object(sys, 'path', [directory, *sys.path]), \
                    patch.object(sys, 'argv', ['connector.py', '--preview', '--state-file', str(Path(directory)/'state.json')]), \
                    patch.object(runtime, 'build_opener', return_value=opener), patch('sys.stdout', new_callable=io.StringIO) as output:
                runtime.main(spec(header_env=job.header_env))
            self.assertEqual(opener.open.call_args.args[0].get_header('Authorization'), 'Bearer portable-api-key')
            self.assertNotIn('portable-password', str(opener.open.call_args))
            self.assertEqual(len(json.loads(output.getvalue())['items']), 1)

    def test_conversation_adds_service_without_replacing_primary_access(self):
        job = self.resource({'type': 'bearer', 'token': KEY})
        job.prompt = 'Use ' + API + ' and ' + PORTAL + ' with portal-user and ' + PASSWORD
        job.save(update_fields=['prompt'])
        model = FakeModel([call('set_source_access', {'url': PORTAL, 'name': 'portal',
                           'authentication': {'type': 'login', 'username': 'portal-user', 'password': PASSWORD}}),
                           AIMessageChunk(content='Both services are configured.')])
        with patch('kb.agent._get_llm', return_value=model), patch('kb.config.llm_settings', return_value={}):
            result = run(job.pk)
        self.assertEqual(result.status, 'answered', result.error)
        self.assertEqual(result.api_url, API)
        self.assertEqual(request_headers(result, API)['Authorization'], 'Bearer ' + KEY)
        self.assertEqual(login_value(credential_profiles(result), PORTAL, 'portal.password'), PASSWORD)
        for secret in (KEY, PASSWORD):
            self.assertNotIn(secret, json.dumps(result.events))
            self.assertNotIn(secret, result.prompt)

    def test_template_has_a_bounded_nonrecursive_profile_editor(self):
        html = render_to_string('kb/_feed_auth_fields.html')
        self.assertEqual(html.count('data-auth-profile-template'), 1)
        self.assertEqual(html.count('value="multiple"'), 1)
        self.assertEqual(html.count('data-auth-widget'), 2)
