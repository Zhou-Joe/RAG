import io
import json
import zipfile
from pathlib import Path
from unittest.mock import patch
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase,TestCase
from kb.connector_runtime import build_batch
from kb.connector_views import package
from kb.feed_protocol import SCHEMA,validate_batch
from kb.feeds import claim,execute
from kb.models import KnowledgeBase,KnowledgeFeed,TrackerRow


def rows(count):
    return [{'id':str(i),'title':'Item '+str(i),'content':'Row '+str(i)} for i in range(count)]


def source_spec():
    return {'name':'Batch limit','url':'https://api.example.test/data','records_path':'data','id_paths':['id'],'title_path':'name','body_fields':[{'label':'Value','path':'value'}],'next_cursor_path':'next','on_error':'fail'}


def source_data(count):
    return {'data':[{'id':str(i),'name':'Item '+str(i),'value':i} for i in range(count)],'next':'page-2'}


class FeedBatchBoundaryTests(SimpleTestCase):
    def test_protocol_accepts_500_items_and_rejects_501(self):
        payload={'schema':SCHEMA,'items':rows(500)}
        self.assertEqual(len(validate_batch(payload)['items']),500)
        with self.assertRaisesMessage(ValueError,'500'):validate_batch({'schema':SCHEMA,'items':rows(501)})

    def test_500_limit_combines_writes_and_deletions(self):
        self.assertEqual(len(validate_batch({'schema':SCHEMA,'items':rows(499),'deleted':['removed']})['items']),499)
        self.assertEqual(len(validate_batch({'schema':SCHEMA,'items':[],'deleted':[str(i) for i in range(500)]})['deleted']),500)
        with self.assertRaisesMessage(ValueError,'500'):validate_batch({'schema':SCHEMA,'items':rows(500),'deleted':['removed']})

    def test_api_mapper_accepts_all_500_without_truncating(self):
        payload=validate_batch(build_batch(source_spec(),source_data(500)))
        self.assertEqual(len(payload['items']),500);self.assertEqual(payload['items'][-1]['id'],'499')
        self.assertEqual(payload['cursor'],'page-2')
        with self.assertRaisesMessage(ValueError,'500'):build_batch(source_spec(),source_data(501))

    def test_2mb_and_per_record_size_limits_are_still_enforced(self):
        payload={'schema':SCHEMA,'items':rows(500)}
        for item in payload['items']:item['content']='x'*5000
        with self.assertRaisesMessage(ValueError,'2MB'):validate_batch(payload)
        with self.assertRaisesMessage(ValueError,'100000'):validate_batch({'schema':SCHEMA,'items':[{'id':'1','title':'Long','content':'x'*100001}]})

    def test_published_schema_exposes_500_as_each_array_boundary(self):
        schema=json.loads((Path(settings.BASE_DIR)/'docs/schemas/knowledge-feed-v1.schema.json').read_text(encoding='utf-8'))
        self.assertEqual(schema['properties']['items']['maxItems'],500)
        self.assertEqual(schema['properties']['deleted']['maxItems'],500)

    def test_downloaded_standalone_runtime_supports_the_same_boundary(self):
        with zipfile.ZipFile(io.BytesIO(package(source_spec()))) as archive:code=archive.read('connector_runtime.py')
        portable={'__name__':'standalone_connector_runtime'};exec(compile(code,'connector_runtime.py','exec'),portable)
        payload=portable['build_batch'](source_spec(),source_data(500))
        self.assertEqual(len(payload['items']),500);self.assertEqual(payload['items'][-1]['id'],'499')
        with self.assertRaises(ValueError):portable['build_batch'](source_spec(),source_data(501))


class FeedBatchPublicationTests(TestCase):
    def setUp(self):
        user=get_user_model().objects.create_user('batch-owner',is_staff=True)
        kb=KnowledgeBase.objects.create(name='Large batch',slug='large-batch',is_folder=True,created_by=user)
        self.feed=KnowledgeFeed.objects.create(kb=kb,name='API',mode='pull',url=source_spec()['url'],adapter_spec=source_spec(),created_by=user,cursor='page-1')

    def test_api_collection_publishes_all_500_and_advances_cursor_once(self):
        run,_=claim(self.feed.pk,trigger='manual',force=True)
        reply=(200,{'content-type':'application/json'},json.dumps(source_data(500)).encode())
        with patch('kb.feed_http.read_url',return_value=reply):result=execute(run.pk)
        self.assertEqual(result.status,'completed',result.error);self.assertEqual(result.counts['created'],500)
        self.assertEqual(TrackerRow.objects.filter(feed=self.feed).count(),500)
        self.assertTrue(TrackerRow.objects.filter(feed=self.feed,external_id='499').exists())
        self.feed.refresh_from_db();self.assertEqual(self.feed.cursor,'page-2')

    def test_api_over_limit_batch_publishes_nothing_and_keeps_cursor(self):
        run,_=claim(self.feed.pk,trigger='manual',force=True)
        reply=(200,{'content-type':'application/json'},json.dumps(source_data(501)).encode())
        with patch('kb.feed_http.read_url',return_value=reply):result=execute(run.pk)
        self.assertEqual(result.status,'failed');self.assertIn('500',result.error)
        self.assertFalse(TrackerRow.objects.filter(feed=self.feed).exists())
        self.feed.refresh_from_db();self.assertEqual(self.feed.cursor,'page-1')
