from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase,SimpleTestCase
from django.urls import reverse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from kb.connector_context import conversation_context
from kb.connector_runtime import duration_value
from kb.connector_schema import configuration_error,model_tools
from kb.feeds import _publish_row,_ensure_tracker
from kb.models import ConnectorDraft,KnowledgeBase,KnowledgeFeed,KbTracker,FeedRecord,TrackerRow,Document


class SchemaTests(SimpleTestCase):
    def test_browser_error_lists_all_missing_configuration_paths(self):
        error=configuration_error({'source_type':'browser'},'missing output')
        self.assertIn('browser.output',error['missing_fields']);self.assertIn('id_paths',error['missing_fields'])

    def test_model_receives_required_nested_browser_output(self):
        from langchain_core.tools import tool
        @tool
        def validate(spec:dict):
            """验证配置。"""
            return spec
        schema=model_tools([validate])[0]['function']['parameters']['properties']['spec']
        self.assertIn('id_paths',schema['required']);self.assertIn('output',schema['properties']['browser']['required'])

    def test_duration_and_overnight_duration(self):
        config={'format':'hhmm','unit':'hours'}
        self.assertEqual(duration_value('08:30','22:00',config),13.5)
        self.assertEqual(duration_value('22:00','06:00',config),8)


class ConversationTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('chat-regression',is_staff=True)
        self.client.force_login(self.user)

    def test_root_recovers_latest_reply_and_questions_survive_context(self):
        root=ConnectorDraft.objects.create(user=self.user,prompt='Collect attraction hours',status='revised',spec={'name':'prior'})
        question=ConnectorDraft.objects.create(user=self.user,parent=root,prompt=root.prompt,status='revised',questions=['单位用小时吗？'])
        reply=ConnectorDraft.objects.create(user=self.user,parent=question,prompt=root.prompt,feedback='是',status='ready')
        context=conversation_context(reply)
        self.assertEqual(context['previous_connector'],root.spec)
        self.assertEqual(context['conversation'][-1]['assistant_questions'],question.questions)
        result=self.client.get(reverse('kb:connector_thread',args=[root.pk])).json()
        self.assertEqual([item['id'] for item in result['thread']],[str(root.pk),str(question.pk),str(reply.pk)])

    def test_enabled_feed_can_be_edited_and_approval_updates_same_feed(self):
        from kb.tests.test_connector import spec
        kb=KnowledgeBase.objects.create(name='Existing',slug='existing-edit',is_folder=True,created_by=self.user)
        feed=KnowledgeFeed.objects.create(kb=kb,name='Existing',adapter_spec=spec(),created_by=self.user)
        parent=ConnectorDraft.objects.create(user=self.user,kb=kb,feed=feed,status='enabled',prompt='采集真实设备日报',spec=spec())
        with patch('kb.collector_api.start_async'):
            response=self.client.post(reverse('kb:connector_refine',args=[parent.pk]),json.dumps({'feedback':'增加一个字段'}),content_type='application/json')
        self.assertEqual(response.status_code,202,response.content)
        child=ConnectorDraft.objects.get(pk=response.json()['job']['id']);child.status='ready';child.spec=spec(name='Updated');child.preview={'valid':True}
        child.save(update_fields=['status','spec','preview'])
        feed.refresh_from_db();self.assertEqual(feed.name,'Existing')
        with patch('kb.collector_api.start_feed'):
            response=self.client.post(reverse('kb:connector_enable',args=[child.pk]),'{}',content_type='application/json')
        self.assertEqual(response.status_code,200,response.content);self.assertEqual(response.json()['feed_id'],str(feed.pk))
        self.assertEqual(KnowledgeFeed.objects.count(),1);self.assertEqual(KnowledgeBase.objects.count(),1)
        feed.refresh_from_db();self.assertEqual(feed.name,'Existing');self.assertEqual(feed.adapter_spec['name'],feed.kb.name);parent.refresh_from_db();self.assertEqual(parent.status,'enabled')

    def test_structured_updates_never_create_documents_and_preserve_other_columns(self):
        kb=KnowledgeBase.objects.create(name='Operating hours',slug='chat-regression',is_folder=True,created_by=self.user)
        feed=KnowledgeFeed.objects.create(kb=kb,name='Hours',write_mode='upsert',created_by=self.user)
        run=feed.runs.create(trigger='manual',lease_token='test')
        tracker=KbTracker.objects.create(kb=kb,fields=[{'label':'Other source'}])
        tracker=_ensure_tracker(feed,[{'label':'duration'}]);self.assertEqual(tracker.fields,[{'label':'Other source'},{'label':'duration'}])
        record=FeedRecord.objects.create(feed=feed,external_id='ride-1')
        item={'id':'ride-1','title':'Ride 1','content':'Hours','fields':{'duration':13.5}}
        self.assertTrue(_publish_row(feed,tracker,record,item,run))
        item['fields']['duration']=12
        self.assertFalse(_publish_row(feed,tracker,record,item,run))
        self.assertEqual(Document.objects.count(),0);self.assertEqual(TrackerRow.objects.count(),1)
        self.assertEqual(TrackerRow.objects.get().values['duration'],12)

    def test_forget_clears_only_finished_owned_dialogue_and_keeps_feed(self):
        kb=KnowledgeBase.objects.create(name='Keep',slug='keep-feed',is_folder=True,created_by=self.user)
        feed=KnowledgeFeed.objects.create(kb=kb,name='Keep',created_by=self.user)
        job=ConnectorDraft.objects.create(user=self.user,prompt='Temporary dialogue',status='enabled',feed=feed)
        response=self.client.post(reverse('kb:connector_forget'),json.dumps({'ids':[str(job.pk)]}),content_type='application/json')
        self.assertEqual(response.status_code,200);self.assertTrue(ConnectorDraft.objects.filter(pk=job.pk).exists())
        self.assertTrue(KnowledgeFeed.objects.filter(pk=feed.pk).exists())
        active=ConnectorDraft.objects.create(user=self.user,prompt='Running')
        response=self.client.post(reverse('kb:connector_forget'),json.dumps({'ids':[str(active.pk)]}),content_type='application/json')
        self.assertEqual(response.status_code,400);self.assertTrue(ConnectorDraft.objects.filter(pk=active.pk).exists())

    def test_chat_auth_shape_without_type_is_inferred_or_reported_not_falsely_saved(self):
        """缺 type 的认证形状：形状明确的照常保存，含义不明的必须报错，不能假报已保存后静默裸奔。"""
        from kb.connector_agent import run
        from kb.tests.test_connector import FakeModel, call
        from langchain_core.messages import AIMessageChunk
        token = 'user-provided-test-token-4417'
        job = ConnectorDraft.objects.create(user=self.user, prompt='从 https://api.example.test/reports 取数，token=' + token)
        model = FakeModel([
            call('set_source_access', {'url': 'https://api.example.test/reports', 'authentication': {'token': token}}),
            call('set_source_access', {'url': 'https://api.example.test/reports', 'authentication': {'credential': token}}, identity='call-2'),
            AIMessageChunk(content='{"questions":["请确认需要的字段"]}')])
        with tempfile.TemporaryDirectory() as directory, self.settings(DATA_DIR=Path(directory)), patch('kb.agent._get_llm', return_value=model):
            result = run(job.pk)
        self.assertIsNotNone(result.credential_id)      # {'token': …} 形状明确，已按 bearer 保存
        failed = [row for row in result.activity.values_list('payload', flat=True)
                  if row.get('kind') == 'tool_end' and not row.get('ok', True)]
        self.assertTrue(failed and any('type' in str(row.get('message', '')) for row in failed), failed)

    def test_chat_auth_explicit_none_reports_no_credential_attached(self):
        """显式 type=none 时工具结果必须说清「未绑定凭据」，否则代理会把 401 归因于密钥错误。"""
        from kb.connector_agent import run
        from kb.tests.test_connector import FakeModel, call
        from langchain_core.messages import AIMessageChunk
        job = ConnectorDraft.objects.create(user=self.user, prompt='从 https://api.example.test/reports 取数，该接口公开')
        model = FakeModel([call('set_source_access', {'url': 'https://api.example.test/reports', 'authentication': {'type': 'none'}}),
            AIMessageChunk(content='{"questions":["请确认需要的字段"]}')])
        with tempfile.TemporaryDirectory() as directory, self.settings(DATA_DIR=Path(directory)), patch('kb.agent._get_llm', return_value=model):
            result = run(job.pk)
        self.assertIsNone(result.credential_id)
        self.assertIn('credentials_attached', str(model.messages[-1]))   # 模型看到的是「未绑定凭据」，不是假的 saved

    def test_chat_auth_is_encrypted_and_omitted_from_activity(self):
        from kb.connector_agent import run
        from kb.tests.test_connector import FakeModel,call
        from langchain_core.messages import AIMessageChunk
        token='user-provided-test-token-9021'
        job=ConnectorDraft.objects.create(user=self.user,prompt='从 https://api.example.test/reports 取数，token='+token)
        model=FakeModel([call('set_source_access',{'url':'https://api.example.test/reports','authentication':{'type':'bearer','token':token}}),
            AIMessageChunk(content='{"questions":["请选择需要的报告日期"]}')])
        with tempfile.TemporaryDirectory() as directory,self.settings(DATA_DIR=Path(directory)),patch('kb.agent._get_llm',return_value=model):
            result=run(job.pk)
        self.assertEqual(result.status,'needs_info',result.error)
        self.assertIsNotNone(result.credential_id)
        self.assertNotIn(token,result.prompt)
        self.assertNotIn(token,str(list(result.activity.values_list('payload',flat=True))))
