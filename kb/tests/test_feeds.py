from __future__ import annotations

import io
import json
import os
import tempfile
import uuid
import zipfile
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime,timedelta,timezone as dt_timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

import httpx
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase,TestCase
from django.urls import reverse
from django.utils import timezone

from kb.feed_cron import CronSchedule
from kb.feed_http import fetch_batch,resolve_target,validate_headers
from kb.feed_protocol import SCHEMA,load_batch,validate_batch
from kb.feeds import FeedBusy,claim,execute,rotate_token,run_due
from kb.models import Document,FeedRecord,FeedRun,KnowledgeBase,KnowledgeFeed,KbTracker,TrackerRow


def batch(content='Operating report: pressure 12 MPa.',key='report-1',**fields):
    return {'schema':SCHEMA,'items':[{'id':key,'title':'Daily report','content':content}],**fields}


class CronTests(SimpleTestCase):
    def test_five_fields_names_ranges_steps_and_sunday_alias(self):
        schedule=CronSchedule('*/15 8-10 * jan-mar mon-fri','UTC')
        self.assertTrue(schedule.matches(datetime(2027,1,4,8,30,tzinfo=dt_timezone.utc)))
        self.assertFalse(schedule.matches(datetime(2027,1,3,8,30,tzinfo=dt_timezone.utc)))
        self.assertEqual(CronSchedule('0 8 * * 7','UTC').dow,{0})

    def test_restricted_day_fields_use_or_but_star_uses_and(self):
        self.assertTrue(CronSchedule('0 8 1 * mon','UTC').matches(datetime(2027,1,4,8,tzinfo=dt_timezone.utc)))
        self.assertFalse(CronSchedule('0 8 * * mon','UTC').matches(datetime(2027,1,1,8,tzinfo=dt_timezone.utc)))

    def test_timezone_and_next_time_are_utc_aware(self):
        start=datetime(2027,1,1,23,59,tzinfo=dt_timezone.utc)
        self.assertEqual(CronSchedule('0 8 * * *','Asia/Shanghai').next_after(start),datetime(2027,1,2,0,tzinfo=dt_timezone.utc))

    def test_dst_gap_is_skipped_and_folds_have_two_utc_slots(self):
        gap=CronSchedule('30 2 * * *','America/New_York').next_after(datetime(2027,3,14,6,tzinfo=dt_timezone.utc))
        self.assertEqual(gap,datetime(2027,3,15,6,30,tzinfo=dt_timezone.utc))
        schedule=CronSchedule('30 1 * * *','America/New_York')
        first=schedule.next_after(datetime(2027,11,7,4,tzinfo=dt_timezone.utc));second=schedule.next_after(first)
        self.assertEqual(second-first,timedelta(hours=1))

    def test_invalid_schedules_and_impossible_dates_fail(self):
        for expression in ['* * * *','0 25 * * *','*/0 * * * *','1/2 * * * *','0 0 * * fri-mon','@reboot','* * * * * curl http://x']:
            with self.subTest(expression=expression),self.assertRaises(ValueError):CronSchedule(expression)
        with self.assertRaises(ValueError):CronSchedule('0 0 31 apr *').next_after(datetime(2027,1,1,tzinfo=dt_timezone.utc))
        with self.assertRaises(ValueError):CronSchedule('* * * * *','invalid/zone')


class ProtocolTests(SimpleTestCase):
    def test_contract_and_sample_file_validate(self):
        self.assertEqual(validate_batch(batch())['items'][0]['id'],'report-1')
        raw=(Path(__file__).resolve().parents[2]/'scripts/connectors/example_feed.json').read_bytes()
        self.assertEqual(load_batch(raw)['schema'],SCHEMA)

    def test_duplicate_ids_and_implicit_snapshot_deletion_are_rejected(self):
        for payload in [batch(deleted=['report-1']),{**batch(),'mode':'snapshot'},batch(items='wrong'),{'schema':'v99','items':[]}]:
            with self.assertRaises(ValueError):validate_batch(payload)

    def test_bounds_and_nonfinite_json_fail(self):
        with self.assertRaises(ValueError):load_batch(b'{}'*(1_000_001))
        with self.assertRaises(ValueError):load_batch(b'{"schema":"fos-rag-feed/v1","items":[],"cursor":NaN}')
        with self.assertRaises(ValueError):validate_batch(batch(content='x'*100001))
        with self.assertRaises(ValueError):validate_batch({'schema':SCHEMA,'items':[],'deleted':[str(i) for i in range(501)]})

    def test_bad_urls_empty_reports_and_naive_timestamps_fail(self):
        for field,value in [('source_url','javascript:alert(1)'),('source_url','https://user:secret@example.com/x'),('updated_at','2027-01-01T08:00:00'),('content','   ')]:
            payload=batch();payload['items'][0][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):validate_batch(payload)


class FeedHttpTests(SimpleTestCase):
    def test_private_dns_is_opt_in_and_metadata_remains_blocked(self):
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',80))]):
            with self.assertRaises(ValueError):resolve_target('http://localhost/feed')
            self.assertEqual(resolve_target('http://localhost/feed',True),('localhost','127.0.0.1'))
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('169.254.169.254',80))]):
            with self.assertRaises(ValueError):resolve_target('http://metadata/feed',True)

    def test_mixed_dns_answer_and_arbitrary_application_env_are_rejected(self):
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',80)),(2,1,6,'',('10.0.0.1',80))]):
            with self.assertRaises(ValueError):resolve_target('http://example.com/feed')
        for headers in [{'Host':'FOS_FEED_X'},{'Authorization':'LLM_API_KEY'},{'X-Test':'literal secret'}]:
            with self.assertRaises(ValueError):validate_headers(headers)

    def fetch(self,handler,*,cursor='',etag='',write_mode='upsert'):
        original=httpx.Client
        def client(**kwargs):return original(**kwargs,transport=httpx.MockTransport(handler))
        feed=SimpleNamespace(url='https://api.example.test/feed?limit=20',allow_private=False,
            header_env={'Authorization':'FOS_FEED_TEST_AUTH'},cursor=cursor,etag=etag,write_mode=write_mode)
        with patch('kb.feed_http.resolve_target',return_value=('api.example.test','203.0.113.10')),patch('kb.feed_http.httpx.Client',side_effect=client),patch.dict(os.environ,{'FOS_FEED_TEST_AUTH':'Bearer test-only'}):
            return fetch_batch(feed)

    def test_dns_is_pinned_but_http_host_tls_name_and_cursor_are_preserved(self):
        def handler(request):
            self.assertEqual(request.url.host,'203.0.113.10');self.assertEqual(request.headers['Host'],'api.example.test')
            self.assertEqual(request.extensions['sni_hostname'],'api.example.test')
            self.assertEqual(request.url.params['cursor'],'checkpoint');self.assertEqual(request.url.params['limit'],'20')
            self.assertNotIn('if-none-match',request.headers)
            return httpx.Response(200,stream=httpx.ByteStream(json.dumps(batch(cursor='next')).encode()),headers={'etag':'"next"','content-type':'application/json'})
        payload,etag=self.fetch(handler,cursor='checkpoint',etag='"old"')
        self.assertEqual(payload['cursor'],'next');self.assertEqual(etag,'')

    def test_etag_304_and_redirect_rejection(self):
        def unchanged(request):
            self.assertEqual(request.headers['If-None-Match'],'"old"');return httpx.Response(304)
        self.assertEqual(self.fetch(unchanged,etag='"old"'),(None,'"old"'))
        with self.assertRaisesMessage(ValueError,'302'):self.fetch(lambda request:httpx.Response(302,headers={'Location':'https://other.test/'}))

    def test_append_fetches_unchanged_content_instead_of_sending_conditional_etag(self):
        def handler(request):
            self.assertNotIn('If-None-Match',request.headers)
            return httpx.Response(200,stream=httpx.ByteStream(json.dumps(batch()).encode()),headers={'content-type':'application/json','etag':'"same"'})
        payload,etag=self.fetch(handler,etag='"same"',write_mode='append')
        self.assertEqual(payload['items'][0]['id'],'report-1');self.assertEqual(etag,'"same"')

    def test_trickled_bytes_cannot_keep_a_request_alive_forever(self):
        clock=[0]
        class Drip(httpx.SyncByteStream):
            def __iter__(self):
                for part in (b'{',b'"',b'}'):
                    clock[0]+=20;yield part
        with patch('kb.feed_http.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesMessage(ValueError,'30秒'):
                self.fetch(lambda request:httpx.Response(200,stream=Drip(),headers={'content-type':'application/json'}))


class FeedExecutionTests(TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        directory=self.stack.enter_context(tempfile.TemporaryDirectory());self.tmp=Path(directory)
        self.stack.enter_context(patch('django.conf.settings.MEDIA_ROOT',self.tmp/'media'))
        self.stack.enter_context(patch('kb.retriever.delete_doc_vectors'))
        self.user=get_user_model().objects.create_user('feed-admin',is_staff=True)
        self.folder=KnowledgeBase.objects.create(name='Auto reports',slug='auto-reports-test',is_folder=True,created_by=self.user)
        self.feed=KnowledgeFeed.objects.create(kb=self.folder,name='Reports',mode='push',write_mode='upsert',created_by=self.user)

    def run_batch(self,payload,request_key=''):
        run,fresh=claim(self.feed.pk,trigger='push',payload=payload,request_key=request_key)
        return execute(run.pk) if fresh else run

    def record(self,external_id='report-1'):
        return self.feed.records.get(external_id=external_id)

    def test_publishes_tracker_rows_without_sublibraries_or_indexing(self):
        run=self.run_batch(batch())
        self.assertEqual(run.counts['created'],1)
        tracker=self.folder.tracker
        self.assertTrue(tracker.enabled)
        self.assertEqual([field['label'] for field in tracker.fields],['标题','内容','更新时间','来源链接','采集时间','来源ID'])
        record=self.record();row=tracker.rows.get(feed=self.feed,external_id=record.external_id)
        self.assertEqual(row.status,TrackerRow.Status.DONE);self.assertEqual(row.values['标题'],'Daily report')
        self.assertIsNone(record.document_id);self.assertEqual(Document.objects.filter(kb=self.folder).count(),0)
        self.assertEqual(KnowledgeBase.objects.filter(parent=self.folder).count(),0)

    def test_upsert_skips_unchanged_and_updates_document_in_place(self):
        self.run_batch(batch());first=self.folder.tracker.rows.get().pk
        self.assertEqual(self.run_batch(batch()).counts['unchanged'],1)
        self.assertEqual(self.run_batch(batch('Changed pressure 18 MPa')).counts['updated'],1)
        record=self.record();self.assertEqual(self.folder.tracker.rows.get().pk,first)
        self.assertIn('Changed pressure',self.folder.tracker.rows.get().values['内容'])
        self.assertEqual(Document.objects.filter(kb=self.folder).count(),0)
        self.run_batch({'schema':SCHEMA,'items':[]})
        self.assertTrue(FeedRecord.objects.filter(feed=self.feed,external_id='report-1',deleted=False).exists())

    def test_structured_fields_become_tracker_columns_and_values(self):
        payload=batch();payload['items'][0]['fields']={'温度':'25'}
        self.run_batch(payload)
        tracker=self.folder.tracker
        self.assertEqual([field['label'] for field in tracker.fields],['标题','温度','更新时间','来源链接','采集时间','来源ID'])
        self.assertEqual(tracker.rows.get().values['温度'],'25')
        payload['items'][0]['fields']={'温度':'31','压力':'12'}
        self.run_batch(payload);tracker.refresh_from_db()
        self.assertEqual(tracker.rows.get().values['温度'],'31')
        self.assertEqual([field['label'] for field in tracker.fields],['标题','温度','更新时间','来源链接','采集时间','来源ID','压力'])
        self.assertGreater(tracker.schema_version,1)

    def test_failure_keeps_previous_rows_and_cursor(self):
        self.run_batch(batch(cursor='good'))
        old=self.record().document_id;payload=batch('unpublished pressure 19 MPa',cursor='bad')
        payload['items'].append({'id':'report-2','title':'Other report','content':'never published'})
        with patch('kb.feeds._publish_row',side_effect=ValueError('simulated outage')):
            run=self.run_batch(payload)
        self.assertEqual(run.status,'failed');self.feed.refresh_from_db()
        self.assertEqual(self.feed.cursor,'good');self.assertEqual(self.record().document_id,old)
        self.assertNotIn('unpublished',self.folder.tracker.rows.get().values['内容'])
        self.assertEqual(self.folder.tracker.rows.count(),1)

    def test_failed_batch_retries_same_key_and_publishes(self):
        payload=batch(batch_id='retry-1')
        with patch('kb.feeds._publish_row',side_effect=ValueError('simulated outage')):
            first=self.run_batch(payload,'batch:retry-1');self.assertEqual(first.status,'failed')
        second=self.run_batch(payload,'batch:retry-1')
        self.assertNotEqual(first.pk,second.pk);self.assertEqual(second.status,'completed')
        self.assertTrue(self.folder.tracker.rows.filter(feed=self.feed,external_id=self.record().external_id).exists());self.assertEqual(self.feed.runs.count(),2)

    def test_explicit_deletion_removes_row_and_upsert_restores_it(self):
        self.run_batch(batch())
        self.run_batch({'schema':SCHEMA,'items':[],'deleted':['report-1']})
        record=self.record();self.assertTrue(record.deleted);self.assertFalse(record.document_id)
        self.assertEqual(self.folder.tracker.rows.count(),0);self.assertEqual(Document.objects.filter(kb=self.folder).count(),0)
        self.run_batch(batch())
        self.assertFalse(self.record().deleted);self.assertEqual(self.folder.tracker.rows.count(),1)

    def test_timestamp_only_change_is_unchanged_and_older_updates_are_ignored(self):
        newer=batch();newer['items'][0]['updated_at']='2027-01-03T08:00:00Z';self.run_batch(newer)
        older=batch('Old incorrect report');older['items'][0]['updated_at']='2027-01-02T08:00:00Z'
        self.assertEqual(self.run_batch(older).counts['stale'],1)
        newest=deepcopy(newer);newest['items'][0]['updated_at']='2027-01-04T08:00:00Z'
        self.assertEqual(self.run_batch(newest).counts['unchanged'],1)
        self.assertNotIn('Old incorrect',self.folder.tracker.rows.get().values['内容'])

    def test_collection_can_complete_after_old_ten_minute_deadline(self):
        from itertools import chain,repeat
        run,_=claim(self.feed.pk,trigger='push',payload=batch())
        with patch('kb.feeds.time.monotonic',side_effect=chain([0],repeat(20*60))):
            result=execute(run.pk)
        self.assertEqual(result.status,'completed',result.error)
        self.assertEqual(self.folder.tracker.rows.count(),1)

    def test_sixty_minute_deadline_stops_publish_and_keeps_cursor(self):
        from itertools import chain,repeat
        run,_=claim(self.feed.pk,trigger='push',payload=batch(cursor='must-not-commit'))
        with patch('kb.feeds.time.monotonic',side_effect=chain([0],repeat(60*60))):
            result=execute(run.pk)
        self.assertEqual(result.status,'failed');self.assertIn('60分钟',result.error)
        self.assertFalse(TrackerRow.objects.filter(feed=self.feed).exists())
        self.feed.refresh_from_db();self.assertEqual(self.feed.cursor,'');self.assertFalse(self.feed.lease_token)

    def test_long_run_lease_prevents_reclaim_after_old_fifteen_minute_expiry(self):
        now=timezone.now()
        with patch('kb.feeds.timezone.now',return_value=now):run,_=claim(self.feed.pk,trigger='push',payload=batch())
        self.feed.refresh_from_db();self.assertEqual(self.feed.lease_until,now+timedelta(minutes=65))
        with patch('kb.feeds.timezone.now',return_value=now+timedelta(minutes=30)):
            with self.assertRaises(FeedBusy):claim(self.feed.pk,trigger='push',payload=batch('overlap'))
        run.refresh_from_db();self.assertEqual(run.status,'running')

    def test_source_lease_and_idempotency_prevent_overlapping_publications(self):
        payload=batch();run,fresh=claim(self.feed.pk,trigger='push',payload=payload,request_key='same')
        self.assertTrue(fresh)
        duplicate,new=claim(self.feed.pk,trigger='push',payload=payload,request_key='same');self.assertFalse(new);self.assertEqual(run.pk,duplicate.pk)
        with self.assertRaises(FeedBusy):claim(self.feed.pk,trigger='push',payload=batch('different'))
        with self.assertRaises(ValueError):claim(self.feed.pk,trigger='push',payload=batch('different'),request_key='same')
        execute(run.pk);self.assertEqual(self.folder.tracker.rows.count(),1)

    def test_expired_worker_is_fenced_and_logged_without_advancing_cursor(self):
        run,_=claim(self.feed.pk,trigger='push',payload=batch(cursor='uncommitted'))
        KnowledgeFeed.objects.filter(pk=self.feed.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.assertEqual(run_due(),[]);run.refresh_from_db();self.assertEqual(run.status,'failed')
        self.assertEqual(execute(run.pk).status,'failed')
        self.feed.refresh_from_db();self.assertEqual(self.feed.cursor,'')

    def test_permission_revocation_stops_already_accepted_run(self):
        run,_=claim(self.feed.pk,trigger='push',payload=batch())
        self.user.is_active=False;self.user.save(update_fields=['is_active'])
        self.assertEqual(execute(run.pk).status,'failed');self.assertFalse(KbTracker.objects.filter(kb=self.folder).exists())

    def test_revoked_scheduled_owner_is_reported_once_and_source_is_disabled(self):
        self.feed.mode='pull';self.feed.next_run_at=timezone.now()-timedelta(minutes=1)
        self.feed.save(update_fields=['mode','next_run_at'])
        self.user.is_active=False;self.user.save(update_fields=['is_active'])
        run_due();run_due();self.feed.refresh_from_db()
        self.assertFalse(self.feed.enabled);self.assertEqual(self.feed.runs.count(),1)
        self.assertEqual(self.feed.runs.get().status,'failed')

    def test_due_scan_claims_schedule_once_and_preserves_checkpoint_on_http_failure(self):
        self.feed.mode='pull';self.feed.url='https://api.example.test/feed';self.feed.next_run_at=timezone.now()-timedelta(minutes=2)
        self.feed.save(update_fields=['mode','url','next_run_at'])
        with patch('kb.feed_http.fetch_batch',side_effect=ValueError('HTTP unavailable')):
            runs=run_due();self.assertEqual(len(runs),1);self.assertEqual(runs[0].status,'failed')
            self.assertEqual(run_due(),[])
        self.feed.refresh_from_db();self.assertEqual(self.feed.cursor,'');self.assertFalse(self.feed.lease_token)

    def test_import_command_validation_is_offline_and_does_not_create_runs(self):
        path=self.tmp/'payload.json';path.write_text(json.dumps(batch()),encoding='utf-8')
        call_command('feed_import',str(path),validate_only=True,stdout=io.StringIO())
        self.assertEqual(self.feed.runs.count(),0)


class FeedViewsTests(TestCase):
    def setUp(self):
        self.admin=get_user_model().objects.create_user('source-admin',is_staff=True)
        self.reader=get_user_model().objects.create_user('source-reader')
        self.kb=KnowledgeBase.objects.create(name='Source reports',slug='source-reports',is_folder=True)
        self.feed=KnowledgeFeed.objects.create(kb=self.kb,name='API reports',mode='push',created_by=self.admin)
        self.token=rotate_token(self.feed)
        self.client.force_login(self.admin)

    def action(self,data):return self.client.post(reverse('kb:feed_action'),json.dumps(data),content_type='application/json',HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_create_source_and_library_without_starting_an_api_request(self):
        response=self.action({'action':'create','name':'New feed','kb_name':'New auto KB','mode':'pull','url':'https://api.example.test/feed','cron':'0 8 * * *'})
        self.assertEqual(response.status_code,200,response.content)
        feed=KnowledgeFeed.objects.get(pk=response.json()['feed']['id'])
        self.assertTrue(feed.kb.is_folder);self.assertIsNotNone(feed.next_run_at);self.assertEqual(feed.runs.count(),0)

    def test_reader_cannot_manage_sources_or_download_kit(self):
        self.client.force_login(self.reader)
        for name in ('feeds','feed_state','feed_kit'):self.assertEqual(self.client.get(reverse('kb:'+name)).status_code,200)
        self.assertEqual(self.client.get(reverse('kb:feed_state')).json()['feeds'],[])

    def test_department_manager_cannot_target_other_department_or_read_arbitrary_server_env(self):
        from accounts.models import UserProfile
        user=get_user_model().objects.create_user('source-dept-admin')
        UserProfile.objects.update_or_create(user=user,defaults={'department':'机械','role':UserProfile.Role.DEPT_ADMIN})
        self.client.force_login(user)
        self.assertEqual(self.action({'action':'pause','id':str(self.feed.pk)}).status_code,404)
        response=self.action({'action':'create','kb_name':'Dept reports','name':'Feed','mode':'pull','url':'https://example.com','header_env':{'Authorization':'FOS_FEED_OTHER_TOKEN'}})
        self.assertEqual(response.status_code,400)

    def test_token_is_source_scoped_hashed_and_not_in_state(self):
        self.feed.refresh_from_db();self.assertNotEqual(self.feed.token_digest,self.token)
        self.assertNotIn(self.token,self.client.get(reverse('kb:feed_state')).content.decode())
        other=KnowledgeFeed.objects.create(kb=self.kb,name='Other',mode='push',created_by=self.admin)
        response=self.client.post(reverse('kb:feed_ingest',args=[other.pk]),json.dumps(batch()),content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertEqual(response.status_code,401)

    def test_machine_push_requires_token_and_duplicate_delivery_starts_once(self):
        url=reverse('kb:feed_ingest',args=[self.feed.pk]);raw=json.dumps(batch(batch_id='delivery-1'))
        self.assertEqual(self.client.post(url,raw,content_type='application/json').status_code,401)
        with patch('kb.feed_views.start_async') as start:
            first=self.client.post(url,raw,content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
            second=self.client.post(url,raw,content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertEqual(first.status_code,202);self.assertEqual(first.json()['run']['id'],second.json()['run']['id']);self.assertTrue(second.json()['replayed'])
        self.assertEqual(start.call_count,1)
        status=self.client.get(first.json()['status_url'],HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertEqual(status.status_code,200)

    def test_rotating_token_revokes_old_token_and_pausing_blocks_pushes(self):
        result=self.action({'action':'rotate_token','id':str(self.feed.pk)});self.assertEqual(result.status_code,200)
        url=reverse('kb:feed_ingest',args=[self.feed.pk])
        self.assertEqual(self.client.post(url,json.dumps(batch()),content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token).status_code,401)
        self.action({'action':'pause','id':str(self.feed.pk)})
        self.assertEqual(self.client.post(url,json.dumps(batch()),content_type='application/json',HTTP_AUTHORIZATION='Bearer '+result.json()['token']).status_code,404)

    def test_kit_contains_versioned_schema_and_adapter_but_no_project_data(self):
        response=self.client.get(reverse('kb:feed_kit'));self.assertEqual(response.status_code,200)
        with zipfile.ZipFile(io.BytesIO(b''.join(response.streaming_content))) as archive:
            self.assertEqual(len(archive.namelist()),5)
            self.assertIn('docs/schemas/knowledge-feed-v1.schema.json',archive.namelist())
            self.assertFalse(any(name.endswith('.sqlite3') or '.env' in name for name in archive.namelist()))

    def test_invalid_cron_url_and_uuid_return_json_without_creating_partial_library(self):
        before=KnowledgeBase.objects.count()
        result=self.action({'action':'create','name':'Invalid','kb_name':'Must not exist','mode':'pull','url':'https://example.com','cron':'0 24 * * *'})
        self.assertEqual(result.status_code,400);self.assertEqual(KnowledgeBase.objects.count(),before)
        self.assertEqual(self.action({'action':'run','id':'not-a-uuid'}).status_code,400)
