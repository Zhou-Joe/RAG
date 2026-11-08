import importlib
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase,override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from kb import feed_reports as reports
from kb.collector_api import approve
from kb.connector_tools import check_spec
from kb.connector_views import _json
from kb.feeds import claim,execute
from kb.models import KnowledgeBase,KnowledgeFeed,ConnectorDraft,FeedReportPlan,FeedReportDelivery

URL='https://tenant.environment.api.powerplatform.com/workflows/fixture/triggers/manual/paths/invoke?sig=fixture'


@override_settings(FEED_REPORT_PUBLIC_BASE='https://reports.example.test')
class UnifiedFeedNameTests(TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        override=override_settings(DATA_DIR=Path(folder.name));override.enable();self.addCleanup(override.disable)
        self.user=get_user_model().objects.create_user('name-builder',is_staff=True)
        self.other=get_user_model().objects.create_user('name-other',is_staff=True)
        self.kb=KnowledgeBase.objects.create(name='Operations',slug='unified-names',is_folder=True,category='business',created_by=self.user)
        self.spec={'name':'Separate source name','url':'https://example.test/data','records_path':'rows',
            'id_paths':['id'],'title_path':'title','body_fields':[{'label':'Value','path':'value'}],
            'cron':'0 8 * * *','timezone':'Asia/Shanghai'}
        self.feed=KnowledgeFeed.objects.create(kb=self.kb,name='Separate feed name',url=self.spec['url'],adapter_spec=self.spec,
            created_by=self.user,cron='0 8 * * *',next_run_at=timezone.now()+timedelta(hours=2))
        self.report={'kind':'report','title':'Separate report name','columns':[{'label':'Source','field':'_source_id'}],
            'range':{'mode':'all'},'schedule':{'mode':'cron','cron':'0 9 * * mon','timezone':'Asia/Shanghai'}}
        self.plan=FeedReportPlan.objects.create(feed=self.feed,created_by=self.user,spec=self.report,enabled=True,
            schedule_mode='cron',cron='0 9 * * mon',timezone='Asia/Shanghai',next_run_at=timezone.now()+timedelta(days=2))
        self.parent=ConnectorDraft.objects.create(user=self.user,kb=self.kb,feed=self.feed,status='enabled',api_url=self.spec['url'],spec=self.spec)
        self.draft=ConnectorDraft.objects.create(user=self.user,kb=self.kb,parent=self.parent,status='ready',api_url=self.spec['url'],spec=self.report,preview={'source_feed':'Old source','valid':True})
        self.client.force_login(self.user)

    def action(self,**extra):
        return self.client.post(reverse('kb:feed_action'),json.dumps({'action':'update','id':str(self.feed.pk),
            'mode':'pull','url':self.feed.url,'cron':self.feed.cron,'timezone':self.feed.timezone,**extra}),
            content_type='application/json',HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def renamed(self):
        self.kb.name='Ride operations';self.kb.save(update_fields=['name','updated_at'])

    def test_new_feed_and_report_use_only_the_kb_name(self):
        self.assertEqual(self.feed.name,'Operations');self.assertEqual(self.feed.adapter_spec['name'],'Operations')
        self.assertEqual(self.plan.spec['title'],'Operations')
        normalized=reports.normalize({key:value for key,value in self.report.items() if key!='title'},self.feed)
        self.assertEqual(normalized['title'],'Operations')

    def test_kb_rename_updates_all_related_names_without_changing_schedules_or_artifacts(self):
        sibling=KnowledgeFeed.objects.create(kb=self.kb,name='Second source',mode='push',created_by=self.other)
        delivery=FeedReportDelivery.objects.create(plan=self.plan,request_key='immutable',status='accepted',content=b'old workbook')
        before_feed=(self.feed.pk,self.feed.url,self.feed.cron,self.feed.next_run_at,self.feed.enabled,self.feed.cursor)
        before_plan=(self.plan.pk,self.plan.cron,self.plan.next_run_at,self.plan.enabled,self.plan.destination)
        self.renamed()
        self.feed.refresh_from_db();self.plan.refresh_from_db();self.parent.refresh_from_db();self.draft.refresh_from_db();sibling.refresh_from_db();delivery.refresh_from_db()
        self.assertEqual(self.feed.name,self.kb.name);self.assertEqual(self.feed.adapter_spec['name'],self.kb.name)
        self.assertEqual(self.plan.spec['title'],self.kb.name);self.assertEqual(self.parent.spec['name'],self.kb.name)
        self.assertEqual(self.draft.spec['title'],self.kb.name);self.assertEqual(self.draft.preview['source_feed'],self.kb.name)
        self.assertEqual(sibling.name,self.kb.name);self.assertEqual(delivery.content,b'old workbook')
        self.assertEqual(before_feed,(self.feed.pk,self.feed.url,self.feed.cron,self.feed.next_run_at,self.feed.enabled,self.feed.cursor))
        self.assertEqual(before_plan,(self.plan.pk,self.plan.cron,self.plan.next_run_at,self.plan.enabled,self.plan.destination))
        self.assertEqual(self.kb.slug,'unified-names')

    def test_edit_settings_accepts_a_single_kb_name_and_exposes_it_everywhere(self):
        response=self.action(kb_name='Unified report data')
        self.assertEqual(response.status_code,200,response.content)
        state=response.json()['feed'];self.assertEqual(state['name'],'Unified report data');self.assertEqual(state['kb_name'],state['name'])
        self.assertEqual(state['report']['title'],state['name'])
        self.kb.refresh_from_db();self.assertEqual(self.kb.name,state['name'])
        html=self.client.get(reverse('kb:feeds')).content.decode()
        self.assertEqual(html.count('name="kb_name"'),1);self.assertNotIn('name="name"',html)

    def test_settings_without_any_name_keeps_the_existing_name(self):
        response=self.action();self.assertEqual(response.status_code,200,response.content)
        self.kb.refresh_from_db();self.assertEqual(self.kb.name,'Operations')

    def test_legacy_name_alias_updates_the_kb_but_cannot_create_a_second_name(self):
        response=self.action(name='Legacy caller rename');self.assertEqual(response.status_code,200,response.content)
        self.kb.refresh_from_db();self.assertEqual(self.kb.name,'Legacy caller rename')
        self.assertEqual(response.json()['feed']['name'],self.kb.name)

    def test_create_needs_only_kb_name_and_existing_kb_ignores_old_source_alias(self):
        base={'action':'create','mode':'push'}
        response=self.client.post(reverse('kb:feed_action'),json.dumps({**base,'kb_name':'One new name'}),content_type='application/json',HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,200,response.content);state=response.json()['feed']
        self.assertEqual(state['name'],'One new name');self.assertEqual(state['kb_name'],state['name'])
        response=self.client.post(reverse('kb:feed_action'),json.dumps({**base,'kb':self.kb.slug,'name':'Ignored old source alias'}),content_type='application/json',HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,200,response.content);self.assertEqual(response.json()['feed']['name'],'Operations')

    def test_non_creator_and_invalid_changes_cannot_rename_any_related_object(self):
        self.client.force_login(self.other)
        self.assertEqual(self.action(kb_name='Forbidden').status_code,403)
        self.client.force_login(self.user)
        for name in ['', 'x'*101, 123]:self.assertEqual(self.action(kb_name=name).status_code,400)
        self.assertEqual(self.action(kb_name='Roll back',write_mode='invalid').status_code,400)
        self.kb.refresh_from_db();self.feed.refresh_from_db();self.plan.refresh_from_db()
        self.assertEqual(self.kb.name,'Operations');self.assertEqual(self.feed.name,'Operations');self.assertEqual(self.plan.spec['title'],'Operations')

    def test_kb_page_rename_is_propagated_and_stale_preview_cannot_revert_it(self):
        response=self.client.post(reverse('kb:kb_rename',args=[self.kb.slug]),{'name':'Renamed in KB settings'},HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,200,response.content)
        # self.parent still contains the old proposal in memory.
        result=check_spec(self.parent,self.parent.spec)
        self.assertEqual(result['name'],'Renamed in KB settings')
        self.assertEqual(_json(self.draft)['spec']['title'],'Renamed in KB settings')
        self.assertEqual(reports.normalize(self.report,self.feed)['title'],'Renamed in KB settings')
        self.plan.spec=self.report;self.plan.save(update_fields=['spec'])
        self.plan.refresh_from_db();self.assertEqual(self.plan.spec['title'],'Renamed in KB settings')

    def test_approved_collection_revision_cannot_change_the_canonical_name(self):
        self.parent.spec=check_spec(self.parent,self.spec);self.parent.save()
        proposal=ConnectorDraft.objects.create(user=self.user,kb=self.kb,parent=self.parent,status='ready',api_url=self.spec['url'],spec=self.spec)
        self.renamed()
        result=approve(user=self.user,job=proposal,run_now=False)
        self.assertEqual(result.pk,self.feed.pk);self.assertEqual(result.name,'Ride operations')
        self.assertEqual(result.adapter_spec['name'],'Ride operations')

    def test_future_email_and_teams_reports_use_current_kb_name_with_real_excel(self):
        # Ingest a real row; mock only the outbound network boundary.
        self.feed.mode='push';self.feed.adapter_spec={};self.feed.save()
        run,_=claim(self.feed.pk,trigger='push',payload={'schema':'fos-rag-feed/v1','items':[{'id':'ride','title':'Attraction','content':'Hours'}]})
        execute(run.pk)
        self.renamed()
        for kind in ['email','teams']:
            plan=reports.configure(self.user,self.feed,{'kind':kind,'url':URL,'recipient':'fixture@example.test'})
            with patch('kb.feed_reports.post_workflow',return_value=202) as post:
                delivery=reports.send(plan,spec=self.report,request_key='named-'+kind)
            self.assertEqual(delivery.status,'accepted',delivery.error)
            payload=post.call_args.args[1]
            if kind=='email':self.assertEqual(payload['s'],'Ride operations')
            else:self.assertEqual(payload['attachments'][0]['content']['body'][0]['text'],'Ride operations')
            self.assertEqual(load_workbook(io.BytesIO(delivery.content)).properties.title,'Ride operations')

    def test_data_migration_aligns_legacy_names_idempotently_without_touching_schedules(self):
        KnowledgeFeed.objects.filter(pk=self.feed.pk).update(name='Old feed',adapter_spec=self.spec)
        FeedReportPlan.objects.filter(pk=self.plan.pk).update(spec=self.report)
        before=(self.plan.next_run_at,self.feed.next_run_at,self.plan.enabled)
        align=importlib.import_module('kb.migrations.0054_unified_feed_names').align_names
        for _ in range(2):align(apps,SimpleNamespace(connection=connection))
        self.feed.refresh_from_db();self.plan.refresh_from_db();self.draft.refresh_from_db()
        self.assertEqual(self.feed.name,'Operations');self.assertEqual(self.feed.adapter_spec['name'],'Operations')
        self.assertEqual(self.plan.spec['title'],'Operations');self.assertEqual(self.draft.spec['title'],'Operations')
        self.assertEqual(before,(self.plan.next_run_at,self.feed.next_run_at,self.plan.enabled))


    def test_builder_does_not_ask_for_an_independent_report_name(self):
        from langchain_core.messages import AIMessageChunk
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        from kb import connector_sessions
        job=ConnectorDraft.objects.create(user=self.user,kb=self.kb,parent=self.parent,status='running',feedback='Explain the report name')
        self.addCleanup(connector_sessions.discard,[job.pk],self.user.pk)
        model=FakeModel([call('ask_user_questions',{'questions':[{'key':'report_title','label':'Report title','type':'text'}]}),
                         AIMessageChunk(content='The report uses the knowledge base name.')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):result=run(job.pk)
        self.assertEqual(result.status,'answered',result.error);self.assertFalse(result.questions)

    def test_builder_keeps_real_missing_fields_when_removing_name_questions(self):
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        from kb import connector_sessions
        job=ConnectorDraft.objects.create(user=self.user,kb=self.kb,parent=self.parent,status='running',feedback='Prepare a report')
        self.addCleanup(connector_sessions.discard,[job.pk],self.user.pk)
        model=FakeModel([call('ask_user_questions',{'questions':[
            {'key':'report_title','label':'Report title','type':'text'},
            {'key':'start_date','label':'Start date','type':'date'}]})])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):result=run(job.pk)
        self.assertEqual(result.status,'needs_info',result.error)
        self.assertEqual([field['key'] for field in result.preview['question_fields']],['start_date'])

    def test_approval_with_a_cached_kb_cannot_restore_its_previous_name(self):
        # Load independent model instances before renaming in another request.
        proposal=ConnectorDraft.objects.create(user=self.user,kb=self.kb,parent=self.parent,status='ready',api_url=self.spec['url'],spec=self.spec)
        proposal=ConnectorDraft.objects.select_related('kb','parent__feed').get(pk=proposal.pk)
        self.renamed()
        feed=approve(user=self.user,job=proposal,run_now=False)
        proposal.refresh_from_db();self.assertEqual(proposal.spec['name'],'Ride operations')
        self.assertEqual(feed.name,'Ride operations')
