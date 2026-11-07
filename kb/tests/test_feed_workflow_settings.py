import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase,override_settings
from django.urls import reverse
from django.utils import timezone
from kb import feed_reports as reports
from kb.models import KnowledgeBase,KnowledgeFeed,ConnectorDraft,FeedReportPlan

EMAIL='https://tenant.environment.api.powerplatform.com/workflows/email/triggers/manual/paths/invoke?sig=email-fixture'
TEAMS=EMAIL.replace('/email/','/teams/').replace('sig=email','sig=teams')

@override_settings(FEED_REPORT_PUBLIC_BASE='https://reports.example.test')
class FeedWorkflowSettingsTests(TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        override=override_settings(DATA_DIR=Path(folder.name));override.enable();self.addCleanup(override.disable)
        self.owner=get_user_model().objects.create_user('builder',is_staff=True)
        self.other=get_user_model().objects.create_user('other-admin',is_staff=True)
        self.kb=KnowledgeBase.objects.create(name='Reports',slug='workflow-config',is_folder=True)
        self.feed=KnowledgeFeed.objects.create(kb=self.kb,name='Collector',mode='pull',url='https://example.test/data',cron='0 8 * * *',created_by=self.owner,allow_private=True,next_run_at=timezone.now())
        self.client.force_login(self.owner)

    def action(self,**data):
        return self.client.post(reverse('kb:feed_action'),json.dumps({'action':'update','id':str(self.feed.pk),'name':self.feed.name,'mode':'pull','url':self.feed.url,'cron':self.feed.cron,**data}),content_type='application/json',HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_owner_saves_two_typed_links_encrypted_without_changing_collector(self):
        next_run_at=self.feed.next_run_at
        with patch('kb.feed_reports.post_workflow') as post:
            response=self.action(email_workflow_url=EMAIL,teams_workflow_url=TEAMS)
            self.assertEqual(response.status_code,200,response.content);post.assert_not_called()
        self.feed.refresh_from_db();plan=self.feed.report_plan
        self.assertEqual(reports.workflow_bindings(reports.destination(plan)),{'email':EMAIL,'teams':TEAMS})
        self.assertNotIn('sig=',plan.destination)
        self.assertEqual(self.feed.created_by,self.owner);self.assertTrue(self.feed.allow_private)
        self.assertEqual(self.feed.next_run_at,next_run_at);self.assertEqual(self.feed.cron,'0 8 * * *');self.assertFalse(plan.enabled)
        self.assertEqual(response.json()['feed']['workflow_settings']['saved_workflow_types'],['email','teams'])
        for name in ('feed_state','report_configuration'):
            url=reverse('kb:'+name,args=[self.feed.pk]) if name=='report_configuration' else reverse('kb:'+name)
            result=self.client.get(url);self.assertEqual(result.status_code,200)
            self.assertNotIn('sig=',result.content.decode())

    def test_blank_fields_keep_links_and_other_type_change_keeps_active_plan(self):
        reports.configure(self.owner,self.feed,{'kind':'email','url':EMAIL,'recipient':'test@example.test'})
        plan=self.feed.report_plan;plan.enabled=True;plan.spec={'title':'Kept'};plan.save()
        response=self.action(email_workflow_url='',teams_workflow_url=TEAMS)
        self.assertEqual(response.status_code,200,response.content)
        plan.refresh_from_db();self.assertTrue(plan.enabled);self.assertEqual(plan.spec,{'title':'Kept'})
        self.assertEqual(reports.selected_workflow(reports.destination(plan)),EMAIL)
        response=self.action(email_workflow_url=EMAIL.replace('email-fixture','replacement'))
        self.assertEqual(response.status_code,200)
        plan.refresh_from_db();self.assertTrue(plan.enabled);self.assertEqual(plan.spec,{'title':'Kept'})

    def test_invalid_second_url_rolls_back_feed_and_first_url(self):
        response=self.action(name='Should roll back',email_workflow_url=EMAIL,teams_workflow_url='https://evil.example/invoke')
        self.assertEqual(response.status_code,400)
        self.feed.refresh_from_db();self.assertEqual(self.feed.name,'Collector')
        self.assertFalse(FeedReportPlan.objects.filter(feed=self.feed).exists())

    def test_other_admin_cannot_edit_or_take_ownership_through_any_write_path(self):
        self.client.force_login(self.other)
        state=self.client.get(reverse('kb:feed_state')).json()['feeds'][0]
        self.assertFalse(state['can_edit'])
        self.assertEqual(self.action(email_workflow_url=EMAIL).status_code,403)
        for command in ('pause','resume','rotate_token'):
            self.assertEqual(self.action(action=command).status_code,403)
        for method in (lambda:reports.save_workflow_settings(self.other,self.feed,{'teams_workflow_url':TEAMS}),lambda:reports.configure(self.other,self.feed,{'kind':'teams','url':TEAMS}),lambda:reports.capture_feedback(self.other,self.feed,'Teams '+TEAMS)):
            with self.assertRaisesMessage(ValueError,'创建者'):method()
        self.feed.refresh_from_db();self.assertEqual(self.feed.created_by,self.owner)
        self.assertFalse(FeedReportPlan.objects.filter(feed=self.feed).exists())

    def test_missing_link_tool_request_directs_to_settings_without_secret_form(self):
        from kb.tests.test_connector import FakeModel,call
        from kb.connector_agent import run
        reports.configure(self.owner,self.feed,{'kind':'teams'})
        parent=ConnectorDraft.objects.create(user=self.owner,kb=self.kb,feed=self.feed,status='enabled')
        job=ConnectorDraft.objects.create(user=self.owner,kb=self.kb,parent=parent,status='running',feedback='Send a Teams report')
        model=FakeModel([call('ask_user_questions',{'questions':[{'key':'teams_workflow_url','label':'Teams 工作流链接','type':'url','workflow_kind':'teams'}]})])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={}):result=run(job.pk)
        self.assertEqual(result.status,'needs_info',result.error)
        self.assertTrue(result.preview['setup_required']);self.assertIn('编辑配置',result.questions[0])
        self.assertNotIn('question_fields',result.preview);self.assertIn('edit='+str(self.feed.pk),result.preview['settings_url'])

    def test_saved_links_do_not_require_reentry_before_choosing_type(self):
        reports.save_workflow_settings(self.owner,self.feed,{'email_workflow_url':EMAIL,'teams_workflow_url':TEAMS})
        state=reports.summary(self.feed)
        self.assertIn('kind',state['missing']);self.assertNotIn('workflow_url',state['missing'])
        plan=reports.configure(self.owner,self.feed,{'kind':'teams'})
        self.assertEqual(reports.selected_workflow(reports.destination(plan)),TEAMS)

    def test_deleted_creator_does_not_grant_other_admin_edit_access(self):
        self.owner.delete();self.feed.refresh_from_db()
        with self.assertRaises(ValueError):reports.save_workflow_settings(self.other,self.feed,{'teams_workflow_url':TEAMS})

    def test_explicit_settings_replace_ambiguous_chat_binding(self):
        reports.capture_feedback(self.owner,self.feed,'工作流：'+EMAIL)
        self.assertTrue(reports.summary(self.feed)['untyped_workflow_saved'])
        reports.save_workflow_settings(self.owner,self.feed,{'teams_workflow_url':TEAMS})
        plan=reports.configure(self.owner,self.feed,{'kind':'teams'})
        self.assertEqual(reports.selected_workflow(reports.destination(plan)),TEAMS)
        self.assertNotIn('pending_url',reports.destination(plan))

    def test_old_untyped_url_cannot_override_existing_typed_binding(self):
        reports.configure(self.owner,self.feed,{'kind':'teams','url':TEAMS})
        reports.capture_feedback(self.owner,self.feed,'工作流：'+EMAIL)
        plan=reports.configure(self.owner,self.feed,{'kind':'teams'})
        self.assertEqual(reports.selected_workflow(reports.destination(plan)),TEAMS)

    def test_non_creator_cannot_configure_send_or_approve_reports_through_http(self):
        reports.configure(self.owner,self.feed,{'kind':'teams','url':TEAMS})
        job=ConnectorDraft.objects.create(user=self.other,kb=self.kb,feed=self.feed,status='ready',spec={'kind':'report'})
        self.client.force_login(self.other)
        with patch('kb.feed_reports.post_workflow') as post:
            response=self.client.post(reverse('kb:report_configure',args=[self.feed.pk]),json.dumps({'kind':'email','url':EMAIL}),content_type='application/json')
            self.assertGreaterEqual(response.status_code,400)
            response=self.client.post(reverse('kb:report_test',args=[job.pk]),json.dumps({'request_key':'creator-check'}),content_type='application/json')
            self.assertGreaterEqual(response.status_code,400);post.assert_not_called()
            with self.assertRaisesMessage(ValueError,'创建者'):reports.approve(job,self.other)
        self.feed.refresh_from_db()
        self.assertEqual(reports.workflow_bindings(reports.destination(self.feed.report_plan)),{'teams':TEAMS})

    def test_saved_teams_binding_drives_real_report_payload_not_email(self):
        # A real saved tracker row is projected into a workbook, with only the final HTTP boundary mocked.
        from kb.feeds import claim,execute
        self.feed.mode='push';self.feed.save()
        payload={'schema':'fos-rag-feed/v1','items':[{'id':'ride','title':'Ride','content':'Hours','fields':{'Open':'09:00','Close':'21:00'}}]}
        run,_=claim(self.feed.pk,trigger='push',payload=payload);execute(run.pk)
        reports.save_workflow_settings(self.owner,self.feed,{'email_workflow_url':EMAIL,'teams_workflow_url':TEAMS})
        plan=reports.configure(self.owner,self.feed,{'kind':'teams'})
        spec={'title':'Hours','range':{'mode':'all'},'columns':[{'label':'Attraction','field':'标题'},{'label':'Open','field':'Open'}],'schedule':{'mode':'manual'}}
        with patch('kb.feed_reports.post_workflow',return_value=202) as post:
            delivery=reports.send(plan,spec=spec,request_key='settings-send-test')
        self.assertEqual(delivery.status,'accepted',delivery.error)
        config,body=post.call_args.args
        self.assertEqual(config['url'],TEAMS);self.assertEqual(config['kind'],'teams')
        self.assertEqual(body['type'],'message');self.assertNotIn('t',body)
        self.assertEqual(body['attachments'][0]['content']['type'],'AdaptiveCard')
        self.assertTrue(bytes(delivery.content).startswith(b'PK'))
