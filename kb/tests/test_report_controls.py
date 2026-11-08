import json
import tempfile
from copy import deepcopy
from pathlib import Path
from datetime import datetime,timedelta,timezone as utc
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase,Client,override_settings
from django.urls import reverse
from django.utils import timezone
from kb import feed_reports as reports,report_controls as controls
from kb.feed_cron import CronSchedule
from kb.feeds import claim,execute
from kb.models import KnowledgeBase,KnowledgeFeed,ConnectorDraft

URL='https://tenant.environment.api.powerplatform.com/workflows/fixture/triggers/manual/paths/invoke?sig=fixture'

@override_settings(FEED_REPORT_PUBLIC_BASE='https://reports.example.test')
class ManualReportControlTests(TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        override=override_settings(DATA_DIR=Path(folder.name));override.enable();self.addCleanup(override.disable)
        self.user=get_user_model().objects.create_user('report-builder',is_staff=True)
        self.other=get_user_model().objects.create_user('other-builder',is_staff=True)
        kb=KnowledgeBase.objects.create(name='Reports',slug='manual-report-controls',is_folder=True,created_by=self.user)
        self.feed=KnowledgeFeed.objects.create(kb=kb,name='Feed',mode='push',created_by=self.user)
        run,_=claim(self.feed.pk,trigger='push',payload={'schema':'fos-rag-feed/v1','items':[{'id':'ride','title':'Ride','content':'Hours'}]});execute(run.pk)
        self.feed.mode='pull';self.feed.url='https://example.test/data';self.feed.cron='*/15 * * * *';self.feed.next_run_at=timezone.now()+timedelta(minutes=15);self.feed.save()
        reports.configure(self.user,self.feed,{'kind':'teams','url':URL})
        parent=ConnectorDraft.objects.create(user=self.user,kb=kb,feed=self.feed,status='enabled')
        self.job=ConnectorDraft.objects.create(user=self.user,kb=kb,parent=parent,status='running')
        spec={'title':'Hours report','columns':[{'label':'Attraction','field':'标题'}],'range':{'mode':'all'},'schedule':{'mode':'cron','cron':'0 9 * * mon','timezone':'Asia/Shanghai'}}
        self.job.spec,self.job.preview=reports.preview(self.job,spec);self.job.status='ready';self.job.save();reports.approve(self.job,self.user)
        self.plan=self.feed.report_plan;self.plan.refresh_from_db()
        self.original_collection=KnowledgeFeed.objects.filter(pk=self.feed.pk).values().get()
        self.ciphertext=self.plan.destination
        self.client.force_login(self.user);self.url=reverse('kb:report_schedule_control',args=[self.feed.pk])

    def action(self,action,**extra):
        response=self.client.post(self.url,json.dumps({'action':action,**extra}),content_type='application/json')
        self.assertEqual(KnowledgeFeed.objects.filter(pk=self.feed.pk).values().get(),self.original_collection)
        self.plan.refresh_from_db();self.assertEqual(self.plan.destination,self.ciphertext)
        return response

    def test_feed_page_exposes_web_editor_and_safe_report_state(self):
        page=self.client.get(reverse('kb:feeds'));self.assertContains(page,'id="feedReportForm"');self.assertContains(page,'feed_report_controls.js')
        state=self.client.get(reverse('kb:feed_state')).json()['feeds'][0]['report']
        self.assertTrue(state['has_report']);self.assertTrue(state['can_edit']);self.assertTrue(state['enabled'])
        self.assertEqual(state['control_url'],self.url);self.assertEqual(state['kind'],'teams')
        self.assertNotIn('sig=',json.dumps(state))

    def test_pause_stops_due_delivery_without_touching_collection(self):
        self.plan.next_run_at=timezone.now()-timedelta(minutes=1);self.plan.save()
        original=deepcopy(self.plan.spec)
        with patch('kb.feed_reports.post_workflow') as post:
            response=self.action('pause');self.assertEqual(response.status_code,200,response.content)
            self.assertEqual(reports.run_due(),[]);post.assert_not_called()
        self.assertFalse(self.plan.enabled);self.assertIsNone(self.plan.next_run_at);self.assertEqual(self.plan.spec,original)

    def test_resume_recomputes_future_time_without_sending_or_collecting(self):
        self.action('pause');now=datetime(2026,11,8,1,5,tzinfo=utc.utc)
        with patch('kb.report_controls.timezone.now',return_value=now),patch('kb.feed_reports.post_workflow') as post,patch('kb.feed_http.fetch_batch') as collect:
            response=self.action('resume');self.assertEqual(response.status_code,200,response.content);post.assert_not_called();collect.assert_not_called()
        self.assertTrue(self.plan.enabled);self.assertEqual(self.plan.next_run_at,CronSchedule(self.plan.cron,self.plan.timezone).next_after(now))
        previous=(self.plan.next_run_at,self.plan.updated_at)
        self.action('resume');self.assertEqual((self.plan.next_run_at,self.plan.updated_at),previous)

    def test_active_frequency_change_updates_only_report_schedule(self):
        original=deepcopy(self.plan.spec);schedule={'mode':'cron','cron':'30 16 * * fri','timezone':'America/New_York'}
        now=timezone.now()
        response=self.action('update_schedule',schedule=schedule);self.assertEqual(response.status_code,200,response.content)
        self.assertTrue(self.plan.enabled);self.assertGreater(self.plan.next_run_at,now)
        self.assertEqual(self.plan.spec,{**original,'schedule':schedule})
        self.assertEqual((self.plan.cron,self.plan.timezone),('30 16 * * fri','America/New_York'))

    def test_paused_report_stays_paused_after_frequency_change(self):
        self.action('pause')
        response=self.action('update_schedule',schedule={'mode':'cron','cron':'15 */3 * * *','timezone':'Asia/Shanghai'})
        self.assertEqual(response.status_code,200,response.content);self.assertFalse(self.plan.enabled);self.assertIsNone(self.plan.next_run_at)
        self.assertEqual(self.plan.cron,'15 */3 * * *')

    def test_on_demand_mode_disables_schedule_until_frequency_is_chosen(self):
        response=self.action('update_schedule',schedule={'mode':'manual','timezone':'Asia/Shanghai'})
        self.assertEqual(response.status_code,200);self.assertFalse(self.plan.enabled);self.assertEqual(self.plan.cron,'');self.assertIsNone(self.plan.next_run_at)
        self.assertEqual(self.action('resume').status_code,400)

    def test_invalid_cron_timezone_and_action_leave_schedule_unchanged(self):
        before=deepcopy(self.plan.spec)
        for schedule in ({'mode':'cron','cron':'0 25 * * *','timezone':'UTC'},{'mode':'cron','cron':'0 9 * * *','timezone':'Not/AZone'},{'mode':'cron','cron':'0 0 31 apr *','timezone':'UTC'},{'mode':'after_collection','timezone':'UTC'}):
            with self.subTest(schedule=schedule):
                self.assertEqual(self.action('update_schedule',schedule=schedule).status_code,400)
                self.assertEqual(self.plan.spec,before);self.assertTrue(self.plan.enabled)
        self.assertEqual(self.action('unknown').status_code,400)

    def test_only_original_builder_can_change_schedule_even_among_admins(self):
        self.client.force_login(self.other)
        for action in ('pause','resume','update_schedule'):
            self.assertEqual(self.action(action,schedule={'mode':'manual','timezone':'UTC'}).status_code,403)
        state=self.client.get(reverse('kb:feed_state')).json()['feeds'][0]['report'];self.assertFalse(state['can_edit'])
        self.assertTrue(self.plan.enabled)

    def test_missing_workflow_prevents_resume_but_allows_saving_paused_frequency(self):
        self.action('pause');self.plan.destination='';self.plan.save();self.ciphertext=''
        self.assertEqual(self.action('update_schedule',schedule={'mode':'cron','cron':'0 11 * * *','timezone':'UTC'}).status_code,200)
        response=self.action('resume');self.assertEqual(response.status_code,400);self.assertIn('编辑配置',response.json()['error']);self.assertFalse(self.plan.enabled)

    def test_pausing_while_report_is_preparing_cancels_post(self):
        self.plan.next_run_at=timezone.now()-timedelta(minutes=1);self.plan.save();workbook=reports.workbook
        def pause(*args):self.action('pause');return workbook(*args)
        with patch('kb.feed_reports.workbook',side_effect=pause),patch('kb.feed_reports.post_workflow') as post:
            result=reports.run_due()[0];self.assertEqual(result.status,'failed');self.assertIn('取消',result.error);post.assert_not_called()

    def test_old_approved_channel_is_recovered_before_manual_resume(self):
        self.plan.spec.pop('delivery');self.plan.save()
        self.job.refresh_from_db();self.job.spec.pop('delivery');self.job.save()
        reports.configure(self.user,self.feed,{'kind':'email','url':URL.replace('/fixture/','/email/'),'recipient':'later@example.test'})
        self.plan.refresh_from_db();self.ciphertext=self.plan.destination
        self.assertEqual(controls.state(self.feed,self.user)['kind'],'teams')
        response=self.action('resume');self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(self.plan.spec['delivery']['kind'],'teams')
        with patch('kb.feed_reports.post_workflow',return_value=202) as post:result=reports.send(self.plan,request_key='legacy-manual-control')
        self.assertEqual(result.status,'accepted',result.error);self.assertEqual(post.call_args.args[0]['kind'],'teams')

    def test_controls_require_csrf_and_do_not_expose_unconfirmed_plans(self):
        client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        response=client.post(self.url,json.dumps({'action':'pause'}),content_type='application/json');self.assertEqual(response.status_code,403)
        self.plan.spec={};self.plan.enabled=False;self.plan.save()
        state=controls.state(self.feed,self.user);self.assertFalse(state['has_report']);self.assertEqual(self.action('resume').status_code,400)
