from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from langchain_core.messages import HumanMessage,ToolMessage

from kb import connector_sessions
from kb.connector_agent import run
from kb.connector_context import WorkingMemory
from kb.connector_storage import load,save
from kb.models import ConnectorDraft,KnowledgeBase,KnowledgeFeed
from kb.tests.test_connector import FakeModel,call,spec,sample


class SavedContextTests(TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.override=self.settings(DATA_DIR=Path(self.directory.name));self.override.enable();self.addCleanup(self.override.disable)
        self.user=get_user_model().objects.create_user('saved-collector',is_staff=True);self.client.force_login(self.user)
        self.ids=[];self.addCleanup(lambda:connector_sessions.discard(self.ids,self.user.pk))

    def execute(self,job,responses):
        self.ids.append(job.pk);model=FakeModel(responses)
        with patch('kb.agent._get_llm',return_value=model),patch('kb.connector_agent.read_url',return_value=(200,{'content-type':'application/json'},json.dumps(sample()).encode())):
            result=run(job.pk)
        return result,model

    def test_confirm_leave_reload_and_edit_preserves_model_tool_context(self):
        parent=ConnectorDraft.objects.create(user=self.user,prompt='从 '+spec()['url']+' 采集设备日报',api_url=spec()['url'])
        parent,_=self.execute(parent,[call('probe_api',{'url':spec()['url']}),call('show_data_preview',{'spec':spec()},'preview')])
        self.assertEqual(parent.status,'ready',parent.error);self.assertTrue(parent.context_snapshot)
        self.assertNotIn('设备日报',parent.context_snapshot)
        with patch('kb.collector_api.start_feed'):
            enabled=self.client.post(reverse('kb:connector_enable',args=[parent.pk]),'{}',content_type='application/json')
        self.assertEqual(enabled.status_code,200,enabled.content);feed_id=enabled.json()['feed_id']
        forgotten=self.client.post(reverse('kb:connector_forget'),json.dumps({'ids':[str(parent.pk)]}),content_type='application/json')
        self.assertEqual(forgotten.status_code,200);parent.refresh_from_db();self.assertTrue(parent.retain_context)
        self.assertTrue(parent.context_snapshot);self.assertTrue(parent.activity.exists())
        resumed=self.client.post(reverse('kb:connector_resume'),json.dumps({'feed':feed_id}),content_type='application/json')
        self.assertEqual(resumed.status_code,200,resumed.content);self.assertEqual(resumed.json()['thread'][-1]['id'],str(parent.pk))
        self.assertNotIn('context_snapshot',resumed.content.decode())
        with patch('kb.collector_api.start_async'):
            response=self.client.post(reverse('kb:connector_refine',args=[parent.pk]),json.dumps({'feedback':'增加采集日期字段'}),content_type='application/json')
        child=ConnectorDraft.objects.get(pk=response.json()['job']['id']);self.assertTrue(child.retain_context)
        candidate=spec();candidate['body_fields'].append({'label':'采集日期','source':'today'})
        child,model=self.execute(child,[call('probe_api',{'url':spec()['url']}),call('show_data_preview',{'spec':candidate},'preview-2')])
        self.assertEqual(child.status,'ready',child.error);self.assertEqual(child.metrics['context_source'],'saved')
        prior=[m for m in model.messages[0] if isinstance(m,ToolMessage)]
        self.assertTrue(prior);self.assertIn('temperature',str(prior));self.assertIn('设备日报',str(model.messages[0]))
        self.assertIn('重新实际取数',str(model.messages[0]))
        KnowledgeFeed.objects.filter(pk=feed_id).update(lease_token='',lease_until=None)
        with patch('kb.collector_api.start_feed'):
            updated=self.client.post(reverse('kb:connector_enable',args=[child.pk]),'{}',content_type='application/json')
        self.assertEqual(updated.status_code,200,updated.content);self.assertEqual(updated.json()['feed_id'],feed_id)
        self.assertEqual(KnowledgeFeed.objects.count(),1)

    def test_existing_script_without_history_gets_edit_entry_and_stable_thread(self):
        kb=KnowledgeBase.objects.create(name='Existing task',slug='existing-saved-task',is_folder=True,created_by=self.user)
        feed=KnowledgeFeed.objects.create(kb=kb,name='Daily report',url=spec()['url'],adapter_spec=spec(),created_by=self.user)
        response=self.client.post(reverse('kb:connector_resume'),json.dumps({'feed':str(feed.pk)}),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content);first=response.json()['thread'][0]
        self.assertEqual(first['status'],'enabled');self.assertEqual(first['spec'],spec(name=kb.name,write_mode='append'))
        again=self.client.post(reverse('kb:connector_resume'),json.dumps({'feed':str(feed.pk)}),content_type='application/json')
        self.assertEqual(again.json()['thread'][0]['id'],first['id']);self.assertEqual(ConnectorDraft.objects.count(),1)
        state=self.client.get(reverse('kb:feed_state')).json()['feeds'][0]
        self.assertIn('?feed='+str(feed.pk),state['ai_edit_url'])
        page=self.client.get(state['ai_edit_url']);self.assertContains(page,'data-feed-id="'+str(feed.pk)+'"')
        self.assertContains(page,'对话随此任务保存')

    def test_snapshots_are_encrypted_owner_bound_and_restore_working_memory(self):
        job=ConnectorDraft.objects.create(user=self.user,prompt='keep actual context');memory=WorkingMemory()
        memory.observe('browser_snapshot',{}, {'url':'https://api.example.test/reports','revision':'v1','text':'observed evidence'})
        save(job,{'messages':[HumanMessage(content='private sample 9438')],'memory':memory,'sources':[]})
        job.refresh_from_db();self.assertNotIn('private sample',job.context_snapshot)
        restored=load(job.pk,self.user.pk);self.assertEqual(restored['messages'][0].content,'private sample 9438')
        self.assertIn('observed evidence',str(restored['memory'].summary()));self.assertIsNone(restored['sample'])
        self.assertIsNone(load(job.pk,self.user.pk+1))
        other=get_user_model().objects.create_user('other-saved-collector',is_staff=True);self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('kb:connector_thread',args=[job.pk])).status_code,404)

    def test_unapproved_temporary_dialogue_is_still_discarded(self):
        job=ConnectorDraft.objects.create(user=self.user,prompt='temporary',status='needs_info')
        response=self.client.post(reverse('kb:connector_forget'),json.dumps({'ids':[str(job.pk)]}),content_type='application/json')
        self.assertEqual(response.status_code,200);self.assertFalse(ConnectorDraft.objects.filter(pk=job.pk).exists())

    def reset_context(self,feed):
        return self.client.post(reverse('kb:connector_clear_context'),json.dumps({'feed':str(feed.pk)}),content_type='application/json')

    def test_clear_replaces_entire_thread_and_saved_live_context_but_preserves_script(self):
        from unittest.mock import Mock
        from kb.feed_credentials import store,bindings,request_headers
        from kb.models import KbTracker,TrackerRow
        kb=KnowledgeBase.objects.create(name='Operating hours',slug='clear-context-hours',is_folder=True,created_by=self.user)
        credential=store(self.user,spec()['url'],{'type':'bearer','token':'preserved-test-token'})
        feed=KnowledgeFeed.objects.create(kb=kb,name='Hours',url=spec()['url'],adapter_spec=spec(write_mode='append'),
            cron='0 8 * * *',write_mode='append',cursor='keep-cursor',enabled=False,created_by=self.user,
            credential=credential,header_env=bindings(credential))
        current=ConnectorDraft.objects.create(user=self.user,kb=kb,feed=feed,status='enabled',prompt='OLD REQUEST: remove duration',spec=spec())
        child=ConnectorDraft.objects.create(user=self.user,kb=kb,parent=current,status='needs_info',feedback='OLD CHANGE',spec=spec())
        unrelated=ConnectorDraft.objects.create(user=self.user,prompt='Other task',context_snapshot='other-context',status='ready')
        tracker=KbTracker.objects.create(kb=kb,enabled=True);row=TrackerRow.objects.create(tracker=tracker,feed=feed,external_id='ride',values={'duration':4})
        memory=WorkingMemory();state={'messages':[HumanMessage(content='OLD SECRET HISTORY')],'memory':memory,'sources':[]}
        save(current,state);save(child,state)
        browser=Mock();connector_sessions.put(current.pk,self.user.pk,{**state,'browser':browser})
        original={field:getattr(feed,field) for field in ('adapter_spec','enabled','cron','cursor','write_mode','updated_at','credential_id','header_env')}
        with self.captureOnCommitCallbacks(execute=True):response=self.reset_context(feed)
        self.assertEqual(response.status_code,200,response.content);data=response.json()['thread']
        self.assertEqual(len(data),1);fresh=ConnectorDraft.objects.get(pk=data[0]['id']);self.ids.append(fresh.pk)
        self.assertIsNone(fresh.parent_id);self.assertEqual(fresh.feed_id,feed.pk);self.assertEqual(fresh.status,'enabled')
        self.assertEqual(fresh.spec,feed.adapter_spec);self.assertFalse(fresh.context_snapshot);self.assertFalse(fresh.browser_state)
        self.assertEqual(fresh.credential_id,credential.pk);self.assertEqual(request_headers(fresh,feed.url)['Authorization'],'Bearer preserved-test-token')
        self.assertIsNone(fresh.sample);self.assertNotIn('OLD',fresh.prompt)
        for job in (current,child):
            job.refresh_from_db();self.assertFalse(job.context_snapshot);self.assertEqual(job.status,'context_cleared')
            self.assertIsNone(connector_sessions.take(job.pk,self.user.pk))
        feed.refresh_from_db();self.assertEqual({field:getattr(feed,field) for field in original},original)
        row.refresh_from_db();self.assertEqual(row.values,{'duration':4});self.assertEqual(feed.runs.count(),0)
        unrelated.refresh_from_db();self.assertEqual(unrelated.context_snapshot,'other-context')
        resumed=self.client.post(reverse('kb:connector_resume'),json.dumps({'feed':str(feed.pk)}),content_type='application/json')
        self.assertEqual([job['id'] for job in resumed.json()['thread']],[str(fresh.pk)])
        with patch('kb.collector_api.start_async') as start:
            stale=self.client.post(reverse('kb:connector_refine',args=[child.pk]),json.dumps({'feedback':'stale edit'}),content_type='application/json')
        self.assertEqual(stale.status_code,400);start.assert_not_called()

    def test_after_clear_model_receives_current_script_without_old_messages(self):
        kb=KnowledgeBase.objects.create(name='Current',slug='clear-context-model',is_folder=True)
        feed=KnowledgeFeed.objects.create(kb=kb,name='Current',url=spec()['url'],adapter_spec=spec(),created_by=self.user)
        parent=ConnectorDraft.objects.create(user=self.user,kb=kb,feed=feed,status='enabled',prompt='OLD HISTORY MARKER',spec=spec())
        save(parent,{'messages':[HumanMessage(content='OLD MODEL MARKER')],'memory':WorkingMemory(),'sources':[]})
        response=self.reset_context(feed);fresh_id=response.json()['thread'][0]['id']
        with patch('kb.collector_api.start_async'):
            result=self.client.post(reverse('kb:connector_refine',args=[fresh_id]),json.dumps({'feedback':'增加采集日期'}),content_type='application/json')
        child=ConnectorDraft.objects.get(pk=result.json()['job']['id'])
        child,model=self.execute(child,[call('probe_api',{'url':spec()['url']}),call('show_data_preview',{'spec':spec()},'preview-reset')])
        self.assertEqual(child.status,'ready',child.error);self.assertFalse(child.metrics.get('context_resumed',False))
        initial=str(model.messages[0]);self.assertNotIn('OLD HISTORY MARKER',initial);self.assertNotIn('OLD MODEL MARKER',initial)
        self.assertIn('temperature',initial);self.assertIn('增加采集日期',initial)
        self.assertFalse(any(isinstance(message,ToolMessage) for message in model.messages[0]))

    def test_clear_refuses_active_edit_and_does_not_touch_context(self):
        kb=KnowledgeBase.objects.create(name='Busy',slug='clear-context-busy',is_folder=True)
        feed=KnowledgeFeed.objects.create(kb=kb,name='Busy',url=spec()['url'],adapter_spec=spec(),created_by=self.user)
        parent=ConnectorDraft.objects.create(user=self.user,kb=kb,feed=feed,status='enabled',context_snapshot='keep')
        child=ConnectorDraft.objects.create(user=self.user,kb=kb,parent=parent,status='running')
        for status in ('running','enabling'):
            child.status=status;child.save(update_fields=['status'])
            response=self.reset_context(feed);self.assertEqual(response.status_code,409,response.content)
            parent.refresh_from_db();self.assertEqual(parent.context_snapshot,'keep');self.assertEqual(parent.status,'enabled')
        self.assertEqual(ConnectorDraft.objects.filter(feed=feed).count(),1)

    def test_clear_is_post_only_validated_and_department_scoped(self):
        from accounts.models import UserProfile
        kb=KnowledgeBase.objects.create(name='Private',slug='clear-context-private',is_folder=True,department='机械')
        feed=KnowledgeFeed.objects.create(kb=kb,name='Private',adapter_spec=spec(),created_by=self.user)
        url=reverse('kb:connector_clear_context')
        self.assertEqual(self.client.get(url).status_code,405)
        self.assertEqual(self.client.post(url,'{"feed":"invalid"}',content_type='application/json').status_code,400)
        manager=get_user_model().objects.create_user('clear-context-other-department')
        UserProfile.objects.update_or_create(user=manager,defaults={'department':'电气','role':UserProfile.Role.DEPT_ADMIN})
        self.client.force_login(manager);self.assertEqual(self.reset_context(feed).status_code,404)
        self.assertFalse(ConnectorDraft.objects.exists())

    def test_late_worker_cannot_restore_a_cleared_snapshot(self):
        job=ConnectorDraft.objects.create(user=self.user,status='context_cleared',prompt='Cleared')
        save(job,{'messages':[HumanMessage(content='late old history')],'memory':WorkingMemory(),'sources':[]})
        job.refresh_from_db();self.assertEqual(job.context_snapshot,'')
