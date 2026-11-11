import io
import json
import zipfile
from pathlib import Path
from unittest.mock import patch
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase,TestCase,override_settings
from django.urls import reverse
from kb.connector_runtime import build_batch
from kb.connector_views import package
from kb.feed_protocol import SCHEMA,MAX_ITEMS,MAX_BYTES,canonical,load_batch,validate_batch
from kb.feeds import claim,execute,rotate_token
from kb.models import KnowledgeBase,KnowledgeFeed,TrackerRow,FeedRun


def rows(count):
    return [{'id':str(i),'title':'Item '+str(i),'content':'Row '+str(i)} for i in range(count)]


def source_spec():
    return {'name':'Batch limit','url':'https://api.example.test/data','records_path':'data','id_paths':['id'],'title_path':'name','body_fields':[{'label':'Value','path':'value'}],'next_cursor_path':'next','on_error':'fail'}


def source_data(count):
    return {'data':[{'id':str(i),'name':'Item '+str(i),'value':i} for i in range(count)],'next':'page-2'}


class FeedBatchBoundaryTests(SimpleTestCase):
    def test_protocol_accepts_50000_items_and_rejects_50001(self):
        self.assertEqual(MAX_ITEMS,50000)
        payload={'schema':SCHEMA,'items':rows(MAX_ITEMS)}
        self.assertEqual(len(validate_batch(payload)['items']),MAX_ITEMS)
        with self.assertRaisesMessage(ValueError,'50000'):validate_batch({'schema':SCHEMA,'items':rows(MAX_ITEMS+1)})

    def test_limit_combines_writes_and_deletions(self):
        self.assertEqual(len(validate_batch({'schema':SCHEMA,'items':rows(MAX_ITEMS-1),'deleted':['removed']})['items']),MAX_ITEMS-1)
        self.assertEqual(len(validate_batch({'schema':SCHEMA,'items':[],'deleted':[str(i) for i in range(MAX_ITEMS)]})['deleted']),MAX_ITEMS)
        with self.assertRaisesMessage(ValueError,'50000'):validate_batch({'schema':SCHEMA,'items':rows(MAX_ITEMS),'deleted':['removed']})

    def test_api_mapper_accepts_all_50000_without_truncating(self):
        payload=validate_batch(build_batch(source_spec(),source_data(MAX_ITEMS)))
        self.assertEqual(len(payload['items']),MAX_ITEMS);self.assertEqual(payload['items'][-1]['id'],str(MAX_ITEMS-1))
        self.assertEqual(payload['cursor'],'page-2')
        with self.assertRaisesMessage(ValueError,'50000'):build_batch(source_spec(),source_data(MAX_ITEMS+1))

    def test_larger_payloads_work_and_byte_and_record_limits_stay_enforced(self):
        self.assertEqual(MAX_BYTES,500_000_000)
        payload={'schema':SCHEMA,'items':rows(500)}
        for item in payload['items']:item['content']='x'*5000
        raw=canonical(payload).encode()
        self.assertGreater(len(raw),2_000_000)
        self.assertEqual(len(load_batch(raw)['items']),500)
        # Exercise both bounded entry points without allocating 500 MB in a test.
        with patch('kb.feed_protocol.MAX_BYTES',len(raw)):
            self.assertEqual(len(load_batch(raw)['items']),500)
        with patch('kb.feed_protocol.MAX_BYTES',len(raw)-1):
            with self.assertRaisesMessage(ValueError,'500MB'):load_batch(raw)
            with self.assertRaisesMessage(ValueError,'500MB'):validate_batch(payload)
        with self.assertRaisesMessage(ValueError,'100000'):validate_batch({'schema':SCHEMA,'items':[{'id':'1','title':'Long','content':'x'*100001}]})

    def test_published_schema_exposes_current_array_boundary(self):
        schema=json.loads((Path(settings.BASE_DIR)/'docs/schemas/knowledge-feed-v1.schema.json').read_text(encoding='utf-8'))
        self.assertEqual(schema['properties']['items']['maxItems'],MAX_ITEMS)
        self.assertEqual(schema['properties']['deleted']['maxItems'],MAX_ITEMS)

    def test_downloaded_standalone_runtime_supports_the_same_boundary(self):
        with zipfile.ZipFile(io.BytesIO(package(source_spec()))) as archive:
            code=archive.read('connector_runtime.py')
            client_code=archive.read('feed_client.py')
            self.assertIn(b'50000',archive.read('README.md'))
        portable={'__name__':'standalone_connector_runtime'};exec(compile(code,'connector_runtime.py','exec'),portable)
        self.assertEqual(portable['MAX_BYTES'],MAX_BYTES)
        payload=portable['build_batch'](source_spec(),source_data(1001))
        self.assertEqual(len(payload['items']),1001);self.assertEqual(payload['items'][-1]['id'],'1000')
        with self.assertRaises(ValueError):portable['build_batch'](source_spec(),source_data(MAX_ITEMS+1))
        client={'__name__':'standalone_feed_client'};exec(compile(client_code,'feed_client.py','exec'),client)
        self.assertEqual(client['MAX_BYTES'],MAX_BYTES)


class FeedBatchPublicationTests(TestCase):
    def setUp(self):
        user=get_user_model().objects.create_user('batch-owner',is_staff=True)
        kb=KnowledgeBase.objects.create(name='Large batch',slug='large-batch',is_folder=True,created_by=user)
        self.feed=KnowledgeFeed.objects.create(kb=kb,name='API',mode='pull',url=source_spec()['url'],adapter_spec=source_spec(),created_by=user,cursor='page-1')

    def test_api_collection_publishes_more_than_old_limit_and_advances_cursor_once(self):
        run,_=claim(self.feed.pk,trigger='manual',force=True)
        reply=(200,{'content-type':'application/json'},json.dumps(source_data(1001)).encode())
        with patch('kb.feed_http.read_url',return_value=reply):result=execute(run.pk)
        self.assertEqual(result.status,'completed',result.error);self.assertEqual(result.counts['created'],1001)
        self.assertEqual(TrackerRow.objects.filter(feed=self.feed).count(),1001)
        self.assertTrue(TrackerRow.objects.filter(feed=self.feed,external_id='1000').exists())
        self.feed.refresh_from_db();self.assertEqual(self.feed.cursor,'page-2')

    def test_api_over_limit_batch_publishes_nothing_and_keeps_cursor(self):
        run,_=claim(self.feed.pk,trigger='manual',force=True)
        reply=(200,{'content-type':'application/json'},json.dumps(source_data(MAX_ITEMS+1)).encode())
        with patch('kb.feed_http.read_url',return_value=reply):result=execute(run.pk)
        self.assertEqual(result.status,'failed');self.assertIn('50000',result.error)
        self.assertFalse(TrackerRow.objects.filter(feed=self.feed).exists())
        self.feed.refresh_from_db();self.assertEqual(self.feed.cursor,'page-1')

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1024)
    def test_authenticated_feed_push_uses_feed_limit_instead_of_generic_form_limit(self):
        self.feed.mode='push';self.feed.save(update_fields=['mode'])
        token=rotate_token(self.feed)
        payload={'schema':SCHEMA,'items':rows(50)}
        for item in payload['items']:item['content']='x'*60000
        raw=canonical(payload)
        self.assertGreater(len(raw),2_621_440)
        with patch('kb.feed_views.start_async'):
            response=self.client.post(reverse('kb:feed_ingest',args=[self.feed.pk]),raw,
                content_type='application/json',HTTP_AUTHORIZATION='Bearer '+token)
        self.assertEqual(response.status_code,202,response.content)
        run=FeedRun.objects.get(pk=response.json()['run']['id'])
        self.assertEqual(run.payload['items'][-1]['content'],'x'*60000)
