from __future__ import annotations

import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock,AsyncMock
import asyncio

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase,TestCase
from django.urls import reverse

from kb.browser_runtime import BrowserSession,fetch_resource,validate_plan,MAX_RECORDS,MAX_PAGES
from kb.connector_agent import run
from kb.connector_runtime import build_batch,validate_spec
from kb.connector_tools import sample_preview
from kb.connector_views import package
from kb.connector_tools import check_spec
from kb.feed_credentials import store
from kb.feed_http import fetch_batch
from kb.models import ConnectorDraft,KnowledgeFeed
from kb.tests.test_connector import FakeModel,answer,call


def web_spec(url='https://website.example.test/list'):
    return {'name':'Attraction hours','on_error':'fail','url':url,'source_type':'browser','records_path':'records','id_paths':['id'],
        'title_path':'name','body_fields':[{'label':'Start','path':'start'},{'label':'End','path':'end'}],
        'source_url_path':'source_url','header_env':{},'browser':{'actions':[{'action':'wait','selector':'.card'}],
        'output':{'kind':'dom','rows':'.card','fields':{'id':{'attribute':'data-id'},'name':{'selector':'.title'},
            'detail_url':{'selector':'a','attribute':'href'}},'detail':{'url_field':'detail_url',
            'fields':{'start':{'selector':'.start'},'end':{'selector':'.end'},'date':{'source':'today'}}},
            'next_selector':'#next','max_pages':2}}}


class BrowserPlanTests(SimpleTestCase):
    def test_closed_records_keep_status_and_blank_duration_but_open_records_need_times(self):
        rule={'path':'state','regex':'^Closed'}
        spec=web_spec();spec['body_fields']=[{'label':'State','path':'state'},
            {'label':'Start','path':'start','required_unless':rule},{'label':'End','path':'end','required_unless':rule},
            {'label':'Duration','computed':{'op':'duration','start':'start','end':'end','format':'hhmm','unit':'hours'},'required_unless':rule}]
        data={'records':[{'id':'closed','name':'Closed ride','state':'Closed for Scheduled Maintenance','start':None,'end':None,'source_url':spec['url']}]}
        payload=build_batch(spec,data);self.assertEqual(payload['items'][0]['fields']['Duration'],'')
        job=SimpleNamespace(api_url=spec['url'],prompt=spec['url'],header_env={})
        preview=sample_preview(job,spec,data);self.assertNotIn('warning',preview)
        duration=next(field for field in preview['field_coverage'] if field['label']=='Duration')
        self.assertEqual(duration['not_applicable'],1);self.assertEqual(duration['total'],0)
        data['records'][0]['state']='Open'
        with self.assertRaisesMessage(ValueError,'start'):build_batch(spec,data)

    def test_browser_limits_accept_50000_records_and_100_pages(self):
        self.assertEqual((MAX_RECORDS,MAX_PAGES),(50000,100))
        candidate=web_spec()['browser'];candidate['max_records']=MAX_RECORDS
        candidate['output']['max_pages']=MAX_PAGES
        self.assertEqual(validate_plan(candidate)['max_records'],MAX_RECORDS)
        candidate['max_records']=MAX_RECORDS+1
        with self.assertRaises(ValueError):validate_plan(candidate)
        candidate['max_records']=500
        self.assertEqual(validate_plan(candidate)['max_records'],500)

    def test_model_tool_schema_matches_runtime_large_batch_limits(self):
        from langchain_core.tools import tool
        from kb.connector_schema import model_tools
        @tool
        def configure(spec:dict)->dict:
            """Configure a collector."""
            return spec
        schema=model_tools([configure])[0]['function']['parameters']['properties']['spec']
        browser=schema['properties']['browser']['properties']
        self.assertEqual(browser['max_records']['maximum'],MAX_RECORDS)
        self.assertEqual(browser['output']['properties']['max_pages']['maximum'],MAX_PAGES)

    def test_dom_count_over_limit_is_rejected_before_reading_rows(self):
        browser=BrowserSession.__new__(BrowserSession);browser.zone='UTC';browser._goto=AsyncMock()
        browser.page=Mock();rows=browser.page.locator.return_value
        rows.first.wait_for=AsyncMock();rows.count=AsyncMock(return_value=MAX_RECORDS+1)
        browser._fields=AsyncMock()
        candidate=web_spec();candidate['browser']['actions']=[]
        with self.assertRaisesMessage(ValueError,'50000条'):
            asyncio.run(browser._collect(candidate))
        browser._fields.assert_not_called()

    def test_browser_spec_supports_css_fields_details_and_portable_package(self):
        import io,zipfile
        spec=web_spec();self.assertEqual(validate_spec(spec)['source_type'],'browser')
        with zipfile.ZipFile(io.BytesIO(package(spec))) as archive:
            self.assertIn('browser_runtime.py',archive.namelist());self.assertIn(b'playwright',archive.read('requirements.txt'))
            compile(archive.read('browser_runtime.py'),'browser_runtime.py','exec')

    def test_browser_plan_rejects_eval_unbounded_pages_and_secret_code(self):
        for plan in [{'actions':[{'action':'evaluate','value':'fetch("file:///etc/passwd")'}],'output':{'rows':'body','fields':{'text':{}}}},
            {'actions':[],'output':{'rows':'body','fields':{'text':{'regex':'(a+)+$'}}}},
            {'output':{'rows':'body','fields':{'text':{}},'max_pages':101}},
            {'output':{'kind':'network','url_contains':''}}, {'output':{'rows':'body','fields':{'text':{}}},'max_records':50001}]:
            with self.assertRaises(ValueError):validate_plan(plan)

    def test_private_requests_require_opt_in_and_matching_origin(self):
        for allowed,origins in [(False,[('http','127.0.0.1',9)]),(True,[('https','example.com',443)])]:
            with self.assertRaises(ValueError):fetch_resource('http://127.0.0.1:9/','GET',{},None,allow_private=allowed,private_origins=origins)


class BrowserTimeoutTests(SimpleTestCase):
    def test_collection_passes_twenty_minute_limit_to_async_cancellation(self):
        browser=BrowserSession.__new__(BrowserSession)
        browser._open=AsyncMock();browser._collect=AsyncMock(return_value={'records':[]})
        async def wait(coroutine,*,timeout):
            self.assertEqual(timeout,20*60)
            return await coroutine
        with patch('kb.browser_runtime.asyncio.wait_for',side_effect=wait):
            result=asyncio.run(browser._dispatch('collect',{'spec':{}}))
        self.assertEqual(result,{'records':[]});self.assertFalse(browser.document_only)

    def test_outer_watchdog_allows_collection_past_old_seven_minute_limit(self):
        import concurrent.futures
        browser=BrowserSession.__new__(BrowserSession);browser.loop=object();browser.checkpoint=Mock();browser._dispatch=Mock(return_value=object())
        future=Mock();future.result.side_effect=[concurrent.futures.TimeoutError(),{'records':[]}]
        with patch('kb.browser_runtime.asyncio.run_coroutine_threadsafe',return_value=future),patch('kb.browser_runtime.time.monotonic',side_effect=[0,15*60]):
            self.assertEqual(browser.call('collect',spec={}),{'records':[]})
        future.cancel.assert_not_called();browser.checkpoint.assert_called_once()

    def test_outer_watchdog_still_cancels_after_twenty_minutes_and_margin(self):
        import concurrent.futures
        browser=BrowserSession.__new__(BrowserSession);browser.loop=object();browser.checkpoint=None;browser._dispatch=Mock(return_value=object())
        future=Mock();future.result.side_effect=concurrent.futures.TimeoutError()
        with patch('kb.browser_runtime.asyncio.run_coroutine_threadsafe',return_value=future),patch('kb.browser_runtime.time.monotonic',side_effect=[0,20*60+11]):
            with self.assertRaisesMessage(ValueError,'浏览器操作超时'):browser.call('collect',spec={})
        future.cancel.assert_called_once()


class BrowserRuntimeTests(SimpleTestCase):
    """真实Edge浏览器加载本机动态站点，验证生产使用的相同重放运行时。"""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        class Handler(BaseHTTPRequestHandler):
            def send(self,body,status=200,kind='text/html',**headers):
                raw=body.encode();self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8');self.send_header('Content-Length',str(len(raw)))
                for key,value in headers.items():self.send_header(key,value)
                self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                if self.path in ('/gzip-resource','/gzip-oversize'):
                    import gzip
                    raw=gzip.compress(b'compressed resource' if self.path=='/gzip-resource' else b'x'*5_000_100)
                    self.send_response(200);self.send_header('Content-Encoding','gzip');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
                if self.path=='/late-hours':
                    self.send('''<h1>Hours detail</h1><div id="data"></div><script>setTimeout(()=>{document.querySelector('#data').innerHTML='<div class="line">Location: Wonderland</div><div class="line">Today hours: 09:00 to 21:00</div>';},3500);</script>''');return
                if self.path=='/semantic':
                    self.send('''<style>.card{cursor:pointer}</style><header><nav>Unrelated navigation</nav></header><main id="content"><h1>Attraction hours</h1><div class="card" onclick="location.href='/detail/ride-1'" ng-test="noise"><span class="title">Ride 1</span></div><div class="card"><span class="title">Ride 2</span></div><table id="hours"><tr><th>Start</th><th>End</th></tr><tr><td>09:00</td><td>21:00</td></tr></table><script>window.secret='not-in-dom';</script></main>''');return
                if self.path.startswith('/many-data-'):
                    count=int(self.path.rsplit('-',1)[1]);self.send(json.dumps({'data':[{'id':str(i),'name':'Item '+str(i)} for i in range(count)]}),kind='application/json');return
                if self.path.startswith('/many-network-'):
                    count=int(self.path.rsplit('-',1)[1]);self.send('<main id="ready"></main><script>fetch("/many-data-'+str(count)+'")</script>');return
                if self.path.startswith('/many-dom-'):
                    count=int(self.path.rsplit('-',1)[1]);self.send('<main>'+''.join('<div class="card" data-id="'+str(i)+'"><span class="title">Item '+str(i)+'</span></div>' for i in range(count))+'</main>');return
                if self.path.startswith('/data'):
                    page='2' if 'page=2' in self.path else '1'
                    self.send(json.dumps({'data':[{'id':'ride-'+page,'name':'Ride '+page,'detail_url':'/detail/ride-'+page,'show_times':'11:00|13:00|16:00'}]}),kind='application/json');return
                if self.path=='/tailwind':
                    self.send('<div class="flex mb-[.2rem]" data-id="ride-1"><div class="min-h-[1.39rem]">今日运营时间待定</div></div>');return
                if self.path=='/auto-list':
                    self.send('<div class="card" data-id="static"><a class="title" href="/detail/static">Static</a></div><div class="card" data-id="dynamic"><a class="title" href="/dynamic-detail">Dynamic</a></div>');return
                if self.path=='/dynamic-detail':
                    self.send('''<h1>Dynamic hours</h1><div id="data"></div><script>fetch('/analytics-ping');setTimeout(()=>{document.querySelector('#data').innerHTML='<div class="start">10:00</div><div class="end">20:00</div>';},400);</script>''');return
                if self.path.startswith('/detail/'):
                    self.send('<h1>Ride details</h1><div class="start">09:00</div><div class="end">21:00</div><script>fetch("/analytics-ping")</script>');return
                if self.path=='/analytics-ping':
                    self.server.trace_hits+=1;self.send('{}',kind='application/json');return
                if self.path=='/login':
                    self.send('<form action="/session" method="post"><input name="username"><input name="password" type="password"><button>Sign in</button></form>');return
                if self.path=='/protected':
                    self.send('<h1>Logged in</h1><div class="value">42</div>' if 'session=fixture' in self.headers.get('Cookie','') else '<h1>Denied</h1>');return
                if self.path=='/click-list':
                    self.send('<div class="card" data-id="ride-1"><button class="title" onclick="location.href=\'/detail/ride-1\'">Ride 1</button></div>');return
                self.send('''<h1>Dynamic attractions</h1><div id="list"></div><button id="next" onclick="load(2)">Next</button>
                <script>async function load(page){let r=await fetch('/data?page='+page),d=await r.json();document.querySelector('#list').innerHTML=d.data.map(x=>`<div class="card" data-id="${x.id}"><a class="title" href="/detail/${x.id}">${x.name}</a></div>`).join('');document.querySelector('#next').disabled=page===2;}load(1);</script>''')
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0'))).decode()
                if self.path=='/session' and 'username=test-user' in body and 'password=test-password' in body:
                    self.send('',status=303,Location='/protected',**{'Set-Cookie':'session=fixture; Path=/; HttpOnly'})
                else:self.send('Denied',status=403)
            def log_message(self,*args):pass
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.server.trace_hits=0;cls.base='http://127.0.0.1:'+str(cls.server.server_port)
        cls.worker=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();super().tearDownClass()

    def browser(self,**extra):
        browser=BrowserSession(approved_urls=[self.base],allow_private=True,**extra);self.addCleanup(browser.close);return browser

    def test_dynamic_dom_pagination_and_detail_pages_generate_real_feed(self):
        browser=self.browser();data=browser.call('collect',spec=web_spec(self.base+'/'))
        self.assertEqual([row['id'] for row in data['records']],['ride-1','ride-2'])
        self.assertEqual(data['records'][0]['start'],'09:00');self.assertTrue(data['records'][1]['source_url'].endswith('/detail/ride-2'))
        batch=build_batch(web_spec(self.base+'/'),data);self.assertIn('21:00',batch['items'][0]['content'])

    def test_html_details_use_real_document_without_unrelated_scripts(self):
        browser=self.browser();spec=web_spec(self.base+'/');spec['browser']['output']['detail']['render']='html'
        before=self.server.trace_hits;data=browser.call('collect',spec=spec)
        self.assertEqual(len(data['records']),2);self.assertEqual(data['records'][0]['start'],'09:00')
        self.assertEqual(self.server.trace_hits,before);self.assertFalse(browser.document_only)

    def test_network_analysis_and_browser_json_capture(self):
        browser=self.browser();browser.call('open',url=self.base+'/')
        network=browser.call('network')['responses'];self.assertTrue(network)
        self.assertEqual(browser.call('response',id=network[-1]['id'])['data']['data'][0]['id'],'ride-1')
        spec=web_spec(self.base+'/');spec['browser']={'output':{'kind':'network','url_contains':'/data?page=1','path':'data'}}
        self.assertEqual(browser.call('collect',spec=spec)['records'][0]['name'],'Ride 1')
        slug=browser.call('extract',rows='.card',fields={'id':{'selector':'a','attribute':'href','regex':'/detail/(.+)$','group':1}})
        self.assertEqual(slug['records'][0]['id'],'ride-1')

    def test_semantic_observation_has_real_refs_clean_html_tables_and_fast_missing_selector(self):
        browser=self.browser();first=browser.call('open',url=self.base+'/semantic')
        self.assertEqual(first['root_selector'],'#content');self.assertEqual(first['observation_type'],'semantic_dom')
        self.assertNotIn('Unrelated navigation',first['html']);self.assertNotIn('ng-test',first['html']);self.assertNotIn('<script',first['html'])
        self.assertEqual(first['tables'][0]['rows'][1],['09:00','21:00'])
        self.assertTrue(any(row['selector']=='.card' and row['count']==2 for row in first['structures']))
        start=time.monotonic();fallback=browser.call('snapshot',selector='nonexistent-widget')
        self.assertLess(time.monotonic()-start,3);self.assertFalse(fallback['selector_matched']);self.assertTrue(fallback['elements'])
        ref=next(row['ref'] for row in first['elements'] if row['selector']=='.card')
        result=browser.call('extract',rows='.card',fields={'name':{'selector':'.title'},'missing':{'selector':'.never'}})
        self.assertFalse(result['ok']);self.assertEqual(result['missing_fields'],['missing']);self.assertEqual(result['records'][0]['name'],'Ride 1')
        clicked=browser.call('action',step={'action':'click','selector':'@'+ref});self.assertIn('09:00',clicked['text'])
        stamp=browser.call('extract',rows='body',fields={'timestamp':{'source':'now'}})['records'][0]['timestamp']
        from datetime import datetime
        self.assertIsNotNone(datetime.fromisoformat(stamp).tzinfo)

    def test_empty_extract_is_failure_and_transient_refs_cannot_be_persisted(self):
        browser=self.browser();browser.call('open',url=self.base+'/semantic')
        result=browser.call('extract',rows='.not-real',fields={'name':{}});self.assertFalse(result['ok']);self.assertEqual(result['matched'],0)
        for selector in ('@e12','[data-fos-ref="e12"]'):
            plan={'output':{'rows':selector,'fields':{'name':{}}}}
            with self.assertRaisesMessage(ValueError,'瞬时元素编号'):validate_plan(plan)

    def test_required_fields_wait_for_actual_data_and_regex_tries_all_candidates(self):
        browser=self.browser();browser.call('open',url=self.base+'/late-hours')
        import asyncio
        result=asyncio.run_coroutine_threadsafe(browser._fields(browser.page.locator('body'),
            {'start':{'selector':'.line','regex':'Today hours: (\\d+:\\d+)','group':1}}),browser.loop).result(timeout=10)
        self.assertEqual(result[0]['start'],'09:00')

    def test_gzip_is_decoded_and_limited_by_decompressed_size(self):
        from kb.browser_runtime import origin
        options={'allow_private':True,'private_origins':{origin(self.base)}}
        self.assertEqual(fetch_resource(self.base+'/gzip-resource','GET',{},None,**options)[2],b'compressed resource')
        self.assertEqual(len(fetch_resource(self.base+'/gzip-oversize','GET',{},None,**options)[2]),5_000_100)
        with patch('kb.browser_runtime.MAX_BYTES',5_000_000):
            with self.assertRaisesMessage(ValueError,'限制'):fetch_resource(self.base+'/gzip-oversize','GET',{},None,**options)

    def test_href_prefix_normalizes_observed_paths_without_accessing_internal_host(self):
        browser=self.browser();browser.call('open',url=self.base+'/')
        result=browser.call('extract',rows='.card',fields={'link':{'selector':'a','attribute':'href',
            'regex':'/detail/[^/?#]+','prefix':self.base}})
        self.assertEqual(result['records'][0]['link'],self.base+'/detail/ride-1')

    def test_field_failure_names_page_selector_and_original_value(self):
        browser=self.browser();browser.call('open',url=self.base+'/detail/ride-1')
        import asyncio
        future=asyncio.run_coroutine_threadsafe(browser._fields(browser.page.locator('body'),
            {'start':{'selector':'.start','regex':'never-match'}}),browser.loop)
        with self.assertRaises(ValueError) as caught:future.result(timeout=10)
        self.assertIn('/detail/ride-1',str(caught.exception));self.assertIn('.start',str(caught.exception));self.assertIn('09:00',str(caught.exception))

    def test_click_only_cards_and_login_credentials_replay(self):
        browser=self.browser(credential_origin=self.base,login={'username':'test-user','password':'test-password'})
        browser.call('open',url=self.base+'/login')
        browser.call('action',step={'action':'fill','selector':'[name=username]','credential':'username'})
        browser.call('action',step={'action':'fill','selector':'[name=password]','credential':'password'})
        result=browser.call('action',step={'action':'click','selector':'button'})
        self.assertIn('Logged in',result['text'])
        spec=web_spec(self.base+'/click-list');spec['browser']['output'].pop('next_selector');spec['browser']['output']['max_pages']=1
        spec['browser']['output']['fields']={'id':{'attribute':'data-id'},'name':{'selector':'.title'}}
        spec['browser']['output']['detail']['click_selector']='.title';spec['browser']['output']['detail'].pop('url_field')
        self.assertEqual(browser.call('collect',spec=spec)['records'][0]['end'],'21:00')


    def test_network_list_really_visits_details_and_injects_system_dates(self):
        spec=web_spec(self.base+'/#/list');spec['browser']={'output':{
            'kind':'network','url_contains':'/data?page=1','path':'data',
            'fields':{'collected_date':{'source':'today'},'name':{'path':'name'}},
            'detail':{'url_field':'detail_url','fields':{'start':{'selector':'.start'},'end':{'selector':'.end'},'date':{'source':'today'}}}}}
        spec['body_fields']+=[{'label':'Date','source':'today'},{'label':'Collected at','source':'now'},
            {'label':'Duration','computed':{'op':'duration','start':'start','end':'end','format':'hhmm','unit':'hours'}}]
        data=self.browser().call('collect',spec=spec)
        row=data['records'][0];self.assertEqual(row['start'],'09:00');self.assertEqual(row['end'],'21:00')
        self.assertEqual(row['date'],row['collected_date']);self.assertTrue(row['source_url'].endswith('/detail/ride-1'))
        fields=build_batch(spec,data)['items'][0]['fields'];self.assertEqual(fields['Duration'],12)
        self.assertEqual(fields['Date'],row['date']);self.assertIn('+08:00',fields['Collected at'])

    def test_tailwind_selectors_and_format_fallback_keep_actual_page_text(self):
        spec=web_spec(self.base+'/tailwind');spec.pop('on_error')
        spec['browser']={'actions':[{'action':'wait','selector':'.flex.mb-[.2rem]'}],
            'output':{'rows':'.flex.mb-[.2rem]','fields':{'id':{'attribute':'data-id'},
            'start':{'selector':'.min-h-[1.39rem]','regex':'(\\d+:\\d+)','group':1}}}}
        spec['body_fields']=[{'label':'Start','path':'start'},{'label':'Date','source':'today'}]
        data=self.browser().call('collect',spec=spec)
        self.assertEqual(len(data['records']),1);self.assertIsNone(data['records'][0]['start'])
        fields=build_batch(spec,data)['items'][0]['fields']
        self.assertIn('今日运营时间待定',fields['Start']);self.assertIn('采集提示',fields)

    def test_auto_details_skip_static_scripts_and_load_dynamic_data_only_when_needed(self):
        spec=web_spec(self.base+'/auto-list');before=self.server.trace_hits
        data=self.browser().call('collect',spec=spec)
        self.assertEqual([row['start'] for row in data['records']],['09:00','10:00'])
        self.assertEqual(self.server.trace_hits-before,1)
        stats=data['__fos_browser_stats'];self.assertEqual(stats['html_details'],1);self.assertEqual(stats['dynamic_details'],1)
        self.assertLess(stats['requests'],12)

    def test_all_regex_extracts_every_match_in_one_node_and_network_text(self):
        browser=self.browser();browser.call('open',url=self.base+'/semantic')
        fields={'times':{'regex':r'\d{1,2}:\d{2}','all':True}}
        result=browser.call('extract',rows='#hours',fields=fields)
        self.assertEqual(result['records'][0]['times'],['09:00','21:00'])
        missing=browser.call('extract',rows='#hours',fields={'times':{'regex':'not-present','all':True}})
        self.assertIsNone(missing['records'][0]['times']);self.assertFalse(missing['ok'])
        spec=web_spec(self.base+'/');spec['body_fields']=[{'label':'Count','computed':{'op':'count','args':[{'field':'times'}]}}]
        spec['browser']={'output':{'kind':'network','url_contains':'/data?page=1','path':'data','fields':{'times':{'path':'show_times','regex':r'\d{1,2}:\d{2}','all':True}}}}
        data=browser.call('collect',spec=spec)
        self.assertEqual(data['records'][0]['times'],['11:00','13:00','16:00']);self.assertEqual(build_batch(spec,data)['items'][0]['fields']['Count'],3)


    def test_dom_collection_accepts_500_records_and_preserves_last_row(self):
        candidate=web_spec(self.base+'/many-dom-500');candidate['body_fields']=[{'label':'Name','path':'name'}]
        candidate['browser']={'output':{'kind':'dom','rows':'.card','fields':{'id':{'attribute':'data-id'},'name':{'selector':'.title'}}}}
        result=self.browser().call('collect',spec=candidate)
        self.assertEqual(len(result['records']),500);self.assertEqual(result['records'][-1]['id'],'499')
        self.assertEqual(len(build_batch(candidate,result)['items']),500)

    def test_dom_collection_accepts_more_than_old_500_limit(self):
        candidate=web_spec(self.base+'/many-dom-501')
        candidate['browser']={'output':{'kind':'dom','rows':'.card','fields':{'id':{'attribute':'data-id'},'name':{'selector':'.title'}}}}
        result=self.browser().call('collect',spec=candidate)
        self.assertEqual(len(result['records']),501);self.assertEqual(result['records'][-1]['id'],'500')

    def test_browser_network_collection_accepts_more_than_old_limit_and_rejects_50001(self):
        for count in (501,MAX_RECORDS+1):
            with self.subTest(count=count):
                candidate=web_spec(self.base+'/many-network-'+str(count))
                candidate['browser']={'output':{'kind':'network','url_contains':'/many-data-','path':'data'}}
                browser=self.browser()
                if count==501:
                    result=browser.call('collect',spec=candidate)
                    self.assertEqual(len(result['records']),501);self.assertEqual(result['records'][-1]['id'],'500')
                else:
                    with self.assertRaisesMessage(ValueError,'50000条'):browser.call('collect',spec=candidate)


class BrowserAgentTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('browser-agent-admin',is_staff=True)
        self.job=ConnectorDraft.objects.create(user=self.user,prompt='打开 https://website.example.test/list，采集项目及运营时间',api_url=web_spec()['url'])
        self.data={'records':[{'id':'ride-1','name':'Ride 1','start':'09:00','end':'21:00','source_url':'https://website.example.test/detail/ride-1'}]}

    def test_html_is_routed_to_browser_and_recipe_is_actually_validated(self):
        spec=web_spec()
        model=FakeModel([call('probe_api',{'url':spec['url']}),call('test_browser_connector',{'spec':spec},'test'),answer(spec)])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.connector_agent.read_url',return_value=(200,{'content-type':'text/html'},b'<html>attractions</html>')),patch('kb.connector_browser.collect',return_value=self.data) as collect:
            result=run(self.job.pk)
        self.assertEqual(result.status,'ready',result.error);self.assertTrue(result.browser_state['tested']);collect.assert_called_once()
        self.assertNotIn('limit',collect.call_args.kwargs);self.assertEqual(result.browser_state['validation_scope'],'full_batch')
        self.assertIn('browser_open',str(model.messages));self.assertIn('09:00',result.preview['preview'][0]['content'])
        self.assertEqual(KnowledgeFeed.objects.count(),0)
        self.assertEqual(result.metrics['calls'],2)

    def test_unreplayed_browser_recipe_is_never_ready(self):
        model=FakeModel([answer(web_spec())]*2)
        with patch('kb.agent._get_llm',return_value=model),patch('kb.connector_agent.MAX_CALLS',2):result=run(self.job.pk)
        self.assertEqual(result.status,'failed');self.assertIn('重放',result.error)

    def test_old_three_record_preview_cannot_be_enabled_without_full_validation(self):
        self.job.status='ready';self.job.spec=web_spec();self.job.preview={'valid':True};self.job.browser_state={'tested':True,'records':3}
        self.job.save(update_fields=['status','spec','preview','browser_state']);self.client.force_login(self.user)
        response=self.client.post(reverse('kb:connector_enable',args=[self.job.pk]),'{}',content_type='application/json')
        self.assertEqual(response.status_code,400);self.assertIn('完整批次',response.json()['error']);self.assertEqual(KnowledgeFeed.objects.count(),0)

    def test_observed_public_search_result_can_be_used_without_forwarding_credentials(self):
        target='https://public-discovered.example.test/list';candidate=web_spec(target)
        with self.assertRaises(ValueError):check_spec(self.job,candidate)
        self.job.browser_state={'observed_urls':[target]}
        self.assertEqual(check_spec(self.job,candidate)['url'],target)
        self.job.header_env={'Authorization':'FOS_FEED_TEST_AUTH'};candidate['header_env']=self.job.header_env
        with self.assertRaises(ValueError):check_spec(self.job,candidate)

    def test_scheduled_feed_uses_browser_collector_and_login_storage(self):
        with tempfile.TemporaryDirectory() as directory,self.settings(DATA_DIR=Path(directory)):
            credential=store(self.user,web_spec()['url'],{'type':'login','username':'test-user','password':'test-password'})
            self.assertEqual(credential.header_names,[]);self.assertNotIn('test-password',credential.encrypted_value)
            feed=SimpleNamespace(adapter_spec=web_spec(),credential=credential,header_env={},url=web_spec()['url'],allow_private=False)
            with patch('kb.connector_browser.collect',return_value=self.data) as collect:payload,_=fetch_batch(feed)
            collect.assert_called_once();self.assertEqual(payload['items'][0]['id'],'ride-1')


class BrowserTransportTests(SimpleTestCase):
    def test_unrelated_trackers_are_skipped_but_declared_origins_still_work(self):
        async def exercise(approved):
            fetch=Mock(return_value=(200,{},b'{}',[]));browser=BrowserSession(approved_urls=approved,fetcher=fetch)
            try:
                route=SimpleNamespace(fulfill=AsyncMock(),abort=AsyncMock())
                request=SimpleNamespace(url='https://www.google-analytics.com/collect',method='GET',resource_type='fetch',post_data_buffer=None,all_headers=AsyncMock(return_value={}))
                await browser._route(route,request);return fetch.call_count,browser.skipped_requests
            finally:browser.close()
        self.assertEqual(asyncio.run(exercise(['https://website.example.test'])),(0,1))
        self.assertEqual(asyncio.run(exercise(['https://www.google-analytics.com'])),(1,0))

    def test_public_assets_reuse_cookies_but_private_assets_are_isolated(self):
        async def exercise(policy,cookies):
            fetch=Mock(return_value=(200,{'cache-control':policy},b'let x=1;',[]))
            browser=BrowserSession(approved_urls=['https://example.test'],fetcher=fetch)
            try:
                for cookie in cookies:
                    route=SimpleNamespace(fulfill=AsyncMock(),abort=AsyncMock())
                    request=SimpleNamespace(url='https://example.test/app.js',method='GET',resource_type='script',post_data_buffer=None,
                        all_headers=AsyncMock(return_value={'cookie':cookie}))
                    await browser._route(route,request)
                return fetch.call_count
            finally:browser.close()
        self.assertEqual(asyncio.run(exercise('public, max-age=300',['a=1','a=2'])),1)
        self.assertEqual(asyncio.run(exercise('private, max-age=300',['a=1','a=2'])),2)
        self.assertEqual(asyncio.run(exercise('no-store',['a=1','a=1'])),2)

    def test_connection_pool_keeps_tls_origins_separate_even_for_same_ip(self):
        from kb.browser_runtime import ResourceTransport
        pool=ResourceTransport()
        try:
            first=pool.client(('https','one.example',443,'1.1.1.1'))
            self.assertIs(pool.client(('https','one.example',443,'1.1.1.1')),first)
            self.assertIsNot(pool.client(('https','two.example',443,'1.1.1.1')),first)
        finally:pool.close()
