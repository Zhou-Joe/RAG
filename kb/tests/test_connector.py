from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from langchain_core.messages import AIMessageChunk, ToolMessage

from kb.connector_agent import MAX_CALLS, MAX_SECONDS, recover_stale, run
from kb.connector_runtime import build_batch, lookup, request_url, validate_spec
from kb.connector_tools import check_spec, observed_json_shape, redact, sample_preview, search_docs
from kb.connector_views import package
from kb.feed_http import fetch_batch
from kb.feed_protocol import validate_batch
from kb.models import ConnectorDraft, KnowledgeBase, KnowledgeFeed


def spec(**extra):
    return {'name':'设备日报','on_error':'fail','url':'https://api.example.test/reports','records_path':'data','id_paths':['device','date'],
        'title_path':'name','body_fields':[{'label':'温度','path':'temperature','unit':'℃'},
            {'label':'报警','path':'alarms','format':'table','columns':[{'label':'编号','path':'code'},{'label':'描述','path':'description'}]}],
        'updated_at_path':'updated','updated_at_format':'unix_seconds','next_cursor_path':'next','cursor_param':'after',
        'cron':'0 8 * * *','timezone':'Asia/Shanghai','header_env':{},**extra}


def sample():
    return {'data':[{'device':'A1','date':'2026-10-31','name':'A1日报','temperature':25,'updated':1793404800,
        'alarms':[{'code':'E1','description':'压力|偏高'}]}],'next':'checkpoint-2'}


class RuntimeTests(SimpleTestCase):
    def test_observed_api_shape_reports_real_pointer_paths_without_large_values(self):
        data={'payload':{'entries':[{'id':'A1','nested':{'value':42},'alarms':[{'code':'E1'}]}]},
            'next':'cursor-2','access_token':'private-token','large':'x'*20000}
        shape=observed_json_shape(data)
        self.assertEqual(shape['root_type'],'object')
        entries=next(row for row in shape['record_arrays'] if row['records_path']=='/payload/entries')
        self.assertEqual(entries['count'],1)
        self.assertIn('/nested/value',[field['path'] for field in entries['item_fields']])
        self.assertIn('/next',[field['path'] for field in shape['observed_paths']])
        self.assertNotIn('private-token',json.dumps(shape))
        self.assertLess(len(json.dumps(shape)),12000)
        self.assertEqual(observed_json_shape([{'id':1}])['record_arrays'][0]['records_path'],'')
        self.assertEqual(observed_json_shape({'id':1})['record_arrays'],[])
        sparse=observed_json_shape({'rows':[{'id':1},{'id':2,'temperature':25}]})
        fields=sparse['record_arrays'][0]['item_fields']
        self.assertEqual(next(field['seen_in'] for field in fields if field['path']=='/temperature'),1)
        long_keys={('field'+str(index)+'x'*200):index for index in range(60)}
        self.assertLessEqual(len(json.dumps(observed_json_shape(long_keys),ensure_ascii=False)),16000)

    def test_document_mode_preserves_long_article_without_tracker_field_cap(self):
        article='Long article content. '*650
        data=sample();data['data'][0]['body']=article
        config=spec(ingest_mode='documents',body_fields=[{'label':'正文','path':'body'},
            {'label':'采集日期','source':'today'}])
        item=validate_batch(build_batch(config,data))['items'][0]
        self.assertIn(article,item['content'])
        self.assertNotIn('采集日期',item['content'])
        self.assertNotIn('fields',item)

    def test_raw_api_preview_rejects_never_observed_required_field(self):
        job=SimpleNamespace(api_url=spec()['url'],prompt=spec()['url'],header_env={})
        guessed=spec(on_error='raw',body_fields=[{'label':'猜测温度','path':'invented'}])
        with self.assertRaisesRegex(ValueError,'所有记录都不存在已配置字段路径'):
            sample_preview(job,guessed,sample())
        guessed['body_fields'][0]['required']=False
        self.assertTrue(sample_preview(job,guessed,sample())['valid'])
        partial=sample();partial['data'].append({**partial['data'][0],'device':'A2','temperature':None})
        self.assertTrue(sample_preview(job,spec(on_error='raw'),partial)['valid'])
        all_null=sample();all_null['data'][0]['temperature']=None
        self.assertTrue(sample_preview(job,spec(on_error='raw'),all_null)['valid'])
        bad_table=spec(on_error='raw',body_fields=[{'label':'报警','path':'alarms','format':'table',
            'columns':[{'label':'猜测编号','path':'missing_code'}]}])
        with self.assertRaisesRegex(ValueError,'报警.猜测编号'):
            sample_preview(job,bad_table,sample())
        with self.assertRaisesRegex(ValueError,'title_path'):
            sample_preview(job,spec(on_error='raw',title_path='invented'),sample())

    def test_nested_mapping_composite_ids_units_tables_timestamps_and_cursor(self):
        payload=validate_batch(build_batch(spec(),sample()));item=payload['items'][0]
        self.assertEqual(json.loads(item['id']),['A1','2026-10-31']);self.assertIn('25 ℃',item['content'])
        self.assertIn('压力\\|偏高',item['content']);self.assertEqual(payload['cursor'],'checkpoint-2')
        self.assertTrue(item['updated_at'].endswith('+00:00'))
        self.assertEqual(payload['batch_id'],build_batch(spec(),sample())['batch_id'])

    def test_pointer_paths_and_single_objects(self):
        self.assertEqual(lookup({'a/b':{'~c':[42]}},'/a~1b/~0c/0'),42)
        config=spec(records_path='',single_record=True,id_paths=['device'],body_fields=[{'label':'报告','path':''}])
        config.pop('next_cursor_path');record=sample()['data'][0]
        self.assertEqual(build_batch(config,record)['items'][0]['id'],'A1')

    def test_missing_ids_required_fields_and_oversized_arrays_fail(self):
        from kb.connector_runtime import MAX_ITEMS
        for change in [lambda d:d['data'][0].pop('device'),lambda d:d['data'][0].pop('temperature'),lambda d:d.update(data=d['data']*(MAX_ITEMS+1))]:
            data=sample();change(data)
            with self.assertRaises(ValueError):build_batch(spec(),data)

    def test_optional_fields_and_explicit_deletes(self):
        config=spec(body_fields=[{'label':'温度','path':'temperature'},{'label':'压力','path':'pressure','required':False}],deleted_path='removed')
        data=sample();data['removed']=['old-id']
        payload=build_batch(config,data);self.assertEqual(payload['deleted'],['old-id']);self.assertNotIn('压力',payload['items'][0]['content'])
        self.assertEqual(build_batch(spec(),sample())['deleted'],[])

    def test_computed_duration_hhmm_iso_and_structured_fields_output(self):
        from kb.connector_runtime import field_value
        duration={'label':'持续时长','unit':'分钟','computed':{'op':'duration','start':'open','end':'close','format':'hhmm','unit':'minutes'}}
        config=spec(body_fields=[{'label':'温度','path':'temperature'},duration])
        data=sample();data['data'][0].update(open='09:00',close='21:00')
        payload=build_batch(config,data);item=payload['items'][0]
        self.assertIn('720',item['content']);self.assertEqual(item['fields'],{'温度':25,'持续时长':720})
        self.assertEqual(field_value(data['data'][0],duration),720)
        night=sample();night['data'][0].update(open='22:00',close='02:00')
        self.assertEqual(build_batch(config,night)['items'][0]['fields']['持续时长'],240)
        iso={'label':'时长','computed':{'op':'duration','start':'start','end':'end','format':'iso8601','unit':'hours'}}
        stamp=sample();stamp['data'][0].update(start='2026-10-31T08:00:00+08:00',end='2026-10-31T16:30:00+08:00')
        self.assertEqual(build_batch(spec(body_fields=[iso]),stamp)['items'][0]['fields']['时长'],8.5)
        job=SimpleNamespace(api_url='https://api.example.test/reports',prompt='设备日报',header_env={})
        coverage={row['label']:row for row in sample_preview(job,config,data)['field_coverage']}
        self.assertEqual(coverage['持续时长']['present'],1)

    def test_computed_duration_validation_and_missing_values(self):
        base={'op':'duration','start':'open','end':'close','format':'hhmm','unit':'minutes'}
        for field in [{'label':'d'},{'label':'d','path':'x','computed':base},{'label':'d','computed':{'op':'add','start':'a','end':'b'}},
                      {'label':'d','computed':{**base,'format':'hh:mm'}},{'label':'d','computed':{**base,'end':''}},
                      {'label':'d','computed':base,'format':'table','columns':[{'label':'x','path':'y'}]}]:
            with self.assertRaises(ValueError):validate_spec(spec(body_fields=[{'label':'温度','path':'temperature'},field]))
        optional={'label':'时长','required':False,'computed':base}
        payload=build_batch(spec(body_fields=[{'label':'温度','path':'temperature'},optional]),sample())
        self.assertNotIn('时长',payload['items'][0]['content']);self.assertNotIn('时长',payload['items'][0].get('fields',{}))
        with self.assertRaises(ValueError):build_batch(spec(body_fields=[{'label':'时长','computed':base}]),sample())

    def test_query_merge_only_committed_cursor_overrides_config(self):
        from urllib.parse import parse_qs,urlsplit
        query=parse_qs(urlsplit(request_url(spec(query={'limit':20,'after':'initial'}),'committed')).query)
        self.assertEqual(query,{'limit':['20'],'after':['committed']})

    def test_invalid_spec_and_nonfinite_query_are_rejected(self):
        for config in [spec(code='import os'),spec(id_paths=[]),spec(single_record='yes'),spec(query={'x':float('nan')}),
            spec(body_fields=[{'label':'x','path':'x','unit':{}}])]:
            with self.assertRaises((ValueError,TypeError)):validate_spec(config)

    def test_generated_zip_is_stdlib_and_compiles_without_executing_user_code(self):
        config=spec(name="报告'\n__import__('os')")
        archive=zipfile.ZipFile(io.BytesIO(package(config)))
        self.assertEqual(set(archive.namelist()),{'connector.py','connector.json','connector_runtime.py','connector_calculations.py','credential_routing.py','feed_client.py','requirements.txt','.env.example','README.md'})
        compile(archive.read('connector.py'),'connector.py','exec')
        self.assertIn(b'No pip',archive.read('requirements.txt'))
        # 独立启动只检查命令行，不导入Django，不接触网络。
        with tempfile.TemporaryDirectory() as directory:
            archive.extractall(directory)
            result=subprocess.run([sys.executable,'-I',str(Path(directory)/'connector.py'),'--help'],capture_output=True,timeout=20)
            # -I 会移除脚本目录，正常的脚本执行按Python默认搜索路径运行。
            if result.returncode:
                result=subprocess.run([sys.executable,str(Path(directory)/'connector.py'),'--help'],capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_mapping_cannot_change_approved_origin_or_env_and_needs_real_sample(self):
        job=SimpleNamespace(api_url='https://api.example.test/reports',prompt='设备日报',header_env={})
        for config in [spec(url='https://evil.test/'),spec(header_env={'Authorization':'FOS_FEED_OTHER'}),spec(query={'api_key':'literal'})]:
            with self.assertRaises(ValueError):check_spec(job,config)
        with self.assertRaises(ValueError):sample_preview(job,spec(),None)
        self.assertTrue(sample_preview(job,spec(),sample())['valid'])

    def test_redacts_known_secrets_and_auth_fields(self):
        clean=redact({'password':'private','nested':{'access_token':'token'},'text':'Bearer ABC123 hidden-value'},('hidden-value',))
        self.assertNotIn('ABC123',json.dumps(clean));self.assertNotIn('hidden-value',json.dumps(clean));self.assertEqual(clean['password'],'[REDACTED]')

    def test_search_providers_use_normalized_bounded_results_and_private_searx_only_by_config(self):
        with patch.dict(os.environ,{'FOS_FEED_SEARCH_URL':'https://search.example.test/search','FOS_FEED_SEARCH_API_KEY':''}),patch('kb.connector_tools.read_url',return_value=(200,{},json.dumps({'results':[{'title':'API','url':'https://api.example.test/docs','content':'fields'}]}).encode())) as read:
            self.assertEqual(search_docs('API docs')[0]['title'],'API');self.assertTrue(read.call_args.kwargs['allow_private'])
        with patch.dict(os.environ,{'FOS_FEED_SEARCH_URL':'','FOS_FEED_SEARCH_API_KEY':'test-search-key'}),patch('kb.connector_tools.read_url',return_value=(200,{},b'{"results":[]}')) as read:
            self.assertEqual(search_docs('API docs'),[]);self.assertEqual(read.call_args.kwargs['method'],'POST')
            self.assertEqual(read.call_args.kwargs['headers']['Authorization'],'Bearer test-search-key')

    def test_raw_api_adapter_validates_batch_and_uses_committed_cursor(self):
        feed=SimpleNamespace(adapter_spec=spec(),header_env={},cursor='committed',allow_private=False)
        with patch('kb.feed_http.read_url',return_value=(200,{'content-type':'application/json'},json.dumps(sample()).encode())) as read:
            payload,etag=fetch_batch(feed)
            self.assertIn('after=committed',read.call_args.args[0]);self.assertEqual(payload['items'][0]['title'],'A1日报');self.assertEqual(etag,'')


class FakeModel:
    def __init__(self,responses):self.responses=iter(responses);self.messages=[]
    def bind_tools(self,*args,**kwargs):self.tools={(t['function']['name'] if isinstance(t,dict) else t.name):t for t in args[0]};return self
    def bind(self,**kwargs):return self
    def stream(self,messages):self.messages.append(list(messages));yield next(self.responses)


def answer(config=None):return AIMessageChunk(content=json.dumps({'connector':config or spec()},ensure_ascii=False))
def call(name,args=None,identity='call-1'):return AIMessageChunk(content='',tool_calls=[{'name':name,'args':args or {},'id':identity}])


class AgentTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('connector-admin',is_staff=True)
        self.job=ConnectorDraft.objects.create(user=self.user,prompt='从 https://api.example.test/reports 采集设备日报',sample=sample())

    def run_model(self,responses):
        model=FakeModel(responses)
        with patch('kb.agent._get_llm',return_value=model):result=run(self.job.pk)
        return result,model

    def test_tool_mapping_then_ready_never_creates_feed_until_approved(self):
        result,model=self.run_model([call('inspect_connector_environment'),call('test_mapping',{'spec':spec()},'call-2'),answer()])
        self.assertEqual(result.status,'ready',result.error);self.assertEqual(result.metrics['calls'],3);self.assertEqual(result.metrics['tools'],2)
        self.assertEqual(KnowledgeFeed.objects.count(),0);self.assertTrue(any(event['kind']=='tool_end' for event in result.events))

    def test_probe_samples_user_api_and_keeps_credentials_out_of_model_and_events(self):
        self.job.sample=None;self.job.api_url=spec()['url'];self.job.header_env={'Authorization':'FOS_FEED_TEST_AUTH'};self.job.save(update_fields=['sample','api_url','header_env'])
        data=sample();data['access_token']='hidden-secret'
        config=spec(header_env=self.job.header_env)
        with patch.dict(os.environ,{'FOS_FEED_TEST_AUTH':'Bearer hidden-secret'}),patch('kb.connector_agent.read_url',return_value=(200,{'content-type':'application/json'},json.dumps(data).encode())) as read:
            result,model=self.run_model([call('probe_api',{'url':config['url']}),answer(config)])
        self.assertEqual(result.status,'ready',str(result.events));self.assertEqual(read.call_args.kwargs['headers']['Authorization'],'Bearer hidden-secret')
        self.assertNotIn('hidden-secret',str(model.messages));self.assertNotIn('hidden-secret',json.dumps(result.events))

    def test_probe_exposes_observed_shape_and_rejects_other_query_sample(self):
        self.job.sample=None;self.job.api_url=spec()['url'];self.job.save(update_fields=['sample','api_url'])
        model=FakeModel([call('probe_api',{'url':spec()['url']}),
            call('inspect_api_sample',{'path':'/data/0'},'inspect'),
            call('test_mapping',{'spec':spec(query={'page':2})},'mapping'),
            AIMessageChunk(content='需要先探测第 2 页的真实响应。')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.connector_agent.read_url',return_value=(200,{'content-type':'application/json'},json.dumps(sample()).encode())) as read:
            result=run(self.job.pk)
        self.assertEqual(result.status,'answered',result.error)
        self.assertEqual(read.call_count,1)
        tools=[json.loads(message.content) for message in model.messages[-1] if isinstance(message,ToolMessage)]
        self.assertEqual(tools[0]['record_arrays'][0]['records_path'],'/data')
        self.assertIn('/device',[row['path'] for row in tools[0]['record_arrays'][0]['item_fields']])
        self.assertEqual(tools[1]['selected_path'],'/data/0')
        self.assertIn('/device',[row['path'] for row in tools[1]['observed_paths']])
        self.assertIn('URL 或 query 与已读取样例不一致',tools[2]['error'])

    def test_source_change_drops_inherited_api_sample(self):
        from kb.collector_api import refine_draft
        self.job.api_url=spec()['url'];self.job.status='answered';self.job.save(update_fields=['api_url','status'])
        with patch('kb.collector_api.start_async'):
            child=refine_draft(user=self.user,job=self.job,requirement='改用新的实际接口',
                api_url='https://api.example.test/other')
        self.assertIsNone(child.sample)

    def test_probe_returns_actionable_http_error_without_accepting_sample(self):
        self.job.sample=None;self.job.api_url=spec()['url'];self.job.save(update_fields=['sample','api_url'])
        model=FakeModel([call('probe_api',{'url':spec()['url']}),AIMessageChunk(content='接口返回错误，请核对参数。')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.connector_agent.read_url',return_value=(429,{'content-type':'application/json','retry-after':'60'},b'{"error":"rate limited"}')):
            result=run(self.job.pk)
        self.assertEqual(result.status,'answered',result.error)
        detail=json.loads(next(message.content for message in model.messages[-1] if isinstance(message,ToolMessage)))
        self.assertEqual(detail['status'],429);self.assertEqual(detail['retry_after'],'60')
        self.assertNotIn('sample',detail)

    def test_rate_limit_from_prior_turn_does_not_block_later_probe(self):
        self.job.sample=None;self.job.api_url=spec()['url'];self.job.save(update_fields=['sample','api_url'])
        with patch('kb.connector_agent.read_url',return_value=(429,{'content-type':'application/json'},b'{"error":"slow down"}')):
            parent,_=self.run_model([call('probe_api',{'url':spec()['url']}),AIMessageChunk(content='请稍后重试。')])
        self.assertEqual(parent.status,'answered',parent.error)
        child=ConnectorDraft.objects.create(user=self.user,parent=parent,prompt=parent.prompt,
            feedback='现在重试',api_url=spec()['url'])
        self.job=child
        with patch('kb.connector_agent.read_url',return_value=(200,{'content-type':'application/json'},json.dumps(sample()).encode())) as read:
            result,_=self.run_model([call('probe_api',{'url':spec()['url']}),AIMessageChunk(content='已取得真实样例。')])
        self.assertEqual(result.status,'answered',result.error)
        read.assert_called_once()

    def test_probe_cannot_send_auth_to_invented_origin(self):
        with patch('kb.connector_agent.read_url') as read:
            result,model=self.run_model([call('probe_api',{'url':'https://evil.test/'}),answer()])
        read.assert_not_called();self.assertEqual(result.status,'ready')
        tool=[message for message in model.messages[-1] if isinstance(message,ToolMessage)][0]
        self.assertIn('error',tool.content)

    def test_incomplete_requirements_ask_questions_and_do_not_create_source(self):
        result,_=self.run_model([AIMessageChunk(content='{"questions":["请提供稳定ID字段和实际GET地址"]}')])
        self.assertEqual(result.status,'needs_info');self.assertTrue(result.questions);self.assertEqual(KnowledgeFeed.objects.count(),0)

    def test_invalid_mapping_is_repaired_within_budget(self):
        result,_=self.run_model([answer(spec(id_paths=['invented'])),answer()])
        self.assertEqual(result.status,'ready',result.error);self.assertEqual(result.metrics['calls'],2)

    def test_bad_json_and_tool_loop_have_finite_end(self):
        self.assertEqual(MAX_CALLS,50)
        result,_=self.run_model([AIMessageChunk(content='{"connector":')]*MAX_CALLS)
        self.assertEqual(result.status,'failed');self.assertLessEqual(result.metrics['calls'],8)
        self.job.status='running';self.job.save(update_fields=['status'])
        result,_=self.run_model([call('inspect_connector_environment',identity='c'+str(i)) for i in range(MAX_CALLS)])
        self.assertEqual(result.status,'failed');self.assertLessEqual(result.metrics['calls'],8)

    def test_revoked_permission_and_stale_worker_cannot_publish(self):
        self.user.is_active=False;self.user.save(update_fields=['is_active'])
        result,_=self.run_model([answer()]);self.assertEqual(result.status,'failed');self.assertEqual(result.metrics['calls'],0)
        ConnectorDraft.objects.filter(pk=self.job.pk).update(status='running',created_at=timezone.now()-timedelta(seconds=MAX_SECONDS+60))
        recover_stale();self.job.refresh_from_db();self.assertEqual(self.job.status,'failed')

    def test_no_sample_is_not_accepted_as_ready(self):
        self.job.sample=None;self.job.save(update_fields=['sample'])
        result,_=self.run_model([answer()]*MAX_CALLS)
        self.assertEqual(result.status,'failed');self.assertEqual(result.preview,{})


class ConnectorViewsTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('builder-admin',is_staff=True)
        self.other=get_user_model().objects.create_user('builder-other',is_staff=True)
        self.reader=get_user_model().objects.create_user('builder-reader')
        self.client.force_login(self.user)
        start=patch('kb.collector_api.start_feed');self.start_feed=start.start();self.addCleanup(start.stop)

    def create(self,**data):
        return self.client.post(reverse('kb:connector_create'),json.dumps({'prompt':'从 https://api.example.test/reports 采集温度日报',**data}),content_type='application/json')

    def ready(self):
        job=ConnectorDraft.objects.create(user=self.user,prompt='API https://api.example.test/reports',status='ready',spec=spec(),preview={'valid':True})
        return job

    def test_create_and_reconnect_start_once_with_no_implicit_feed(self):
        with patch('kb.collector_api.start_async') as start:
            response=self.create(sample=sample());self.assertEqual(response.status_code,202,response.content)
            job=response.json()['job'];self.assertEqual(self.client.get(job['status_url']).status_code,200)
            self.assertEqual(self.create().status_code,400);self.assertEqual(start.call_count,1)
        self.assertEqual(KnowledgeFeed.objects.count(),0)

    def test_private_open_by_default_and_follows_the_conversation(self):
        """内网访问默认开放：页面不再有开关，显式关闭仍有效并随后续反馈延续。"""
        with patch('kb.collector_api.start_async'):
            created = self.create()
            self.assertEqual(created.status_code, 202, created.content)
            job = ConnectorDraft.objects.get(pk=created.json()['job']['id'])
            self.assertTrue(job.allow_private)
        ConnectorDraft.objects.filter(pk=job.pk).update(status='needs_info')
        with patch('kb.collector_api.start_async'):
            reply = self.client.post(reverse('kb:connector_refine', args=[job.pk]),
                json.dumps({'feedback': '请重新读取内网接口文档'}), content_type='application/json')
            self.assertEqual(reply.status_code, 202, reply.content)
            child = ConnectorDraft.objects.get(pk=reply.json()['job']['id'])
            self.assertTrue(child.allow_private)
        ConnectorDraft.objects.filter(pk=child.pk).update(status='needs_info')
        with patch('kb.collector_api.start_async'):
            off = self.client.post(reverse('kb:connector_refine', args=[child.pk]),
                json.dumps({'feedback': '改用公开接口', 'allow_private': False}), content_type='application/json')
            self.assertEqual(off.status_code, 202, off.content)
            self.assertFalse(off.json()['job']['allow_private'])
        from accounts.models import UserProfile
        dept = get_user_model().objects.create_user('dept-builder')
        UserProfile.objects.update_or_create(user=dept, defaults={'department': '机械', 'role': UserProfile.Role.DEPT_ADMIN})
        self.client.force_login(dept)
        with patch('kb.collector_api.start_async'):
            own = self.create(allow_private=True)
            self.assertEqual(own.status_code, 202, own.content)
            self.assertTrue(own.json()['job']['allow_private'])   # 部门管理员同样自动开放，无需额外授权
        page = self.client.get(reverse('kb:connector_builder'))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'connectorPrivate')
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse('kb:connector_builder')), 'connectorPrivate')

    def test_thread_returns_conversation_chain_and_builder_page_renders_chat(self):
        root=ConnectorDraft.objects.create(user=self.user,prompt='原始采集需求',status='failed')
        child=ConnectorDraft.objects.create(user=self.user,parent=root,prompt=root.prompt,feedback='改用浏览器采集',status='ready',spec=spec(),preview={'valid':True,'items':1})
        response=self.client.get(reverse('kb:connector_thread',args=[child.pk]))
        self.assertEqual(response.status_code,200)
        chain=response.json()['thread'];self.assertEqual(len(chain),2)
        self.assertEqual(chain[0]['id'],str(root.pk));self.assertEqual(chain[1]['id'],str(child.pk))
        self.assertEqual(chain[1]['feedback'],'改用浏览器采集')
        page=self.client.get(reverse('kb:connector_builder'))
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'connectorMessages');self.assertContains(page,'cx-composer-box')
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('kb:connector_thread',args=[child.pk])).status_code,404)

    def test_enable_creates_scheduled_adapter_once_and_download_contains_no_secret(self):
        job=self.ready();url=reverse('kb:connector_enable',args=[job.pk])
        first=self.client.post(url,'{}',content_type='application/json');second=self.client.post(url,'{}',content_type='application/json')
        self.assertEqual(first.status_code,200,first.content);self.assertEqual(first.json()['feed_id'],second.json()['feed_id']);self.assertEqual(KnowledgeFeed.objects.count(),1)
        feed=KnowledgeFeed.objects.get();self.assertEqual(feed.adapter_spec['id_paths'],['device','date']);self.assertIsNotNone(feed.next_run_at)
        response=self.client.get(reverse('kb:connector_download',args=[job.pk]));self.assertEqual(response.status_code,200)
        archive=zipfile.ZipFile(io.BytesIO(b''.join(response.streaming_content)));self.assertIn('connector_runtime.py',archive.namelist())

    def test_other_users_and_readers_cannot_read_or_activate_drafts(self):
        job=self.ready();self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('kb:connector_status',args=[job.pk])).status_code,404)
        self.assertEqual(self.client.post(reverse('kb:connector_enable',args=[job.pk]),'{}',content_type='application/json').status_code,404)
        self.client.force_login(self.reader);self.assertEqual(self.client.get(reverse('kb:connector_builder')).status_code,200)
        self.assertEqual(self.client.get(reverse('kb:connector_status',args=[job.pk])).status_code,404)

    def test_invalid_input_and_unapproved_job_do_not_create_kb(self):
        self.assertEqual(self.create(header_env={'Authorization':'NOT_APPROVED'}).status_code,400)
        self.assertEqual(self.create(allow_private='true').status_code,400)
        job=self.ready();job.status='running';job.save(update_fields=['status']);before=KnowledgeBase.objects.count()
        self.assertEqual(self.client.post(reverse('kb:connector_enable',args=[job.pk]),'{}',content_type='application/json').status_code,400)
        self.assertEqual(KnowledgeBase.objects.count(),before)

    def test_generated_source_edits_keep_mapping_and_update_actual_request(self):
        job=self.ready();self.client.post(reverse('kb:connector_enable',args=[job.pk]),'{}',content_type='application/json')
        feed=KnowledgeFeed.objects.get();KnowledgeFeed.objects.filter(pk=feed.pk).update(lease_token='',lease_until=None)
        response=self.client.post(reverse('kb:feed_action'),json.dumps({'action':'update','id':str(feed.pk),
            'name':'New name','mode':'pull','url':'https://api.example.test/v2','cron':'0 9 * * *','timezone':'Asia/Shanghai'}),
            content_type='application/json',HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code,200,response.content);feed.refresh_from_db()
        self.assertEqual(feed.adapter_spec['url'],'https://api.example.test/v2');self.assertEqual(feed.adapter_spec['id_paths'],['device','date'])
