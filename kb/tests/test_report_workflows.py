import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase,override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
from kb import feed_reports as reports
from kb.feeds import claim,execute
from kb.models import KnowledgeBase,KnowledgeFeed,ConnectorDraft,FeedReportPlan,FeedReportDelivery

URL='https://tenant.environment.api.powerplatform.com/powerautomate/workflows/fixture/triggers/manual/paths/invoke?sig=fixture'

@override_settings(FEED_REPORT_PUBLIC_BASE='')
class ReportWorkflowTests(TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        override=override_settings(DATA_DIR=Path(folder.name));override.enable();self.addCleanup(override.disable)
        self.user=get_user_model().objects.create_user('report-admin',is_staff=True)
        self.client.force_login(self.user)
        self.kb=KnowledgeBase.objects.create(name='Hours',slug='hours-report',is_folder=True,created_by=self.user)
        self.feed=KnowledgeFeed.objects.create(kb=self.kb,name='Hours',mode='push',created_by=self.user)
        self.run=self.publish()
        self.spec={'title':'运营时间报告','range':{'mode':'all'},'schedule':{'mode':'cron','cron':'0 9 * * mon','timezone':'Asia/Shanghai'},'columns':[{'label':'Attraction','field':'标题'},{'label':'Open','field':'Open'},{'label':'Close','field':'Close'},{'label':'Duration','computed':{'op':'duration','start':'Open','end':'Close','format':'hhmm','unit':'hours'}}]}
        self.plan=reports.configure(self.user,self.feed,{'kind':'email','url':URL,'recipient':'fixture@example.com','public_base':'https://reports.example.com'})
        self.parent=ConnectorDraft.objects.create(user=self.user,kb=self.kb,feed=self.feed,status='enabled',prompt='collect hours',spec={'url':'https://example.com/hours'})
        self.job=ConnectorDraft.objects.create(user=self.user,kb=self.kb,parent=self.parent,status='running',prompt='collect hours',feedback='生成运营时间报告')

    def publish(self):
        payload={'schema':'fos-rag-feed/v1','items':[{'id':'ride-1','title':'=HYPERLINK("https://invalid")','content':'Hours','fields':{'Open':'09:00','Close':'21:00','Count':'10'}}]}
        run,_=claim(self.feed.pk,trigger='push',payload=payload);return execute(run.pk)

    def test_projection_computation_and_excel_formula_safety(self):
        spec,result=reports.preview(self.job,self.spec)
        self.assertEqual(result['preview'][0]['Duration'],12)
        headers,rows=reports.table(self.feed,spec)
        book=load_workbook(io.BytesIO(reports.workbook(spec['title'],headers,rows)))
        self.assertEqual(book.active['A2'].data_type,'s');self.assertEqual(book.active['D2'].value,12)
        self.assertEqual(book.active.freeze_panes,'A2')

    def test_preview_never_sends_and_approval_keeps_original_collection(self):
        original=self.feed.adapter_spec;cron=self.feed.cron
        with patch('kb.feed_reports.post_workflow') as post:
            self.job.spec,self.job.preview=reports.preview(self.job,self.spec);self.job.status='ready';self.job.save()
            reports.approve(self.job,self.user);post.assert_not_called()
        self.feed.refresh_from_db();self.assertEqual(self.feed.adapter_spec,original);self.assertEqual(self.feed.cron,cron)
        self.plan.refresh_from_db();self.assertTrue(self.plan.enabled)

    def test_successful_collection_dispatches_once_with_email_keys_and_download(self):
        self.spec['schedule']={'mode':'after_collection'};self.plan.schedule_mode='after_collection'
        self.plan.spec=reports.normalize(self.spec,self.feed);self.plan.enabled=True;self.plan.save()
        with patch('kb.feed_reports.post_workflow',return_value=202) as post:
            reports.after_run(self.run);reports.after_run(self.run)
        self.assertEqual(post.call_count,1)
        body=post.call_args.args[1];self.assertEqual(set(body),{'t','s','b'});self.assertIn('Download complete Excel',body['b'])
        self.assertNotIn('sig=fixture',body['b']);self.assertIn('&quot;',body['b'])
        delivery=FeedReportDelivery.objects.get();self.assertEqual(delivery.status,'accepted')
        import re
        url=re.search(r'href="([^"]+)"',body['b'])[1]
        response=self.client.get(url.split('reports.example.com')[1]);self.assertEqual(response.status_code,200)
        delivery.expires_at=timezone.now();delivery.save();self.assertEqual(self.client.get(url.split('reports.example.com')[1]).status_code,404)

    def test_teams_payload_is_adaptive_card_and_html_is_escaped(self):
        spec=reports.normalize(self.spec,self.feed);headers,rows=reports.table(self.feed,spec)
        result=reports.payload({'kind':'teams'},spec,headers,rows,'https://reports.example.com/download')
        self.assertEqual(result['attachments'][0]['contentType'],'application/vnd.microsoft.card.adaptive')

    def test_timeout_is_not_automatically_retried(self):
        self.plan.spec=reports.normalize(self.spec,self.feed);self.plan.enabled=True;self.plan.save()
        with patch('kb.feed_reports.post_workflow',side_effect=TimeoutError) as post:
            first=reports.send(self.plan,run=self.run);second=reports.send(self.plan,run=self.run)
        self.assertEqual(first.status,'unknown');self.assertEqual(first.pk,second.pk);self.assertEqual(post.call_count,1)

    def test_failed_collection_and_disabled_plan_do_not_send(self):
        with patch('kb.feed_reports.post_workflow') as post:
            self.assertIsNone(reports.send(self.plan,run=self.run))
            self.plan.enabled=True;self.run.status='failed';self.assertIsNone(reports.send(self.plan,run=self.run))
            post.assert_not_called()

    def test_destination_is_encrypted_and_wrong_workflow_links_rejected(self):
        self.assertNotIn('sig=fixture',self.plan.destination)
        self.assertEqual(reports.destination(self.plan)['url'],URL)
        for value in ['http://localhost/invoke','https://automations.teams.cloud.microsoft/RunFromLink?shareToken=abc',URL.replace('tenant.environment.api.powerplatform.com','evil.example.com')]:
            with self.assertRaises(ValueError):reports.workflow_url(value)

    def test_reader_cannot_preview_configure_or_send(self):
        reader=get_user_model().objects.create_user('report-reader');self.client.force_login(reader)
        response=self.client.post(reverse('kb:report_configure',args=[self.feed.pk]),json.dumps({'url':URL}),content_type='application/json')
        self.assertEqual(response.status_code,400)
        with self.assertRaises(ValueError):reports.require_feed(reader,self.feed)

    def test_dialogue_secret_capture_removes_webhook_from_model_input(self):
        cleaned=reports.capture_feedback(self.user,self.feed,'把报告发邮件给 fixture@example.com '+URL)
        self.assertNotIn(URL,cleaned);self.assertNotIn('sig=',cleaned)
        self.plan.refresh_from_db();self.assertFalse(self.plan.enabled)

    def test_numeric_filters_compare_numbers(self):
        spec=reports.normalize({**self.spec,'filters':[{'field':'Count','op':'gte','value':2}]},self.feed)
        self.assertEqual(len(reports.table(self.feed,spec)[1]),1)

    def test_agent_can_preview_report_without_recollecting(self):
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        model=FakeModel([call('preview_scheduled_report',{'report':self.spec})])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):
            result=run(self.job.pk)
        self.assertEqual(result.status,'ready',result.error);self.assertEqual(result.spec['kind'],'report')

    def test_continued_report_keeps_plan_and_removes_workflow_url_before_storage(self):
        from kb.collector_api import refine_draft
        from kb.connector_context import conversation_context
        self.job.spec=reports.normalize(self.spec,self.feed);self.job.status='ready';self.job.save()
        with patch('kb.collector_api.start_async'):
            child=refine_draft(user=self.user,job=self.job,requirement='改为发送邮件给 fixture@example.com '+URL)
        self.assertNotIn('sig=',child.feedback)
        context=conversation_context(child)
        self.assertEqual(context['previous_report']['title'],self.kb.name)
        self.assertEqual(context['previous_connector']['url'],'https://example.com/hours')

    def test_collection_hook_preserves_success_when_report_delivery_fails(self):
        self.spec['schedule']={'mode':'after_collection'};self.plan.schedule_mode='after_collection'
        self.plan.spec=reports.normalize(self.spec,self.feed);self.plan.enabled=True;self.plan.save()
        with patch('kb.feed_reports.post_workflow',side_effect=TimeoutError) as post:
            run=self.publish()
        self.assertEqual(run.status,'completed');self.assertEqual(post.call_count,1)
        self.assertEqual(FeedReportDelivery.objects.get(run=run).status,'unknown')

    def test_question_tool_shows_only_requested_missing_fields(self):
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        fields=[{'key':'recipient','label':'报告发到哪个邮箱？','type':'email'}]
        model=FakeModel([call('ask_user_questions',{'questions':fields})])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):
            result=run(self.job.pk)
        self.assertEqual(result.status,'needs_info',result.error)
        self.assertEqual(result.questions,['报告发到哪个邮箱？'])
        self.assertEqual(result.preview['question_fields'][0]['type'],'email')
        self.assertEqual(result.spec,{})

    def test_switch_to_teams_asks_for_teams_link_instead_of_reusing_email(self):
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        model=FakeModel([call('configure_report_delivery',{'kind':'teams','public_base':'https://reports.example.com'}),call('preview_scheduled_report',{'report':self.spec},identity='report-2')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):
            result=run(self.job.pk)
        self.assertEqual(result.status,'needs_info',result.error)
        self.assertTrue(result.preview['setup_required']);self.assertIn('编辑配置',result.questions[0]);self.assertNotIn('question_fields',result.preview)
        self.plan.refresh_from_db();config=reports.destination(self.plan)
        self.assertEqual(config['kind'],'teams');self.assertEqual(config['url'],'')
        self.assertEqual(config['workflows']['email'],URL)
        self.assertFalse(self.plan.enabled)

    def test_question_form_rejects_executable_or_unsupported_input(self):
        for fields in [[{'label':'Code','type':'script'}],[{'label':'Choose','type':'select','options':['x']*20}],[{'key':'bad name','label':'Question'}]]:
            with self.assertRaises(ValueError):reports.question_fields(fields)

    def test_question_answer_continues_saved_report_conversation(self):
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        from kb.collector_api import refine_draft
        from kb.connector_context import conversation_context
        first=FakeModel([call('ask_user_questions',{'questions':[{'key':'recipient','label':'收件邮箱','type':'email'}]})])
        with patch('kb.agent._get_llm',return_value=first),patch('kb.config.llm_settings',return_value={}):
            parent=run(self.job.pk)
        with patch('kb.collector_api.start_async'):
            child=refine_draft(user=self.user,job=parent,requirement='收件邮箱：updated@example.com')
        context=conversation_context(child)
        self.assertEqual(context['previous_connector']['url'],'https://example.com/hours')
        second=FakeModel([call('configure_report_delivery',{'kind':'email','recipient':'updated@example.com'}),call('preview_scheduled_report',{'report':self.spec},identity='final-report')])
        with patch('kb.agent._get_llm',return_value=second),patch('kb.config.llm_settings',return_value={}):
            result=run(child.pk)
        self.assertEqual(result.status,'ready',result.error)
        self.plan.refresh_from_db()
        self.assertEqual(reports.destination(self.plan)['recipient'],'updated@example.com')
        self.assertFalse(self.plan.enabled)

    def test_delivery_summary_identifies_only_missing_details_without_url(self):
        from kb.feed_credentials import _cipher
        self.plan.destination=_cipher().encrypt(json.dumps({'kind':'email','url':URL}).encode()).decode()
        self.plan.save()
        state=reports.summary(self.feed)
        self.assertTrue(state['workflow_saved'])
        self.assertFalse(state['configured'])
        self.assertEqual(state['missing'],['public_base','recipient'])
        self.assertNotIn('sig=',json.dumps(state))


    @override_settings(FEED_REPORT_PUBLIC_BASE='')
    def test_workflow_link_alone_does_not_guess_delivery_type(self):
        self.plan.destination='';self.plan.save()
        text=reports.capture_feedback(self.user,self.feed,'工作流地址：'+URL)
        self.assertNotIn('sig=',text)
        state=reports.summary(self.feed)
        self.assertFalse(state['workflow_saved']);self.assertTrue(state['untyped_workflow_saved'])
        self.assertEqual(state['missing'],['kind','public_base'])


    def clear_site(self,*,base='',source='request'):
        from kb.feed_credentials import _cipher
        self.plan.refresh_from_db();config=reports.destination(self.plan)
        config.update(public_base=base,public_base_source=source)
        self.plan.destination=_cipher().encrypt(json.dumps(config).encode()).decode()
        self.plan.save()

    def test_refine_captures_external_app_address_before_model_starts(self):
        from kb.connector_context import conversation_context
        self.clear_site();self.job.status='needs_info';self.job.save()
        observed=[]
        def capture(job_id):
            child=ConnectorDraft.objects.get(pk=job_id)
            observed.append(conversation_context(child)['report_delivery'])
        with patch('kb.collector_api.start_async',side_effect=capture):
            response=self.client.post(reverse('kb:connector_refine',args=[self.job.pk]),json.dumps({'feedback':'报告用默认本站地址即可'}),content_type='application/json',HTTP_HOST='reports.example.com:8100')
        self.assertEqual(response.status_code,202,response.content)
        self.assertEqual(observed[0]['public_base'],'http://reports.example.com:8100')
        self.assertNotIn('public_base',observed[0]['missing'])

    def test_resume_captures_address_for_saved_task_and_preserves_enabled_plan(self):
        self.feed.adapter_spec={'url':'https://example.com/hours'};self.feed.save()
        self.clear_site();self.plan.enabled=True;self.plan.save()
        self.job.status='needs_info';self.job.save()
        response=self.client.post(reverse('kb:connector_resume'),json.dumps({'feed':str(self.feed.pk)}),content_type='application/json',secure=True,HTTP_HOST='reports.example.com:8443')
        self.assertEqual(response.status_code,200,response.content)
        self.plan.refresh_from_db()
        self.assertTrue(self.plan.enabled)
        self.assertEqual(reports.destination(self.plan)['public_base'],'https://reports.example.com:8443')

    @override_settings(USE_X_FORWARDED_HOST=False,SECURE_PROXY_SSL_HEADER=None)
    def test_untrusted_forwarded_headers_cannot_change_report_address(self):
        from django.test import RequestFactory
        self.clear_site()
        request=RequestFactory().get('/',HTTP_HOST='reports.example.com:8100',HTTP_X_FORWARDED_HOST='spoofed.example',HTTP_X_FORWARDED_PROTO='https')
        reports.remember_request_site(self.user,self.feed,request)
        self.assertEqual(reports.summary(self.feed)['public_base'],'http://reports.example.com:8100')

    @override_settings(ALLOWED_HOSTS=['reports.example.com'],USE_X_FORWARDED_HOST=True,SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https'))
    def test_trusted_proxy_preserves_external_https_host_and_port(self):
        from django.test import RequestFactory
        self.clear_site()
        request=RequestFactory().get('/',HTTP_HOST='localhost:8000',HTTP_X_FORWARDED_HOST='reports.example.com:8443',HTTP_X_FORWARDED_PROTO='https')
        reports.remember_request_site(self.user,self.feed,request)
        self.assertEqual(reports.summary(self.feed)['public_base'],'https://reports.example.com:8443')

    @override_settings(ALLOWED_HOSTS=['allowed.example.com'])
    def test_disallowed_host_is_not_saved(self):
        from django.test import RequestFactory
        self.clear_site()
        reports.remember_request_site(self.user,self.feed,RequestFactory().get('/',HTTP_HOST='untrusted.example.com'))
        self.assertFalse(reports.summary(self.feed)['site_address_available'])

    def test_localhost_does_not_replace_existing_public_origin(self):
        from django.test import RequestFactory
        self.clear_site(base='https://reports.example.com',source='request')
        reports.remember_request_site(self.user,self.feed,RequestFactory().get('/',HTTP_HOST='localhost:8000'))
        self.assertEqual(reports.summary(self.feed)['public_base'],'https://reports.example.com')
        for value in ['http://127.0.0.2:8000','http://0.0.0.0:8000','http://[::]:8000','http://[::ffff:127.0.0.1]','http://sub.localhost:8000','http://example.com:bad']:
            with self.subTest(value=value),self.assertRaises(ValueError):reports.public_base(value)

    @override_settings(FEED_REPORT_PUBLIC_BASE='https://deployed.example.com:8443/rag')
    def test_deployment_address_completes_config_without_user_site_input(self):
        from django.test import RequestFactory
        self.clear_site()
        reports.remember_request_site(self.user,self.feed,RequestFactory().get('/',HTTP_HOST='localhost:8000'))
        plan=reports.configure(self.user,self.feed,{'kind':'email','recipient':'fixture@example.com'})
        self.assertEqual(reports.destination(plan)['public_base'],'https://deployed.example.com:8443/rag')
        self.assertNotIn('public_base',reports.summary(self.feed)['missing'])

    @override_settings(FEED_REPORT_PUBLIC_BASE='https://deployed.example.com/rag')
    def test_background_report_uses_deployment_origin_without_http_request(self):
        self.clear_site()
        with patch('kb.feed_reports.post_workflow',return_value=202) as post:
            delivery=reports.send(self.plan,spec=self.spec,request_key='site-default-background')
        self.assertEqual(delivery.status,'accepted',delivery.error)
        self.assertIn('https://deployed.example.com/rag/kb/reports/download/',post.call_args.args[1]['b'])

    def test_mounted_app_prefix_is_in_download_link_exactly_once(self):
        from django.urls import get_script_prefix,set_script_prefix
        delivery=FeedReportDelivery.objects.create(plan=self.plan,request_key='mounted-link')
        old=get_script_prefix()
        try:
            set_script_prefix('/rag/')
            url=reports.download_link(delivery,'https://deployed.example.com/rag')
        finally:set_script_prefix(old)
        self.assertTrue(url.startswith('https://deployed.example.com/rag/kb/reports/download/'),url)
        self.assertNotIn('/rag/rag/',url)

    def test_auto_origin_updates_after_redeployment_and_manual_override_is_retained(self):
        from django.test import RequestFactory
        request=RequestFactory().get('/',HTTP_HOST='new.example.com:8100')
        self.clear_site(base='https://old.example.com',source='request')
        reports.remember_request_site(self.user,self.feed,request)
        self.assertEqual(reports.summary(self.feed)['public_base'],'http://new.example.com:8100')
        self.clear_site(base='https://chosen.example.com',source='manual')
        reports.remember_request_site(self.user,self.feed,request)
        self.assertEqual(reports.summary(self.feed)['public_base'],'https://chosen.example.com')

    @override_settings(FEED_REPORT_PUBLIC_BASE='https://deployed.example.com')
    def test_agent_checks_site_and_configures_without_asking_user(self):
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        self.clear_site()
        model=FakeModel([call('get_report_site_address',{}),call('configure_report_delivery',{'kind':'email'}),call('preview_scheduled_report',{'report':self.spec},identity='auto-site-report')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):
            result=run(self.job.pk)
        self.assertEqual(result.status,'ready',result.error)
        self.assertIn('https://deployed.example.com',str(model.messages))
        self.assertEqual(result.preview['delivery']['public_base'],'https://deployed.example.com')

    @override_settings(FEED_REPORT_PUBLIC_BASE='https://deployed.example.com')
    def test_known_app_address_is_removed_from_followup_questions(self):
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        self.clear_site()
        model=FakeModel([call('ask_user_questions',{'questions':[{'key':'public_base','label':'报告站点完整地址','type':'url'},{'key':'format','label':'报告需要怎样排版？','type':'textarea'}]})])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):
            result=run(self.job.pk)
        self.assertEqual(result.status,'needs_info',result.error)
        self.assertEqual(result.questions,['报告需要怎样排版？'])

    @override_settings(FEED_REPORT_PUBLIC_BASE='https://deployed.example.com')
    def test_redundant_site_question_does_not_pause_report_generation(self):
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        self.clear_site()
        model=FakeModel([call('ask_user_questions',{'questions':[{'key':'public_base','label':'报告站点地址','type':'url'}]}),call('preview_scheduled_report',{'report':self.spec},identity='continue-report')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):
            result=run(self.job.pk)
        self.assertEqual(result.status,'ready',result.error)
        self.assertEqual(result.questions,[])


    def history_rows(self,values):
        from kb.models import TrackerRow
        tracker=self.feed.tracker_rows.first().tracker
        self.feed.tracker_rows.all().delete()
        rows=[]
        for index,item in enumerate(values):
            rows.append(TrackerRow.objects.create(tracker=tracker,feed=self.feed,feed_run=self.run,external_id='history-'+str(index),status='done',values={'标题':'Ride '+str(index),**item}))
        return rows

    def approve_independent(self):
        self.job.spec,self.job.preview=reports.preview(self.job,self.spec);self.job.status='ready';self.job.save()
        reports.approve(self.job,self.user);self.plan.refresh_from_db()
        return self.plan

    def due_report(self):
        from datetime import timedelta
        self.approve_independent()
        due=timezone.now()-timedelta(minutes=1)
        FeedReportPlan.objects.filter(pk=self.plan.pk).update(next_run_at=due)
        return due

    def test_report_approval_sets_own_schedule_without_changing_collection(self):
        self.feed.cron='*/15 * * * *';self.feed.enabled=False;self.feed.save()
        before=self.feed.next_run_at
        self.approve_independent();self.feed.refresh_from_db()
        self.assertEqual(self.plan.schedule_mode,'cron');self.assertEqual(self.plan.cron,'0 9 * * mon')
        self.assertIsNotNone(self.plan.next_run_at)
        self.assertEqual(self.feed.cron,'*/15 * * * *');self.assertEqual(self.feed.next_run_at,before)
        self.assertFalse(self.feed.enabled)

    def test_collection_does_not_dispatch_independently_scheduled_report(self):
        self.approve_independent()
        with patch('kb.feed_reports.post_workflow') as post:
            run=self.publish();reports.after_run(run)
        self.assertEqual(run.status,'completed');post.assert_not_called()

    def test_due_report_sends_from_saved_data_even_if_collection_paused_or_failed(self):
        due=self.due_report()
        self.feed.enabled=False;self.feed.save();self.run.status='failed';self.run.save()
        with patch('kb.feeds.claim') as collect,patch('kb.feeds.execute') as execute_collect,patch('kb.feed_reports.post_workflow',return_value=202) as post:
            deliveries=reports.run_due()
        self.assertEqual(len(deliveries),1);self.assertEqual(deliveries[0].status,'accepted',deliveries[0].error)
        self.assertEqual(deliveries[0].scheduled_for,due)
        self.assertIsNone(deliveries[0].run_id)
        self.assertEqual(post.call_count,1);collect.assert_not_called();execute_collect.assert_not_called()

    def test_overlapping_ticks_claim_scheduled_occurrence_once(self):
        self.due_report();now=timezone.now()
        with patch('kb.feed_reports.post_workflow',return_value=202) as post:
            first=reports.run_due(now);second=reports.run_due(now)
        self.assertEqual(len(first),1);self.assertEqual(second,[]);self.assertEqual(post.call_count,1)
        self.plan.refresh_from_db();self.assertGreater(self.plan.next_run_at,now)

    def test_unknown_scheduled_delivery_is_not_resent_for_same_occurrence(self):
        due=self.due_report()
        with patch('kb.feed_reports.post_workflow',side_effect=TimeoutError) as post:
            first=reports.run_due()[0]
            FeedReportPlan.objects.filter(pk=self.plan.pk).update(next_run_at=due)
            self.assertEqual(reports.run_due(),[])
        self.assertEqual(first.status,'unknown');self.assertEqual(post.call_count,1)

    def test_paused_report_does_not_send(self):
        self.due_report();self.plan.enabled=False;self.plan.save()
        with patch('kb.feed_reports.post_workflow') as post:self.assertEqual(reports.run_due(),[])
        post.assert_not_called()

    def test_revoked_report_owner_disables_schedule_once_with_visible_error(self):
        self.due_report();self.user.is_active=False;self.user.save()
        with patch('kb.feed_reports.post_workflow') as post:
            first=reports.run_due();self.assertEqual(reports.run_due(),[])
        self.assertEqual(first[0].status,'failed');post.assert_not_called()
        self.plan.refresh_from_db();self.assertFalse(self.plan.enabled);self.assertIsNone(self.plan.next_run_at)

    def test_report_configuration_change_cancels_claimed_send(self):
        self.due_report();original=reports.workbook
        def change(*args):
            FeedReportPlan.objects.filter(pk=self.plan.pk).update(enabled=False)
            return original(*args)
        with patch('kb.feed_reports.workbook',side_effect=change),patch('kb.feed_reports.post_workflow') as post:
            delivery=reports.run_due()[0]
        self.assertEqual(delivery.status,'failed');self.assertIn('取消',delivery.error);post.assert_not_called()

    def test_manual_report_saves_rules_without_enabling_any_schedule(self):
        self.spec['schedule']={'mode':'manual'};self.approve_independent()
        self.assertEqual(self.plan.schedule_mode,'manual');self.assertFalse(self.plan.enabled)
        self.assertIsNone(self.plan.next_run_at)

    def test_missing_range_or_report_schedule_requires_user_information(self):
        for missing in ('range','schedule'):
            spec={key:value for key,value in self.spec.items() if key!=missing}
            with self.subTest(missing=missing),self.assertRaises(ValueError):reports.normalize(spec,self.feed)

    def test_raw_export_uses_selected_fields_across_all_saved_history(self):
        self.history_rows([{'Count':'10'},{'Count':'20'},{'Count':'30'}])
        spec=reports.normalize({**self.spec,'columns':[{'label':'Quantity','field':'Count'}]},self.feed)
        headers,rows=reports.table(self.feed,spec)
        self.assertEqual(headers,['Quantity']);self.assertEqual([row['Quantity'] for row in rows],['10','20','30'])

    def test_global_aggregation_streams_full_dataset_beyond_raw_export_limit(self):
        self.history_rows([{'Count':10},{'Count':20},{'Count':30},{'Count':40}])
        spec=reports.normalize({**self.spec,'columns':[{'label':'Quantity','field':'Count'}],'aggregates':[{'field':'Quantity','op':'sum','label':'Total'},{'field':'Quantity','op':'avg','label':'Average'},{'field':'Quantity','op':'count','label':'Records'}]},self.feed)
        with patch('kb.feed_reports.MAX_ROWS',2):headers,rows=reports.table(self.feed,spec)
        self.assertEqual(rows,[{'Total':100,'Average':25,'Records':4}])
        self.assertEqual(headers,['Total','Average','Records'])

    def test_grouped_aggregation_uses_selected_business_date_range(self):
        self.history_rows([{'Date':'2026-10-31','Attraction':'A','Count':100},{'Date':'2026-11-01','Attraction':'A','Count':10},{'Date':'2026-11-02','Attraction':'A','Count':20},{'Date':'2026-11-02','Attraction':'B','Count':5}])
        spec=reports.normalize({**self.spec,'range':{'mode':'dates','start':'2026-11-01','end':'2026-11-02','field':'Date'},'columns':[{'label':'Attraction','field':'Attraction'},{'label':'Quantity','field':'Count'}],'group_by':['Attraction'],'aggregates':[{'field':'Quantity','op':'sum','label':'Total'}]},self.feed)
        self.assertEqual(reports.table(self.feed,spec)[1],[{'Attraction':'A','Total':30},{'Attraction':'B','Total':5}])

    def test_date_window_uses_report_timezone_and_calendar_periods(self):
        from datetime import datetime,timezone as utc
        from zoneinfo import ZoneInfo
        at=datetime(2026,11,8,16,30,tzinfo=utc.utc);zone=ZoneInfo('Asia/Shanghai')
        cases=[({'mode':'last_week'},('2026-11-02','2026-11-08')),({'mode':'last_month'},('2026-10-01','2026-10-31')),({'mode':'days','days':7,'include_today':False},('2026-11-02','2026-11-08')),({'mode':'yesterday'},('2026-11-08','2026-11-08'))]
        for scope,wanted in cases:
            with self.subTest(scope=scope):self.assertEqual(tuple(day.isoformat() for day in reports.date_window(scope,at,zone)),wanted)

    def test_collection_date_range_includes_both_end_dates(self):
        from datetime import datetime,timezone as utc
        from kb.models import TrackerRow
        rows=self.history_rows([{'Count':1},{'Count':2},{'Count':3}])
        # Shanghai: Oct 31 23:59, Nov 1 00:00 and Nov 3 00:00.
        for row,stamp in zip(rows,[datetime(2026,10,31,15,59,tzinfo=utc.utc),datetime(2026,10,31,16,0,tzinfo=utc.utc),datetime(2026,11,2,16,0,tzinfo=utc.utc)]):TrackerRow.objects.filter(pk=row.pk).update(updated_at=stamp)
        spec=reports.normalize({**self.spec,'range':{'mode':'dates','start':'2026-11-01','end':'2026-11-02'},'columns':[{'label':'Quantity','field':'Count'}]},self.feed)
        self.assertEqual(reports.table(self.feed,spec)[1],[{'Quantity':2}])

    def test_raw_limit_applies_after_field_filters_without_truncating_input(self):
        self.history_rows([{'Count':1},{'Count':2},{'Count':3},{'Count':100}])
        spec=reports.normalize({**self.spec,'columns':[{'label':'Quantity','field':'Count'}],'filters':[{'field':'Count','op':'gte','value':100}]},self.feed)
        with patch('kb.feed_reports.MAX_ROWS',2):self.assertEqual(reports.table(self.feed,spec)[1],[{'Quantity':100}])

    def test_each_scheduled_occurrence_recalculates_current_saved_data(self):
        self.history_rows([{'Count':10}]);self.spec['columns']=[{'label':'Quantity','field':'Count'}]
        due=self.due_report()
        with patch('kb.feed_reports.post_workflow',return_value=202):first=reports.run_due()[0]
        self.history_rows([{'Count':10},{'Count':20}])
        from datetime import timedelta
        FeedReportPlan.objects.filter(pk=self.plan.pk).update(next_run_at=due+timedelta(seconds=1))
        with patch('kb.feed_reports.post_workflow',return_value=202):second=reports.run_due()[0]
        book1=load_workbook(io.BytesIO(bytes(first.content)));book2=load_workbook(io.BytesIO(bytes(second.content)))
        self.assertEqual(book1.active.max_row,2);self.assertEqual(book2.active.max_row,3)

    def test_due_report_uses_occurrence_date_for_window_when_tick_is_late(self):
        from datetime import datetime,timezone as utc
        self.history_rows([{'Date':'2026-10-26','Count':10},{'Date':'2026-11-02','Count':20}])
        self.spec.update(range={'mode':'last_week','field':'Date'},columns=[{'label':'Quantity','field':'Count'}])
        self.approve_independent()
        due=datetime(2026,11,2,1,0,tzinfo=utc.utc)
        FeedReportPlan.objects.filter(pk=self.plan.pk).update(next_run_at=due)
        with patch('kb.feed_reports.post_workflow',return_value=202):delivery=reports.run_due(datetime(2026,11,9,1,0,tzinfo=utc.utc))[0]
        self.assertEqual(delivery.status,'accepted',delivery.error)
        book=load_workbook(io.BytesIO(bytes(delivery.content)));self.assertEqual(book.active['A2'].value,10)

    def test_common_scheduler_scans_reports_before_collection_for_one_tick(self):
        from django.core.management import call_command
        order=[]
        with patch('kb.management.commands.run_feed_scheduler.run_reports_due',side_effect=lambda:order.append('report')),patch('kb.management.commands.run_feed_scheduler.run_due',side_effect=lambda:order.append('collection')):
            call_command('run_feed_scheduler',once=True,stdout=io.StringIO())
        self.assertEqual(order,['report','collection'])

    def test_long_collection_cannot_block_independent_report_loop(self):
        from django.core.management import call_command
        from threading import Event,current_thread
        ready=Event();threads=[]
        def report_loop(*args):threads.append(current_thread().name);ready.set()
        def slow_collection():
            self.assertTrue(ready.wait(2));raise KeyboardInterrupt
        with patch('kb.management.commands.run_feed_scheduler.watch_reports',side_effect=report_loop),patch('kb.management.commands.run_feed_scheduler.run_due',side_effect=slow_collection),patch('kb.management.commands.run_feed_scheduler.close_old_connections'):
            call_command('run_feed_scheduler',stdout=io.StringIO())
        self.assertEqual(threads,['report-scheduler'])


    def test_preview_date_is_current_and_next_delivery_date_is_separate(self):
        from datetime import datetime,timezone as utc
        self.history_rows([{'Date':'2026-11-07','Count':10},{'Date':'2026-10-31','Count':99}])
        self.spec.update(range={'mode':'last_week','field':'Date'},columns=[{'label':'Quantity','field':'Count'}])
        with patch('kb.feed_reports.timezone.now',return_value=datetime(2026,11,7,1,tzinfo=utc.utc)):
            _,preview=reports.preview(self.job,self.spec)
        self.assertEqual(preview['items'],1);self.assertEqual(preview['preview'],[{'Quantity':99}])
        self.assertTrue(preview['preview_as_of'].startswith('2026-11-07T01:00'))
        self.assertTrue(preview['next_send_at'].startswith('2026-11-09T01:00'))

    def test_empty_future_window_has_real_empty_preview_and_can_be_configured(self):
        self.spec.update(range={'mode':'dates','start':'2099-01-01','end':'2099-01-07'})
        _,preview=reports.preview(self.job,self.spec)
        self.assertTrue(preview['valid']);self.assertEqual(preview['items'],0);self.assertEqual(preview['preview'],[])
        self.assertIn('暂无数据',preview['warning'])
        self.approve_independent();self.assertTrue(self.plan.enabled)

    def test_existing_approved_collection_triggered_reports_survive_data_migration(self):
        import importlib
        from django.apps import apps
        self.plan.spec={'title':'Older report','columns':self.spec['columns']};self.plan.enabled=True;self.plan.save()
        importlib.import_module('kb.migrations.0050_independent_report_schedules').retain_legacy_reports(apps,None)
        self.plan.refresh_from_db()
        self.assertTrue(self.plan.enabled);self.assertEqual(self.plan.schedule_mode,'after_collection')
        self.assertEqual(self.plan.spec['range']['mode'],'latest')
        self.assertEqual(self.plan.spec['schedule']['mode'],'after_collection')

    def test_report_approval_requires_complete_delivery_details(self):
        from kb.feed_credentials import _cipher
        config=reports.destination(self.plan);config['recipient']=''
        self.plan.destination=_cipher().encrypt(json.dumps(config).encode()).decode();self.plan.save()
        self.job.spec,self.job.preview=reports.preview(self.job,self.spec);self.job.status='ready';self.job.save()
        with self.assertRaisesRegex(ValueError,'收件邮箱'):reports.approve(self.job,self.user)


    def test_email_and_teams_workflows_are_saved_and_selected_by_type(self):
        teams=URL.replace('/fixture/','/teams-fixture/')
        reports.configure(self.user,self.feed,{'kind':'teams','url':teams})
        state=reports.summary(self.feed)
        self.assertTrue(state['configured']);self.assertEqual(state['workflow_type'],'teams')
        self.assertEqual(state['saved_workflow_types'],['email','teams']);self.assertEqual(state['recipient'],'')
        self.assertNotIn('sig=',json.dumps(state))
        plan=reports.configure(self.user,self.feed,{'kind':'email'})
        self.assertEqual(reports.selected_workflow(reports.destination(plan)),URL)
        plan=reports.configure(self.user,self.feed,{'kind':'teams'})
        self.assertEqual(reports.selected_workflow(reports.destination(plan)),teams)

    def test_missing_teams_link_never_posts_to_existing_email_webhook(self):
        plan=reports.configure(self.user,self.feed,{'kind':'teams'})
        state=reports.summary(self.feed)
        self.assertFalse(state['configured']);self.assertFalse(state['workflow_saved'])
        self.assertEqual(state['missing'],['workflow_url'])
        with patch('kb.feed_reports.post_workflow') as post:
            delivery=reports.send(plan,spec=self.spec)
        self.assertEqual(delivery.status,'failed');self.assertIn('Teams频道',delivery.error);post.assert_not_called()

    def test_teams_link_answer_resumes_report_without_losing_report_rules(self):
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        from kb.collector_api import refine_draft
        teams=URL.replace('/fixture/','/teams-fixture/')
        reports.configure(self.user,self.feed,{'kind':'teams'})
        first=FakeModel([call('preview_scheduled_report',{'report':self.spec})])
        with patch('kb.agent._get_llm',return_value=first),patch('kb.config.llm_settings',return_value={}):waiting=run(self.job.pk)
        self.assertEqual(waiting.status,'needs_info');self.assertEqual(waiting.spec['kind'],'report')
        self.assertTrue(waiting.preview['setup_required']);self.assertIn('编辑配置',waiting.questions[0])
        reports.save_workflow_settings(self.user,self.feed,{'teams_workflow_url':teams})
        with patch('kb.collector_api.start_async'):
            child=refine_draft(user=self.user,job=waiting,requirement='已在编辑配置保存，继续')
        self.assertNotIn('sig=',child.feedback)
        model=FakeModel([call('preview_scheduled_report',{'report':self.spec})])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):ready=run(child.pk)
        self.assertEqual(ready.status,'ready',ready.error);self.assertEqual(ready.preview['delivery']['workflow_type'],'teams')
        self.assertEqual(ready.spec['schedule']['cron'],self.spec['schedule']['cron'])

    def test_unknown_link_type_is_saved_until_user_chooses_type(self):
        self.plan.destination='';self.plan.save()
        reports.capture_feedback(self.user,self.feed,'工作流：'+URL)
        state=reports.summary(self.feed)
        self.assertTrue(state['untyped_workflow_saved']);self.assertFalse(state['workflow_saved'])
        plan=reports.configure(self.user,self.feed,{'kind':'teams','public_base':'https://reports.example.com'})
        self.assertEqual(reports.selected_workflow(reports.destination(plan)),URL)

    def test_plain_question_finishes_with_answer_without_any_builder_tool(self):
        from langchain_core.messages import AIMessageChunk
        from kb.tests.test_connector import FakeModel
        from kb.connector_agent import run
        self.job.feedback='为什么上一次报告没有数据？请解释，不要修改。';self.job.save()
        text='上次预览使用了下次发送日期，因此当天的数据不在那个日期范围内。'
        model=FakeModel([AIMessageChunk(content=text)])
        original_spec=self.feed.adapter_spec;original_cron=self.feed.cron
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}),patch('kb.feed_reports.preview') as preview,patch('kb.connector_agent.read_url') as fetch:
            result=run(self.job.pk)
        self.assertEqual(result.status,'answered',result.error);self.assertEqual(result.preview['answer'],text)
        self.assertEqual(result.metrics['calls'],1);self.assertEqual(result.metrics['tools'],0)
        self.assertEqual(result.spec,{});preview.assert_not_called();fetch.assert_not_called()
        self.feed.refresh_from_db();self.assertEqual(self.feed.cron,original_cron);self.assertEqual(self.feed.adapter_spec,original_spec)

    def test_structured_answer_is_displayed_and_next_chat_turn_preserves_context(self):
        from langchain_core.messages import AIMessageChunk
        from kb.tests.test_connector import FakeModel
        from kb.connector_agent import run
        from kb.connector_context import conversation_context
        model=FakeModel([AIMessageChunk(content=json.dumps({'answer':'报告计划和采集计划相互独立。'}))])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):answered=run(self.job.pk)
        self.assertEqual(answered.status,'answered',answered.error)
        with patch('kb.collector_api.start_async'):
            response=self.client.post(reverse('kb:connector_refine',args=[answered.pk]),json.dumps({'feedback':'那改成每周一上午九点发送'}),content_type='application/json')
        self.assertEqual(response.status_code,202,response.content)
        child=ConnectorDraft.objects.get(pk=response.json()['job']['id'])
        context=conversation_context(child)
        self.assertEqual(context['previous_connector']['url'],'https://example.com/hours')
        self.assertEqual(context['conversation'][-1]['assistant_answer'],'报告计划和采集计划相互独立。')
        replay=self.client.get(reverse('kb:connector_status',args=[answered.pk])).json()['job']
        self.assertEqual(replay['preview']['answer'],'报告计划和采集计划相互独立。')

    def test_current_clock_is_refreshed_across_midnight_in_resumed_conversation(self):
        from datetime import datetime,timezone as utc
        from langchain_core.messages import AIMessageChunk
        from kb.tests.test_connector import FakeModel
        from kb.connector_agent import run
        from kb.collector_api import refine_draft
        first=FakeModel([AIMessageChunk(content='现在是11月7日。')])
        with patch('django.utils.timezone.now',return_value=datetime(2026,11,7,15,59,tzinfo=utc.utc)),patch('kb.agent._get_llm',return_value=first),patch('kb.config.llm_settings',return_value={}):answered=run(self.job.pk)
        with patch('kb.collector_api.start_async'):child=refine_draft(user=self.user,job=answered,requirement='现在是几号？')
        second=FakeModel([AIMessageChunk(content='现在是11月8日。')])
        with patch('django.utils.timezone.now',return_value=datetime(2026,11,7,16,1,tzinfo=utc.utc)),patch('kb.agent._get_llm',return_value=second),patch('kb.config.llm_settings',return_value={}):result=run(child.pk)
        self.assertEqual(result.status,'answered',result.error)
        self.assertIn('"current_date": "2026-11-08"',second.messages[0][-1].content)
        self.assertIn('"timezone": "Asia/Shanghai"',second.messages[0][-1].content)

    def test_clock_tool_uses_runtime_clock_and_requested_timezone(self):
        from datetime import datetime,timezone as utc
        from kb.connector_context import clock_context
        with patch('django.utils.timezone.now',return_value=datetime(2026,11,7,17,tzinfo=utc.utc)):
            self.assertEqual(clock_context()['current_date'],'2026-11-08')
            self.assertEqual(clock_context('UTC')['current_date'],'2026-11-07')
        with self.assertRaises(ValueError):clock_context('not-a-timezone')

    def test_history_tool_explains_saved_failure_without_refetching(self):
        from langchain_core.messages import AIMessageChunk
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        from kb.models import ConnectorEvent
        ConnectorEvent.objects.create(job=self.parent,kind='tool_end',payload={'tool':'preview_scheduled_report','ok':False,'message':'选定的日期范围没有数据。'})
        model=FakeModel([call('inspect_task_history'),AIMessageChunk(content='历史记录显示，选定日期范围没有数据。')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}),patch('kb.connector_agent.read_url') as fetch,patch('kb.feed_reports.preview') as preview:
            result=run(self.job.pk)
        self.assertEqual(result.status,'answered',result.error);self.assertEqual(result.metrics['tools'],1)
        self.assertIn('选定的日期范围没有数据',str(model.messages[-1]));fetch.assert_not_called();preview.assert_not_called()

    def test_daily_today_preview_does_not_become_tomorrow_after_send_time(self):
        from datetime import datetime,timezone as utc
        self.history_rows([{'Date':'2026-11-07','Count':10}])
        self.spec.update(range={'mode':'today','field':'Date'},columns=[{'label':'Quantity','field':'Count'}],schedule={'mode':'cron','cron':'15 19 * * *','timezone':'Asia/Shanghai'})
        with patch('django.utils.timezone.now',return_value=datetime(2026,11,7,12,tzinfo=utc.utc)):
            _,preview=reports.preview(self.job,self.spec)
        self.assertEqual(preview['preview'],[{'Quantity':10}])
        self.assertTrue(preview['preview_as_of'].startswith('2026-11-07T12:00'))
        self.assertTrue(preview['next_send_at'].startswith('2026-11-08T11:15'))


    def test_builder_accepts_short_first_question_as_chat(self):
        from langchain_core.messages import AIMessageChunk
        from kb.tests.test_connector import FakeModel
        from kb.connector_agent import run
        self.job.status='cancelled';self.job.save()
        with patch('kb.collector_api.start_async'):
            reply=self.client.post(reverse('kb:connector_create'),json.dumps({'prompt':'你好'}),content_type='application/json')
        self.assertEqual(reply.status_code,202,reply.content)
        model=FakeModel([AIMessageChunk(content='你好，有什么想了解的？')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):result=run(reply.json()['job']['id'])
        self.assertEqual(result.status,'answered',result.error);self.assertEqual(result.metrics['tools'],0)


    def test_report_aggregation_converts_numeric_strings_instead_of_discarding_them(self):
        self.history_rows([{'Count':10},{'Count':'20'},{'Count':'1,200'},{'Count':'0'}])
        spec=reports.normalize({**self.spec,'columns':[{'label':'Quantity','field':'Count'}],'aggregates':[{'field':'Quantity','op':'sum','label':'Total'},{'field':'Quantity','op':'count','label':'Samples'}]},self.feed)
        self.assertEqual(reports.table(self.feed,spec)[1],[{'Total':1230,'Samples':4}])

    def test_regex_postprocessing_handles_units_locales_and_scaling_with_raw_values_preserved(self):
        from kb.connector_calculations import evaluate_computed
        cases=[(15,{},15),('15',{},15),('1,200',{},1200),('约 1,200 分钟',{'pattern':r'([0-9,]+)\s*分钟','group':1},1200),
               ('1.234,50 min',{'pattern':r'([0-9.,]+)\s*min','group':1,'decimal_separator':',','thousands_separator':'.'},1234.5),
               ('1.5 小时',{'pattern':r'([0-9.]+)\s*小时','group':1,'scale':60},90)]
        for value,options,wanted in cases:
            source={'raw':value};rule={'op':'parse_number','args':[{'field':'raw'}],**options}
            with self.subTest(value=value):self.assertEqual(evaluate_computed(source,rule),wanted);self.assertEqual(source['raw'],value)

    def test_ambiguous_or_unavailable_numeric_text_is_not_guessed_or_filled_with_zero(self):
        from kb.connector_calculations import evaluate_computed
        rule={'op':'parse_number','args':[{'field':'raw'}]}
        for value in ('官网未公布排队时长','10–20分钟',True):
            with self.subTest(value=value),self.assertRaises(ValueError):evaluate_computed({'raw':value},rule)
        self.assertIsNone(evaluate_computed({'raw':'官网未公布排队时长'},{**rule,'on_error':'null'}))
        self.assertIsNone(evaluate_computed({'raw':'10 20'},{**rule,'pattern':r'\d+','on_error':'null'}))
        self.assertEqual(evaluate_computed({'raw':'0'},rule),0)

    def test_contains_on_numeric_inputs_is_safe_and_preserves_container_semantics(self):
        from kb.connector_calculations import evaluate_computed
        rule={'op':'contains','args':[{'field':'raw'},'未公布']}
        self.assertFalse(evaluate_computed({'raw':15},rule))
        self.assertTrue(evaluate_computed({'raw':'官网未公布排队时长'},rule))
        self.assertTrue(evaluate_computed({'raw':['未公布','other']},rule))

    def test_conversion_preview_reports_original_values_and_missing_counts(self):
        self.history_rows([{'Count':'15 分钟'},{'Count':'25 分钟'},{'Count':'官网未公布排队时长'}])
        rule={'op':'parse_number','args':[{'field':'Count'}],'pattern':r'([0-9]+)\s*分钟','group':1,'on_error':'null'}
        self.spec.update(columns=[{'label':'Minutes','computed':rule}],aggregates=[{'field':'Minutes','op':'avg','label':'Average'},{'field':'Minutes','op':'count','label':'ValidSamples'}])
        _,preview=reports.preview(self.job,self.spec)
        self.assertEqual(preview['preview'],[{'Average':20,'ValidSamples':2}])
        audit=preview['transformation_audit']['Minutes']
        self.assertEqual(audit['converted'],2);self.assertEqual(audit['missing'],1)
        self.assertEqual(audit['examples'][0]['input']['Count'],'15 分钟')
        self.assertIn('官网未公布排队时长',str(list(self.feed.tracker_rows.values_list('values',flat=True))))

    def test_entirely_missing_numeric_group_does_not_claim_zero_total(self):
        self.history_rows([{'Count':'未公布'}])
        self.spec.update(columns=[{'label':'Minutes','computed':{'op':'parse_number','args':[{'field':'Count'}],'on_error':'null'}}],aggregates=[{'field':'Minutes','op':'sum','label':'Total'},{'field':'Minutes','op':'count','label':'Samples'}])
        _,preview=reports.preview(self.job,self.spec)
        self.assertEqual(preview['preview'],[{'Total':'','Samples':0}])

    def test_agent_can_test_regex_conversion_on_distinct_real_historical_values(self):
        from langchain_core.messages import AIMessageChunk
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        self.history_rows([{'Count':'15 分钟'},{'Count':20},{'Count':'未公布'}])
        rule={'op':'parse_number','args':[{'field':'Count'}],'pattern':r'([0-9]+)\s*分钟','group':1,'on_error':'null'}
        model=FakeModel([call('preview_report_calculation',{'computed':rule}),AIMessageChunk(content='数字字符串和数值均可转换，未公布值保留为空。')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):result=run(self.job.pk)
        self.assertEqual(result.status,'answered',result.error)
        self.assertIn('15 分钟',str(model.messages[-1]));self.assertIn('20',str(model.messages[-1]))


    def test_quality_summary_is_delivered_and_exported_with_raw_samples(self):
        self.history_rows([{'Count':'15 分钟'},{'Count':'未公布'}])
        self.spec.update(columns=[{'label':'Minutes','computed':{'op':'parse_number','args':[{'field':'Count'}],'pattern':r'([0-9]+)\s*分钟','group':1,'on_error':'null'}}],aggregates=[{'field':'Minutes','op':'avg','label':'Average'}])
        with patch('kb.feed_reports.post_workflow',return_value=202) as post:delivery=reports.send(self.plan,spec=self.spec)
        self.assertEqual(delivery.status,'accepted',delivery.error)
        self.assertIn('1条有效，1条缺失/未解析',post.call_args.args[1]['b'])
        book=load_workbook(io.BytesIO(bytes(delivery.content)))
        self.assertIn('Transformations',book.sheetnames)
        self.assertIn('15 分钟',book['Transformations']['D2'].value)

    def test_ambiguous_decimal_separator_requires_an_explicit_rule(self):
        from kb.connector_calculations import evaluate_computed
        rule={'op':'parse_number','args':[{'field':'raw'}]}
        with self.assertRaises(ValueError):evaluate_computed({'raw':'1,2'},rule)
        self.assertEqual(evaluate_computed({'raw':'1,2'},{**rule,'decimal_separator':',','thousands_separator':''}),1.2)


    def test_structured_teams_answer_uses_field_type_not_email_words_in_label(self):
        from kb.collector_api import refine_draft
        teams=URL.replace('/fixture/','/structured-teams/')
        label='请提供Teams工作流链接。当前只保存了邮件工作流，邮件链接不能用于Teams。'
        self.job.status='needs_info';self.job.questions=[label]
        self.job.preview={'question_fields':[{'key':'teams_workflow_url','label':label,'type':'url','required':True,'workflow_kind':'teams'}]};self.job.save()
        with patch('kb.collector_api.start_async'):
            child=refine_draft(user=self.user,job=self.job,requirement=label+'：'+teams,question_answers=[{'key':'teams_workflow_url','value':teams}])
        self.plan.refresh_from_db();config=reports.destination(self.plan)
        self.assertEqual(config['workflows']['email'],URL);self.assertEqual(config['workflows']['teams'],teams)
        self.assertEqual(config['kind'],'teams');self.assertNotIn('sig=',child.feedback)

    def test_form_http_transport_ignores_example_url_in_question_label(self):
        teams=URL.replace('/fixture/','/http-teams/')
        label='Teams链接（示例 https://prod-xx.westus.logic.azure.com/workflows/example）'
        self.job.status='needs_info';self.job.questions=[label]
        self.job.preview={'question_fields':[{'key':'teams_workflow_url','label':label,'type':'url','required':True}]};self.job.save()
        with patch('kb.collector_api.start_async'):
            response=self.client.post(reverse('kb:connector_refine',args=[self.job.pk]),json.dumps({'feedback':label+'：'+teams,'question_answers':[{'key':'teams_workflow_url','value':teams}]}),content_type='application/json')
        self.assertEqual(response.status_code,202,response.content)
        self.assertNotIn('sig=',response.json()['job']['feedback'])
        self.plan.refresh_from_db();self.assertEqual(reports.selected_workflow(reports.destination(self.plan)),teams)

    def test_ambiguous_label_saves_pending_url_instead_of_overwriting_email_binding(self):
        teams=URL.replace('/fixture/','/pending-teams/')
        reports.capture_feedback(self.user,self.feed,'Teams频道工作流；当前只保存了邮件工作流：'+teams)
        self.plan.refresh_from_db();config=reports.destination(self.plan)
        self.assertEqual(config['workflows']['email'],URL);self.assertEqual(config['pending_url'],teams)
        selected=reports.configure(self.user,self.feed,{'kind':'teams'})
        self.assertEqual(reports.selected_workflow(reports.destination(selected)),teams)

    def test_saved_link_does_not_suppress_question_for_a_different_type(self):
        self.assertFalse(reports.redundant_workflow_question({'key':'teams_workflow_url','label':'Teams频道工作流链接'},reports.summary(self.feed)))
        self.assertTrue(reports.redundant_workflow_question({'key':'email_workflow_url','label':'邮件工作流链接'},reports.summary(self.feed)))

    def test_raw_json_questions_cannot_repeat_request_for_already_saved_link(self):
        from langchain_core.messages import AIMessageChunk
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        teams=URL.replace('/fixture/','/known-teams/')
        reports.configure(self.user,self.feed,{'kind':'teams','url':teams})
        model=FakeModel([AIMessageChunk(content=json.dumps({'questions':['请提供Teams频道工作流链接']})),call('preview_scheduled_report',{'report':self.spec})])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):result=run(self.job.pk)
        self.assertEqual(result.status,'ready',result.error);self.assertEqual(result.questions,[])


    def report_job(self,kind,recipient='fixture@example.com',status='ready'):
        teams=URL.replace('/fixture/','/teams-fixture/')
        reports.configure(self.user,self.feed,{'kind':kind,'url':teams if kind=='teams' else URL,'recipient':recipient,'public_base':'https://reports.example.com'})
        self.job.spec,self.job.preview=reports.preview(self.job,self.spec)
        self.job.status=status;self.job.save()
        return self.job

    def test_http_test_send_uses_report_target_after_another_target_was_selected(self):
        job=self.report_job('teams',status='revised')
        reports.configure(self.user,self.feed,{'kind':'email','recipient':'new@example.com'})
        url=reverse('kb:report_test',args=[job.pk])
        with patch('kb.feed_reports.post_workflow',return_value=202) as post:
            response=self.client.post(url,json.dumps({'request_key':'same-click-key'}),content_type='application/json')
            repeat=self.client.post(url,json.dumps({'request_key':'same-click-key'}),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content);self.assertEqual(repeat.status_code,200)
        self.assertEqual(post.call_count,1)
        config,body=post.call_args.args
        self.assertEqual(config['kind'],'teams');self.assertIn('/teams-fixture/',config['url'])
        self.assertEqual(body['attachments'][0]['content']['type'],'AdaptiveCard');self.assertNotIn('t',body)
        self.assertEqual(reports.delivery_context(job)['kind'],'teams')
        self.plan.refresh_from_db();self.assertEqual(reports.destination(self.plan)['kind'],'email')

    def test_email_test_keeps_original_recipient_when_later_draft_changes_it(self):
        job=self.report_job('email',recipient='original@example.com')
        reports.configure(self.user,self.feed,{'kind':'email','recipient':'later@example.com'})
        with patch('kb.feed_reports.post_workflow',return_value=202) as post:
            response=self.client.post(reverse('kb:report_test',args=[job.pk]),json.dumps({'request_key':'recipient-key'}),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(post.call_args.args[1]['t'],'original@example.com')

    def test_frozen_report_selection_resolves_replacement_url_from_encrypted_config(self):
        job=self.report_job('teams')
        replacement=URL.replace('/fixture/','/replacement-teams/')
        reports.save_workflow_settings(self.user,self.feed,{'teams_workflow_url':replacement})
        self.assertNotIn('sig=',json.dumps(job.spec)+json.dumps(job.preview))
        with patch('kb.feed_reports.post_workflow',return_value=202) as post:
            response=self.client.post(reverse('kb:report_test',args=[job.pk]),json.dumps({'request_key':'replacement-key'}),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(post.call_args.args[0]['url'],replacement)

    def test_unvalidated_report_cannot_be_test_sent_even_with_ready_status(self):
        self.job.spec=reports.normalize(self.spec,self.feed);self.job.status='ready';self.job.preview={};self.job.save()
        with patch('kb.feed_reports.post_workflow') as post:
            response=self.client.post(reverse('kb:report_test',args=[self.job.pk]),json.dumps({'request_key':'invalid-preview'}),content_type='application/json')
        self.assertEqual(response.status_code,400);post.assert_not_called()

    def test_approved_report_delivery_survives_normalization_and_later_selection(self):
        job=self.report_job('teams')
        reports.approve(job,self.user)
        self.plan.refresh_from_db();self.assertEqual(self.plan.spec['delivery']['kind'],'teams')
        reports.configure(self.user,self.feed,{'kind':'email'})
        self.plan.refresh_from_db()
        with patch('kb.feed_reports.post_workflow',return_value=202) as post:
            delivery=reports.send(self.plan,request_key='bound-approved-report')
        self.assertEqual(delivery.status,'accepted',delivery.error)
        self.assertEqual(post.call_args.args[0]['kind'],'teams')
