from __future__ import annotations

"""可独立分发的 Playwright 采集运行时：有限网页动作、DOM/网络取数及详情页遍历。"""

import asyncio
import concurrent.futures
import hashlib
import http.cookies
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

try:from .browser_dom import OBSERVE_SCRIPT
except ImportError:from browser_dom import OBSERVE_SCRIPT

MAX_RECORDS=500
MAX_REQUESTS=600
MAX_BYTES=60_000_000
COLLECT_TIMEOUT_SECONDS=20*60


def origin(url: str) -> tuple:
    parts=urlsplit(url)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('浏览器只接受不含用户名或密码的 HTTP(S) 地址。')
    return parts.scheme,parts.hostname.lower(),parts.port or (443 if parts.scheme=='https' else 80)


def validate_plan(plan: dict,*,allow_refs=False) -> dict:
    """校验声明式浏览器步骤，不允许模型脚本、eval 或无限循环。"""
    if not isinstance(plan,dict) or not plan.keys()<={'actions','output','max_records'}:raise ValueError('网页采集配置字段无效。')
    if not allow_refs:
        def stable(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key in ('selector','rows','click_selector','next_selector') and isinstance(item,str) and (re.search(r'@e\d+|data-fos-ref',item)):
                        raise ValueError('采集脚本不能保存当前页面的瞬时元素编号，请使用观察结果中的真实稳定CSS选择器。')
                    stable(item)
            elif isinstance(value,list):
                for item in value:stable(item)
        stable(plan)
    maximum=plan.get('max_records',MAX_RECORDS)
    if not isinstance(maximum,int) or isinstance(maximum,bool) or not 1<=maximum<=MAX_RECORDS:raise ValueError('每批网页采集最多500条记录。')
    actions=plan.get('actions',[])
    def steps(values):
        if not isinstance(values,list) or len(values)>30:raise ValueError('网页动作最多30步。')
        for step in values:
            if not isinstance(step,dict) or not step.keys()<={'action','selector','url','value','credential','state','pixels','milliseconds'}:raise ValueError('网页动作字段无效。')
            action=step.get('action')
            if action not in ('goto','click','fill','select','wait','scroll'):raise ValueError('仅支持打开、点击、填写、选择、等待和滚动。')
            for key in ('selector','url','value','credential','state'):
                if key in step and (not isinstance(step[key],str) or len(step[key])>2000):raise ValueError('网页动作参数过长或格式无效。')
            if action=='goto':origin(step.get('url',''))
            elif action in ('click','fill','select') and not step.get('selector'):raise ValueError('网页动作需要 selector。')
            if step.get('credential') not in (None,'username','password'):raise ValueError('只允许引用已保存的网页登录账号或密码。')
            if action in ('fill','select') and 'value' not in step and 'credential' not in step:raise ValueError('填写或选择需要 value 或 credential 引用。')
            if step.get('state','visible') not in ('visible','attached','hidden'):raise ValueError('不支持的等待状态。')
            for key,limit in (('pixels',4000),('milliseconds',5000)):
                if key in step and (not isinstance(step[key],int) or isinstance(step[key],bool) or not 0<=step[key]<=limit):raise ValueError('网页等待或滚动超出范围。')
    steps(actions)
    output=plan.get('output')
    if not isinstance(output,dict):raise ValueError('browser.output必须是取数对象；dom需rows/fields，network需url_contains/path。')
    if 'max_records' in output:
        from copy import deepcopy
        plan=deepcopy(plan);output=plan['output']
        if 'max_records' in plan and plan['max_records']!=output['max_records']:raise ValueError('browser.max_records与browser.output.max_records冲突。')
        plan['max_records']=output.pop('max_records')
        maximum=plan['max_records']
        if type(maximum) is not int or not 1<=maximum<=MAX_RECORDS:raise ValueError('browser.max_records须为1至500。')
    unsupported=output.keys()-{'kind','rows','fields','detail','next_selector','max_pages','url_contains','path'}
    if unsupported:raise ValueError('browser.output含不支持的字段：'+', '.join(sorted(unsupported))+'。max_records应放在browser内，与output平级。')
    if output.get('kind','dom')=='network':
        if not isinstance(output.get('url_contains'),str) or not output['url_contains'] or len(output['url_contains'])>1000:raise ValueError('网络取数需要实际观察到的 url_contains。')
        if not isinstance(output.get('path',''),str):raise ValueError('网络取数 path 必须是 JSON 路径。')
        if output.get('detail') and (not isinstance(output['detail'],dict) or not output['detail'].get('url_field')):raise ValueError('network详情需要url_field；点击式详情请使用dom模式。')
        validate_plan({'output':{'kind':'dom','rows':'body','fields':output.get('fields') or {'_record':{}},**({'detail':output['detail']} if output.get('detail') else {})}},allow_refs=allow_refs)
    elif output.get('kind','dom')=='dom':
        if not isinstance(output.get('rows'),str) or not output['rows'] or len(output['rows'])>1000:raise ValueError('网页列表需要 rows 选择器。')
        def fields(mapping):
            if not isinstance(mapping,dict) or not 1<=len(mapping)<=30:raise ValueError('网页字段必须是1至30项映射。')
            for name,field in mapping.items():
                if not isinstance(name,str) or not 1<=len(name)<=100 or not isinstance(field,dict) or not field.keys()<={'selector','attribute','all','regex','group','required','source','prefix','default','path'}:raise ValueError('网页字段配置无效。')
                if 'default' in field and (not isinstance(field['default'],str) or len(field['default'])>500):raise ValueError('网页缺失提示 default 必须是不超过500字符的文本。')
                for key in ('selector','attribute','regex','prefix','path'):
                    if key in field and (not isinstance(field[key],str) or len(field[key])>1000):raise ValueError('网页选择器或模式格式无效。')
                if field.get('source') not in (None,'url','today','now'):raise ValueError('字段 source 仅支持 url、today（日期）或 now（含时区的采集时间）。')
                if any(key in field and not isinstance(field[key],bool) for key in ('all','required')):raise ValueError('all/required 必须是布尔值。')
                if not isinstance(field.get('group',0),int) or not 0<=field.get('group',0)<=8:raise ValueError('regex group 超出范围。')
                pattern=field.get('regex','')
                if len(pattern)>300 or re.search(r'\\[1-9]|\(\?[=!<]|\([^)]*[+*{][^)]*\)[+*{]',pattern):raise ValueError('网页取数模式包含不支持的复杂表达式。')
                try:re.compile(pattern)
                except re.error as exc:raise ValueError('网页取数 regex 无效。') from exc
        fields(output.get('fields'))
        detail=output.get('detail')
        if detail:
            if not isinstance(detail,dict) or not detail.keys()<={'url_field','click_selector','fields','actions','render'} or not (detail.get('url_field') or detail.get('click_selector')):raise ValueError('详情页需要 url_field 或 click_selector。')
            if detail.get('render','auto') not in ('auto','browser','html'):raise ValueError('详情render仅支持auto、browser或html。')
            if detail.get('render')=='html' and not detail.get('url_field'):raise ValueError('HTML详情模式需要实际url_field链接，点击操作请使用browser模式。')
            for key in ('url_field','click_selector'):
                if key in detail and (not isinstance(detail[key],str) or len(detail[key])>1000):raise ValueError('详情页定位格式无效。')
            fields(detail.get('fields'));steps(detail.get('actions',[]))
        if 'next_selector' in output and (not isinstance(output['next_selector'],str) or len(output['next_selector'])>1000):raise ValueError('翻页选择器格式无效。')
        if not isinstance(output.get('max_pages',1),int) or not 1<=output.get('max_pages',1)<=10:raise ValueError('每批最多浏览10页列表。')
    else:raise ValueError('output.kind 仅支持 dom 或 network。')
    return plan


def fetch_resource(url,method,headers,body,*,private_origins=(),allow_private=False,_client_for=None):
    """固定 DNS 后代理浏览器请求，既有离线策略仍对实际 HTTP 连接生效。"""
    import httpx
    scheme,host,port=origin(url)
    addresses=list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)))
    if not addresses:raise ValueError('网页主机没有可用地址。')
    private=allow_private and origin(url) in private_origins
    for address in addresses:
        ip=ipaddress.ip_address(address)
        if ip.version==6 and ip.ipv4_mapped:ip=ip.ipv4_mapped
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or (ip.is_reserved and not (private and ip.is_loopback)) or (not ip.is_global and not private):raise ValueError('网页请求指向未授权的内网地址。')
    original=httpx.URL(url)
    values={k:v for k,v in headers.items() if k.lower() not in ('host','content-length','accept-encoding','connection','transfer-encoding')}
    values.update({'Host':original.netloc.decode(),'Accept-Encoding':'gzip, deflate'})
    if not any(key.lower()=='cookie' for key in values):values['Cookie']=''
    start=time.monotonic();data=bytearray()
    pooled=_client_for((scheme,host,port,addresses[0])) if _client_for else None
    with (nullcontext(pooled) if pooled else httpx.Client(timeout=httpx.Timeout(15,connect=5),follow_redirects=False,trust_env=False)) as client:
        with client.stream(method,original.copy_with(host=addresses[0]),headers=values,content=body,extensions={'sni_hostname':host}) as response:
            for part in response.iter_bytes(chunk_size=65536):
                data.extend(part)
                if len(data)>5_000_000:raise ValueError('网页资源超过5MB限制。')
                if time.monotonic()-start>25:raise ValueError('网页资源读取超过25秒限制。')
            cookies=response.headers.get_list('set-cookie')
            outgoing={k:v for k,v in response.headers.items() if k.lower() not in ('set-cookie','content-length','content-encoding','transfer-encoding','content-security-policy-report-only','report-to','nel')}
            return response.status_code,outgoing,bytes(data),cookies


class ResourceTransport:
    """每次采集独立的连接池；按原主机和固定IP隔离TLS及Cookie。"""
    def __init__(self):self.clients={};self.lock=threading.Lock()

    def client(self,key):
        import httpx
        with self.lock:
            if key not in self.clients:
                if len(self.clients)>=64:return None
                self.clients[key]=httpx.Client(timeout=httpx.Timeout(15,connect=5),follow_redirects=False,trust_env=False,
                    limits=httpx.Limits(max_connections=6,max_keepalive_connections=4,keepalive_expiry=60))
            return self.clients[key]

    def __call__(self,*args,**kwargs):return fetch_resource(*args,**kwargs,_client_for=self.client)

    def close(self):
        with self.lock:clients=list(self.clients.values());self.clients.clear()
        for client in clients:client.close()


class BrowserSession:
    """独立事件循环中的真实浏览器，避免与模型的异步流互相阻塞。"""
    def __init__(self,*,approved_urls,headers=None,credential_origin=None,login=None,allow_private=False,screenshot_path=None,checkpoint=None,timezone='Asia/Shanghai',fetcher=None):
        self.approved={origin(url) for url in approved_urls};self.private_origins=frozenset(self.approved)
        self.headers=headers or {};self.credential_origin=origin(credential_origin) if credential_origin else None
        self.login=login or {};self.allow_private=allow_private;self.screenshot_path=Path(screenshot_path) if screenshot_path else None;self.checkpoint=checkpoint
        self.transport=ResourceTransport() if fetcher is None else None
        self.zone=timezone;self.fetcher=fetcher or self.transport;self.requests=0;self.bytes=0;self.network=[];self.responses={};self.response_count=0;self.visited=[];self.blocked=[]
        self.assets={};self.asset_bytes=0
        self.cache_hits=0;self.skipped_requests=0;self.html_details=0;self.dynamic_details=0
        self.pending_requests=0;self.last_observation={};self.document_revision=0
        self.document_only=False
        self.tolerant=False;self.collection_stamp=None
        self.loop=asyncio.ProactorEventLoop() if sys.platform=='win32' else asyncio.new_event_loop()
        self.thread=threading.Thread(target=self.loop.run_forever,name='connector-browser',daemon=True);self.thread.start()
        self.page=None;self.context=None;self.browser=None;self.playwright=None

    def call(self,operation,**arguments):
        future=asyncio.run_coroutine_threadsafe(self._dispatch(operation,arguments),self.loop)
        start=time.monotonic()
        while True:
            try:return future.result(timeout=1)
            except concurrent.futures.TimeoutError:
                try:
                    if self.checkpoint:self.checkpoint()
                    if time.monotonic()-start>(COLLECT_TIMEOUT_SECONDS+10 if operation=='collect' else 65):raise ValueError('浏览器操作超时。')
                except Exception:
                    future.cancel();raise

    def close(self):
        if self.loop.is_running():
            try:asyncio.run_coroutine_threadsafe(self._close(),self.loop).result(timeout=10)
            except Exception:pass
            self.loop.call_soon_threadsafe(self.loop.stop);self.thread.join(timeout=2)
            if not self.loop.is_running():self.loop.close()

    async def _close(self):
        for resource in (self.context,self.browser):
            if resource:
                try:await asyncio.wait_for(resource.close(),timeout=3)
                except Exception:pass
        if self.playwright:
            try:await asyncio.wait_for(self.playwright.stop(),timeout=3)
            except Exception:pass
        pending=[task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in pending:task.cancel()
        if pending:await asyncio.gather(*pending,return_exceptions=True)
        if self.transport:await asyncio.to_thread(self.transport.close)

    async def _open(self):
        if self.page:return
        from playwright.async_api import async_playwright
        self.playwright=await async_playwright().start()
        options={'headless':True,'args':['--disable-background-networking','--disable-component-update','--disable-default-apps','--force-webrtc-ip-handling-policy=disable_non_proxied_udp']}
        edge=Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe'
        executable=os.environ.get('FOS_BROWSER_EXECUTABLE','')
        if executable:options['executable_path']=executable
        elif os.name=='nt' and edge.exists():options['channel']='msedge'
        self.browser=await self.playwright.chromium.launch(**options)
        self.context=await self.browser.new_context(viewport={'width':1440,'height':1050},locale='en-US',timezone_id=self.zone,service_workers='block',accept_downloads=False)
        self.context.set_default_timeout(8000);self.context.set_default_navigation_timeout(25000)
        await self.context.route('**/*',self._route)
        await self.context.route_web_socket('**/*',lambda route:route.close())
        self.page=await self.context.new_page();self.page.set_default_timeout(8000);self.page.set_default_navigation_timeout(25000)
        self.page.on('dialog',lambda dialog:asyncio.create_task(dialog.dismiss()))

    async def _route(self,route,request):
        if self.document_only and request.resource_type!='document':
            await route.abort();return
        if request.resource_type in ('image','font','media') or request.method not in ('GET','HEAD','POST'):
            await route.abort();return
        host=urlsplit(request.url).hostname or ''
        trackers=('google-analytics.com','googletagmanager.com','doubleclick.net','connect.facebook.net','clarity.ms','bat.bing.com')
        if origin(request.url) not in self.approved and any(host==suffix or host.endswith('.'+suffix) for suffix in trackers):
            self.skipped_requests+=1;await route.abort();return
        self.pending_requests+=1
        try:
            target=origin(request.url)
            values=await request.all_headers()
            if self.credential_origin==target:values.update(self.headers)
            elif self.headers:
                for key in self.headers:values.pop(key.lower(),None)
            cacheable=request.method=='GET' and request.resource_type in ('script','stylesheet')
            sensitive={'cookie','authorization',*(key.lower() for key in self.headers)}
            cache_key=(request.url,tuple(sorted((key.lower(),value) for key,value in values.items() if key.lower() in sensitive)))
            public_key=(request.url,'public')
            cached=self.assets.get(public_key) or self.assets.get(cache_key) if cacheable else None
            if cached:
                self.cache_hits+=1;status,headers,body=cached;await route.fulfill(status=status,headers=headers,body=body);return
            self.requests+=1
            if self.requests>MAX_REQUESTS or self.bytes>MAX_BYTES:raise ValueError('网页请求量超过本次采集上限。请减少整页导航与逐条详情访问，优先network或html渲染取数，并用max_pages缩小范围；图片等静态资源已自动拦截，无需处理。')
            status,headers,body,cookies=await asyncio.to_thread(self.fetcher,request.url,request.method,values,request.post_data_buffer,
                private_origins=self.private_origins,allow_private=self.allow_private)
            self.bytes+=len(body)
            policy=headers.get('cache-control','').lower();vary=headers.get('vary','').lower()
            if cacheable and status==200 and not cookies and not any(flag in policy for flag in ('no-store','no-cache')) and '*' not in vary and self.asset_bytes+len(body)<=20_000_000:
                key=public_key if 'public' in policy and 'private' not in policy and not any(name in vary for name in sensitive) else cache_key
                self.assets[key]=(status,headers,body);self.asset_bytes+=len(body)
            for raw in cookies:
                try:
                    jar=http.cookies.SimpleCookie();jar.load(raw)
                    for key,morsel in jar.items():
                        cookie={'name':key,'value':morsel.value,'domain':urlsplit(request.url).hostname,'path':morsel['path'] or '/',
                                'httpOnly':bool(morsel['httponly']),'secure':bool(morsel['secure'])}
                        if morsel['max-age']=='0':cookie['expires']=1
                        # URL-bound cookies cannot widen their scope to another host.
                        await self.context.add_cookies([cookie])
                except Exception:pass
            if 'json' in headers.get('content-type','').lower() and status==200 and len(body)<=2_000_000:
                try:
                    data=json.loads(body)
                    self.response_count+=1;identity=str(self.response_count);self.responses[identity]=data
                    self.network.append({'id':identity,'url':request.url,'method':request.method,'status':status,
                        'preview':json.dumps(data,ensure_ascii=False)[:1800]})
                    if len(self.network)>80:
                        removed=self.network.pop(0);self.responses.pop(removed['id'],None)
                except (ValueError,UnicodeError):pass
            await route.fulfill(status=status,headers=headers,body=body)
        except Exception as exc:
            self.blocked.append({'url':request.url,'reason':str(exc)[:200] if isinstance(exc,ValueError) else type(exc).__name__})
            try:await route.abort()
            except Exception:pass
        finally:self.pending_requests-=1

    async def _settle(self):
        """短暂观察动态正文是否稳定；持续联网的页面也有明确截止时间。"""
        prior=None;steady=0
        for _ in range(12):
            await self.page.wait_for_timeout(250)
            try:current=await self.page.locator('body').evaluate('(e)=>[e.innerText.length,e.childElementCount,location.href]',timeout=700)
            except Exception:continue
            steady=steady+1 if current==prior else 0;prior=current
            if steady>=2 and self.pending_requests==0:break

    async def _goto(self,url,*,observe=True):
        target=origin(url)
        if target not in self.approved:raise ValueError('只能主动打开用户提供的主机或已在网页中观察到的链接。')
        from playwright.async_api import TimeoutError as PlaywrightTimeout,Error as PlaywrightError
        try:response=await self.page.goto(url,wait_until='domcontentloaded')
        except PlaywrightTimeout:
            if not await self.page.locator('body').count() or len(await self.page.locator('body').inner_text(timeout=2000))<50:raise
            response=None
        except PlaywrightError as exc:
            reason=next((row['reason'] for row in reversed(self.blocked) if row['url']==url),str(exc).splitlines()[0])
            raise ValueError('网页导航失败：'+url+'；'+reason+'；已请求 '+str(self.requests)+' 个资源，读取 '+str(round(self.bytes/1_000_000,1))+' MB。') from exc
        if response and response.status>=400:raise ValueError('网页返回 HTTP '+str(response.status))
        if not self.document_only:await self._settle()
        self.visited.append(self.page.url)
        return await self._snapshot() if observe else None

    def _selector(self,selector):
        if re.fullmatch(r'@e\d+',selector):return '[data-fos-ref="'+selector[1:]+'"]'
        return re.sub(r'(?<!\\)\.([A-Za-z_][\w-]*\[[^\]]+\])',lambda match:'[class~='+json.dumps(match[1],ensure_ascii=False)+']',selector)

    async def _snapshot(self,selector='body'):
        requested=selector;warning=''
        try:
            scope=self.page.locator(self._selector(selector)).first
            matched=await scope.count()>0
        except Exception:matched=False
        if not matched:
            warning='定位不存在：'+selector+'。已立即返回当前页面的真实内容和可用元素，不必等待或重复猜同一定位。'
            selector='body';scope=self.page.locator('body')
        snapshot=await scope.evaluate(OBSERVE_SCRIPT,{'auto':selector=='body','expand':selector!='body'},timeout=2000)
        snapshot['requested_selector']=requested;snapshot['selector_matched']=matched
        if warning:snapshot['warning']=warning
        snapshot['revision']=hashlib.sha256((snapshot['url']+'\n'+snapshot['html']).encode()).hexdigest()[:16]
        for element in snapshot['elements']:
            href=element.get('href')
            if href:
                from urllib.parse import urljoin
                try:self.approved.add(origin(urljoin(self.page.url,href)))
                except ValueError:pass
        if self.screenshot_path:
            self.screenshot_path.parent.mkdir(parents=True,exist_ok=True)
            try:
                await self.page.screenshot(path=str(self.screenshot_path),full_page=False,timeout=3000)
                snapshot['screenshot_available']=True
            except Exception:snapshot['screenshot_available']=False
        snapshot['network']=self.network[-8:];snapshot['blocked_requests']=self.blocked[-4:]
        self.last_observation=snapshot
        return snapshot

    async def _action(self,step):
        action=step['action'];selector=self._selector(step.get('selector',''))
        if action in ('click','fill','select') and not await self.page.locator(selector).count():
            raise ValueError('当前页面不存在该元素：'+step.get('selector','')+'。请使用最近快照中的元素编号或真实 selector，不要重复等待。')
        if action in ('click','fill','select') and not await self.page.locator(selector).first.is_visible():
            raise ValueError('元素存在但当前不可见，请选择快照中的可见控件或先打开对应区域。')
        if action=='goto':await self._goto(step['url'])
        elif action=='click':
            await self._click(self.page.locator(selector).first)
        elif action in ('fill','select'):
            if step.get('credential'):
                if origin(self.page.url)!=self.credential_origin:raise ValueError('不能在其他主机的页面填写登录凭据。')
                if step['credential'] not in self.login:raise ValueError('请在页面保存网页登录账号与密码。')
                value=self.login[step['credential']]
            else:value=step.get('value','')
            if action=='fill':await self.page.locator(selector).first.fill(value)
            else:await self.page.locator(selector).first.select_option(value)
        elif action=='wait':
            if selector:await self.page.locator(selector).first.wait_for(state=step.get('state','visible'))
            else:await self.page.wait_for_timeout(step.get('milliseconds',1000))
        elif action=='scroll':
            await self.page.mouse.wheel(0,step.get('pixels',900));await self.page.wait_for_timeout(300)
        self.visited.append(self.page.url)

    async def _click(self,item):
        text=await item.inner_text(timeout=5000)
        if re.search(r'立即购买|确认订单|立即支付|删除|pay now|place order|checkout|delete',text,re.I):raise ValueError('采集工具不执行购买、支付或删除操作。')
        previous=self.page;pages=set(self.context.pages)
        await item.click();await self.page.wait_for_timeout(350)
        new=[page for page in self.context.pages if page not in pages]
        if new:
            self.page=new[-1]
            await self.page.wait_for_load_state('domcontentloaded')
            if previous!=self.context.pages[0]:await previous.close()
        await self._settle()

    async def _read_fields(self,rows,fields,limit=MAX_RECORDS):
        # 固定取数程序只接受选择器和字段参数，不运行模型提供的 JavaScript。
        fields={name:{**field,**({'selector':self._selector(field['selector'])} if field.get('selector') else {})} for name,field in fields.items()}
        records=await rows.evaluate_all('''(nodes,options)=>nodes.slice(0,options.limit).map(root=>{
          const capture={warnings:[],raw:{},text:(root.innerText||'').slice(0,2000)};
          const result=Object.fromEntries(Object.entries(options.fields).map(([name,f])=>{
          if(f.source==='url')return [name,location.href];if(f.source==='today'||f.source==='now')return [name,null];
          let found;try{found=f.selector?Array.from(root.querySelectorAll(f.selector)):[root];}catch(error){capture.warnings.push(name+': selector无效');capture.raw[name]=capture.text;return [name,null];}
          const values=found.map(e=>f.attribute?e.getAttribute(f.attribute):(e.innerText??e.textContent??'')).map(v=>v===null?null:String(v).trim()).filter(v=>v!==null);
          let matches=f.attribute==='href'?values.map(v=>new URL(v,location.href).href):values;
          if(f.regex){const pattern=new RegExp(f.regex,f.all?'gi':'i');matches=f.all?matches.flatMap(v=>Array.from(String(v).matchAll(pattern),m=>m[f.group||0]??null)).filter(v=>v!==null):matches.map(v=>{const m=String(v).match(pattern);return m?m[f.group||0]??null:null;}).filter(v=>v!==null);}
          if(f.prefix)matches=matches.map(v=>f.prefix+v);
          if(!matches.length||matches[0]===''){capture.raw[name]=values.join(' | ').slice(0,2000)||capture.text;if(!('default' in f))capture.warnings.push(name+': 未匹配取数规则，保留原文');}
          return [name,f.all?(matches.length?matches:null):matches[0]??null];
        }));if(capture.warnings.length)result.__fos_capture=capture;return result;})''',{'fields':fields,'limit':limit})
        for record in records:
            for name,field in fields.items():
                if field.get('source')=='today':record[name]=(self.collection_stamp or datetime.now(ZoneInfo(self.zone))).date().isoformat()
                if field.get('source')=='now':record[name]=(self.collection_stamp or datetime.now(ZoneInfo(self.zone))).isoformat()
                if 'default' in field and record.get(name) in (None,'',[]):record[name]=field['default']
        return records

    async def _fields(self,rows,fields,limit=MAX_RECORDS,allow_missing=False):
        """等待真正需要的字段而非只等待页面外壳；失败附带定位与原文诊断。"""
        def missing(records):
            return [(index,name) for index,record in enumerate(records) for name,field in fields.items()
                    if field.get('required',True) and record.get(name) in (None,'',[])]
        records=await self._read_fields(rows,fields,limit)
        if allow_missing:return records
        absent=missing(records);deadline=time.monotonic()+(2 if self.tolerant else 6)
        while absent and time.monotonic()<deadline:
            await self.page.wait_for_timeout(200)
            records=await self._read_fields(rows,fields,limit);absent=missing(records)
        if absent and not self.tolerant:
            index,name=absent[0];field=fields[name]
            raw=await self._read_fields(rows,{name:{k:v for k,v in field.items() if k not in ('regex','group')}},limit)
            value=raw[index].get(name) if index<len(raw) else None
            reason='选择器未匹配到内容' if value in (None,'',[]) else '原文不符合取数规则：'+str(value)[:180]
            raise ValueError('网页未找到必需字段：'+name+'；'+reason+'；定位：'+field.get('selector','当前记录')+'；页面：'+self.page.url)
        return records

    async def _collect(self,spec,limit=None):
        self.tolerant=spec.get('on_error','raw')!='fail'
        self.collection_stamp=datetime.now(ZoneInfo(self.zone))
        plan=validate_plan(spec['browser']);await self._goto(spec['url'],observe=False)
        for index,step in enumerate(plan.get('actions',[])):
            if index==0 and step['action']=='goto' and step['url']==spec['url']:continue
            await self._action(step)
        output=plan['output'];maximum=min(plan.get('max_records',MAX_RECORDS),limit or MAX_RECORDS)
        if output.get('kind','dom')=='network':
            for _ in range(50):
                matching=[row for row in self.network if output['url_contains'] in row['url']]
                if matching:break
                await self.page.wait_for_timeout(100)
            if not matching:raise ValueError('没有观察到匹配的真实 JSON 网络响应。')
            try:from .connector_runtime import lookup
            except ImportError:from connector_runtime import lookup
            data=lookup(self.responses[matching[-1]['id']],output.get('path',''))
            if not isinstance(data,list):raise ValueError('网络响应 path 必须指向记录数组。')
            if len(data)>MAX_RECORDS and limit is None:raise ValueError('网络响应超过500条，请在页面选择范围或使用分页。')
            records=[dict(record) if isinstance(record,dict) else {'value':record} for record in data[:maximum]]
            for record in records:
                for name,field in output.get('fields',{}).items():
                    if field.get('source')=='today':value=self.collection_stamp.date().isoformat()
                    elif field.get('source')=='now':value=self.collection_stamp.isoformat()
                    elif field.get('source')=='url':value=self.page.url
                    else:value=lookup(record,field.get('path',name),None)
                    if value is not None and field.get('regex'):
                        raw=value;candidates=value if isinstance(value,list) else [value]
                        matches=[match.group(field.get('group',0)) for candidate in candidates for match in re.finditer(field['regex'],str(candidate),re.I) if field.get('group',0)<=len(match.groups())]
                        value=(matches if field.get('all') else matches[0]) if matches else None
                        if value is None:
                            capture=record.setdefault('__fos_capture',{'warnings':[],'raw':{}})
                            capture['raw'][name]=str(raw)[:2000];capture['warnings'].append(name+'：未匹配取数规则，保留原文')
                    if value is not None and field.get('prefix'):value=field['prefix']+str(value)
                    if value is None:value=field.get('default')
                    record[name]=value
            positions=[(self.page.url,index,0) for index in range(len(records))]
        else:records=[];positions=[]
        for page_number in range(0 if output.get('kind')=='network' else output.get('max_pages',1)):
            rows=self.page.locator(self._selector(output['rows']));await rows.first.wait_for(state='attached')
            count=await rows.count()
            if len(records)+count>MAX_RECORDS and limit is None:raise ValueError('网页记录超过500条，请选择范围或配置分页。')
            remaining=maximum-len(records)
            values=await self._fields(rows,output['fields'],remaining)
            records.extend(values);positions.extend([(self.page.url,index,page_number) for index in range(len(values))])
            if len(records)>=maximum or not output.get('next_selector'):break
            next_page=self.page.locator(self._selector(output['next_selector'])).first
            if await next_page.count()==0 or not await next_page.is_visible() or not await next_page.is_enabled() or await next_page.get_attribute('aria-disabled')=='true':break
            await self._click(next_page)
        detail=output.get('detail')
        for index,record in enumerate(records):
            if detail:
                mode=detail.get('render','auto');self.document_only=bool(detail.get('url_field')) and mode in ('html','auto')
                if detail.get('url_field'):
                    url=record.get(detail['url_field'])
                    if not isinstance(url,str) or not url:
                        if not self.tolerant:raise ValueError('详情链接字段为空。')
                        capture=record.setdefault('__fos_capture',{'warnings':[],'raw':{}})
                        capture['warnings'].append('详情链接缺失，保留列表中的原始记录');record.setdefault('source_url',positions[index][0]);continue
                    # 列表中实际抽取的链接可作为下一步目标，网络策略仍逐请求检查。
                    from urllib.parse import urljoin
                    url=urljoin(positions[index][0],url);record[detail['url_field']]=url
                    self.approved.add(origin(url));await self._goto(url,observe=False)
                else:
                    url,row_index,page_number=positions[index]
                    await self._goto(spec['url'],observe=False)
                    for step in plan.get('actions',[]):await self._action(step)
                    for _ in range(page_number):await self._click(self.page.locator(self._selector(output['next_selector'])).first)
                    row=self.page.locator(self._selector(output['rows'])).nth(row_index)
                    target=row.locator(self._selector(detail['click_selector'])).first if detail['click_selector'] else row
                    await self._click(target)
                if self.document_only and mode=='auto':
                    initial=(await self._fields(self.page.locator('body'),detail['fields'],allow_missing=True))[0]
                    dom_fields=[name for name,field in detail['fields'].items() if not field.get('source')]
                    usable=all(initial.get(name) not in (None,'',[]) for name,field in detail['fields'].items() if field.get('required',True)) and (not dom_fields or any(initial.get(name) not in (None,'',[]) for name in dom_fields))
                    if detail.get('actions') or not usable:
                        self.document_only=False;self.dynamic_details+=1;await self._goto(url,observe=False)
                    else:self.html_details+=1
                elif self.document_only:self.html_details+=1
                else:self.dynamic_details+=1
                for step in detail.get('actions',[]):await self._action(step)
                try:
                    values=(await self._fields(self.page.locator('body'),detail['fields']))[0]
                    prior=record.get('__fos_capture',{});current=values.get('__fos_capture',{})
                    record.update(values)
                    if prior or current:record['__fos_capture']={'warnings':prior.get('warnings',[])+current.get('warnings',[]),'raw':{**prior.get('raw',{}),**current.get('raw',{})},'text':current.get('text') or prior.get('text','')}
                except ValueError as exc:
                    label=record.get(spec.get('title_path','name')) or record.get('name') or str(index+1)
                    raise ValueError('项目「'+str(label)[:100]+'」读取失败：'+str(exc)) from exc
                record['source_url']=self.page.url
            else:record.setdefault('source_url',positions[index][0])
        self.document_only=False
        if self.screenshot_path:await self._snapshot()
        return {'records':records,'__fos_collected_at':self.collection_stamp.isoformat(),
            '__fos_browser_stats':{'requests':self.requests,'megabytes':round(self.bytes/1_000_000,2),'cache_hits':self.cache_hits,'skipped_trackers':self.skipped_requests,'html_details':self.html_details,'dynamic_details':self.dynamic_details}}

    async def _dispatch(self,operation,arguments):
        await self._open()
        if operation=='open':return await self._goto(arguments['url'])
        if operation=='snapshot':return await self._snapshot(arguments.get('selector','body'))
        if operation=='action':
            step=arguments['step'];validate_plan({'actions':[step],'output':{'kind':'dom','rows':'body','fields':{'text':{}}}},allow_refs=True)
            await self._action(step);return await self._snapshot()
        if operation=='extract':
            validate_plan({'output':{'kind':'dom','rows':arguments.get('rows','body'),'fields':arguments['fields']}},allow_refs=True)
            rows=self.page.locator(self._selector(arguments.get('rows','body')));count=await rows.count()
            if not count:return {'ok':False,'error':'未找到记录选择器，不能将空结果当作采集成功。','matched':0,'suggested_selectors':self.last_observation.get('structures',[])[:10],'url':self.page.url}
            records=await self._fields(rows,arguments['fields'],10,allow_missing=True)
            missing=sorted({key for row in records for key,field in arguments['fields'].items() if field.get('required',True) and row.get(key) in (None,'',[])})
            return {'ok':not missing,'records':records,'matched':count,'missing_fields':missing,'url':self.page.url,
                    **({'warning':'部分字段未找到，请依据页面真实结构修正；这还不是验证成功的采集方案。'} if missing else {})}
        if operation=='network':return {'responses':self.network[-20:]}
        if operation=='response':
            data=self.responses.get(arguments['id'])
            if data is None:raise ValueError('网络响应已过期或不是 JSON。')
            return {'data':data}
        if operation=='collect':
            try:return await asyncio.wait_for(self._collect(arguments['spec'],arguments.get('limit')),timeout=COLLECT_TIMEOUT_SECONDS)
            finally:self.document_only=False;self.collection_stamp=None
        raise ValueError('不支持的浏览器操作。')
