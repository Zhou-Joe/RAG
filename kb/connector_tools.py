from __future__ import annotations

"""连接器生成工具：公开资料搜索、有限文档读取、样例脱敏与字段映射测试。"""
import json
import os
import re
from html.parser import HTMLParser
from urllib.parse import urlencode,urlsplit,urljoin,parse_qs

from .feed_http import read_url
from .feed_protocol import validate_batch
from .connector_runtime import build_batch,validate_spec,lookup,field_value,field_exempt,with_collection_date

SENSITIVE={'authorization','password','passwd','api_key','apikey','secret','access_token','refresh_token','token','cookie'}


def redact(value,secrets=()):
    if isinstance(value,dict):
        return {key:('[REDACTED]' if str(key).lower().replace('-','_') in SENSITIVE
            and not (isinstance(item,str) and re.fullmatch(r'FOS_FEED_[A-Z0-9_]+',item)) else redact(item,secrets)) for key,item in value.items()}
    if isinstance(value,list):return [redact(item,secrets) for item in value]
    if isinstance(value,str):
        for secret in secrets:
            if secret:value=value.replace(secret,'[REDACTED]')
        value=re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+','Bearer [REDACTED]',value)
    return value


def observed_json_shape(sample):
    """Give the agent real JSON Pointer paths without dumping a large response."""
    sample=redact(sample)
    paths=[];arrays=[];limited=False

    def pointer(parts):
        value=''.join('/'+str(part).replace('~','~0').replace('/','~1') for part in parts)
        return value if len(value)<=500 else None

    def kind(value):
        if value is None:return 'null'
        if isinstance(value,bool):return 'boolean'
        if isinstance(value,dict):return 'object'
        if isinstance(value,list):return 'array'
        if isinstance(value,(int,float)):return 'number'
        return 'string'

    def describe(value,path):
        item={'path':path,'type':kind(value)}
        if isinstance(value,dict):
            item['keys']=[str(key)[:80] for key in list(value)[:40]];item['count']=len(value)
            if len(value)>40 or any(len(str(key))>80 for key in list(value)[:40]):item['keys_truncated']=True
        elif isinstance(value,list):
            item['count']=len(value)
            if value and isinstance(value[0],dict):item['first_item_keys']=[str(key)[:60] for key in list(value[0])[:12]]
        elif value is not None:item['example']=str(value)[:60]
        return item

    def fields_in_record(record,parts=(),depth=0):
        fields=[]
        if not isinstance(record,dict):return fields
        for key,value in list(record.items())[:60]:
            path=pointer((*parts,key))
            if path is None:continue
            fields.append(describe(value,path))
            if isinstance(value,dict) and depth<2:fields.extend(fields_in_record(value,(*parts,key),depth+1))
            if len(fields)>=80:break
        return fields[:80]

    def record_fields(records):
        union={}
        for record in records[:50]:
            for field in fields_in_record(record):
                path=field['path']
                if path in union:
                    union[path]['seen_in']+=1
                    if field['type']!=union[path]['type']:
                        union[path].setdefault('other_types',[])
                        if field['type'] not in union[path]['other_types']:union[path]['other_types'].append(field['type'])
                elif len(union)<80:union[path]={**field,'seen_in':1}
        return list(union.values())

    def visit(value,parts=(),depth=0):
        nonlocal limited
        if len(paths)>=80:limited=True;return
        path=pointer(parts)
        if path is None:limited=True;return
        paths.append(describe(value,path))
        if isinstance(value,list):
            if len(arrays)<12:
                item_types=list(dict.fromkeys(kind(item) for item in value[:50]))
                arrays.append({'records_path':path,'count':len(value),'item_type':item_types[0] if len(item_types)==1 else 'mixed' if item_types else 'unknown',
                    'sampled_items':min(len(value),50),'item_fields':record_fields(value)})
            else:limited=True
        elif isinstance(value,dict):
            if depth>=6:
                if value:limited=True
                return
            for index,(key,child) in enumerate(value.items()):
                visit(child,(*parts,key),depth+1)
                if len(paths)>=80:
                    if index<len(value)-1:limited=True
                    break

    visit(sample)
    result={'root_type':kind(sample),'observed_paths':paths,'record_arrays':arrays,
        'paths_truncated':limited,'path_syntax':'JSON Pointer; record field paths are relative to each array item'}
    while len(json.dumps(result,ensure_ascii=False))>16000:
        candidates=[array for array in arrays if array['item_fields']]
        if candidates:
            max(candidates,key=lambda array:len(array['item_fields']))['item_fields'].pop()
        elif len(paths)>1:paths.pop()
        elif len(arrays)>1:arrays.pop()
        else:break
        result['paths_truncated']=True
    return result


class PageText(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.skip=0;self.links=[];self.link=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('script','style'):self.skip+=1
        if tag in ('p','div','h1','h2','h3','li','pre','tr'):self.parts.append('\n')
        if tag=='a' and 'result__a' in attrs.get('class',''):self.link={'url':attrs.get('href',''),'title':''}
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.skip=max(0,self.skip-1)
        if tag=='a' and self.link:self.links.append(self.link);self.link=None
    def handle_data(self,text):
        if not self.skip:self.parts.append(text)
        if self.link:self.link['title']+=text


def search_docs(query):
    if not isinstance(query,str) or not 1<=len(query)<=300:raise ValueError('搜索词必须在1至300字符之间。')
    searx=os.environ.get('FOS_FEED_SEARCH_URL','')
    key=os.environ.get('FOS_FEED_SEARCH_API_KEY','')
    if searx:
        status,headers,body=read_url(searx+('&' if '?' in searx else '?')+urlencode({'q':query,'format':'json'}),allow_private=True)
        if status!=200:raise ValueError('SearXNG 搜索返回 HTTP '+str(status))
        results=json.loads(body).get('results',[])
    elif key:
        status,headers,body=read_url('https://api.tavily.com/search',method='POST',headers={'Authorization':'Bearer '+key},
            json_body={'query':query,'search_depth':'basic','max_results':5,'include_answer':False,'include_raw_content':False})
        if status!=200:raise ValueError('Tavily 搜索返回 HTTP '+str(status))
        results=json.loads(body).get('results',[])
    else:
        status,headers,body=read_url('https://html.duckduckgo.com/html/?'+urlencode({'q':query}))
        if status!=200:raise ValueError('公开网页搜索暂不可用，请提供 API 文档链接或配置 Tavily/SearXNG。')
        parser=PageText();parser.feed(body.decode('utf-8',errors='replace'));results=parser.links
    out=[]
    for result in results:
        url=result.get('url','');url=urljoin('https://html.duckduckgo.com',url)
        url=parse_qs(urlsplit(url).query).get('uddg',[url])[0]
        if urlsplit(url).scheme in ('http','https'):
            out.append({'title':result.get('title','')[:200],'url':url,'snippet':str(result.get('content',''))[:700]})
    return out[:5]


def origin(url):
    parts=urlsplit(url)
    return (parts.scheme,parts.hostname,parts.port or (443 if parts.scheme=='https' else 80))


def declared_urls(job):
    urls=[job.api_url] if job.api_url else []
    text=job.prompt+'\n'+getattr(job,'feedback','')
    parent=getattr(job,'parent',None)
    for _ in range(20):
        if parent is None:break
        text+='\n'+parent.feedback
        parent=parent.parent
    urls.extend(re.findall(r'https?://[^\s<>"\]\)]+',text))
    return [url.rstrip('.,，。；;') for url in urls]


def api_origins(job):
    """有认证时只向专门填写的 API 主机发送凭据，不把文档主机视为认证目标。"""
    if job.header_env or getattr(job,'credential_id',None):
        if not job.api_url:raise ValueError('使用认证时请单独填写实际 API 地址，以确定凭据接收主机。')
        from .feed_credentials import credential_urls
        return {origin(job.api_url),*(origin(url) for url in credential_urls(job))}
    return {origin(url) for url in declared_urls(job)}


def check_spec(job,spec):
    # Existing tasks inherit the KB name even if an old model transcript
    # proposes a different source name. New KBs still receive their first name.
    if getattr(job,'kb_id',None) and isinstance(spec,dict):
        from .models import KnowledgeBase
        spec={**spec,'name':KnowledgeBase.objects.values_list('name',flat=True).get(pk=job.kb_id)}
    validate_spec(spec)
    from .feed_protocol import validate_url
    validate_url(spec['url'],allow_fragment=spec.get('source_type')=='browser')
    if any(key.lower() in SENSITIVE for key in {**dict(parse_qs(urlsplit(spec['url']).query)),**spec.get('query',{})}):raise ValueError('认证信息请使用请求头环境变量，不要写入 URL 或查询参数。')
    approved=api_origins(job)
    if spec.get('source_type')=='browser' and not job.header_env and not getattr(job,'credential_id',None):
        approved.update(origin(url) for url in getattr(job,'browser_state',{}).get('observed_urls',[]))
    if origin(spec['url']) not in approved:raise ValueError('来源地址尚未确认，请先用浏览器打开用户提供或搜索到的真实网页。')
    if spec.get('header_env',{})!=job.header_env:raise ValueError('不能新增或修改用户未授权的认证环境变量。')
    from .feed_http import validate_headers
    from .feed_cron import CronSchedule
    validate_headers(spec.get('header_env',{}));CronSchedule(spec.get('cron','0 * * * *'),spec.get('timezone','Asia/Shanghai'))
    return validate_spec(with_collection_date(spec))


def sample_preview(job,spec,sample):
    spec=check_spec(job,spec)
    if sample is None:raise ValueError('尚无响应样例，请使用 probe_api 或请用户提供脱敏 JSON。')
    payload=validate_batch(build_batch(spec,sample))
    if not payload['items']:raise ValueError('样例没有记录，无法验证稳定 ID 和必需字段；请提供非空样例。')
    preview={'valid':True,'items':len(payload['items']),'deleted':len(payload['deleted']),'write_mode':spec.get('write_mode','append'),
            'preview':[{**item,'content':item['content'][:4000]} for item in payload['items'][:2]],'cursor':payload.get('cursor')}
    records=lookup(sample,spec.get('records_path',''))
    if spec.get('single_record'):records=[records]
    coverage=[]
    for field in spec.get('body_fields',[]):
        present=0;exempt=0
        for record in records:
            try:value=field_value(record,field,{'timezone':spec.get('timezone','Asia/Shanghai'),'records':records})
            except (KeyError,ValueError,IndexError,TypeError):value=None
            if value is None and field_exempt(record,field):exempt+=1;continue
            if value not in (None,'',[]):present+=1
        coverage.append({'label':field['label'],'present':present,'total':len(records)-exempt,'not_applicable':exempt})
    preview['field_coverage']=coverage
    if spec.get('source_type','api')=='api':
        absent=object()
        unmapped=[field['label']+' ('+field['path']+')'
            for field in spec['body_fields']
            if field.get('required',True) and isinstance(field.get('path'),str)
            and all(lookup(record,field['path'],absent) is absent for record in records)]
        if spec.get('title_path') and all(lookup(record,spec['title_path'],absent) is absent for record in records):
            unmapped.append('title_path ('+spec['title_path']+')')
        for field in spec['body_fields']:
            if field.get('format')!='table':continue
            tables=[lookup(record,field['path'],None) for record in records]
            rows=[row for table in tables if isinstance(table,list) for row in table if isinstance(row,dict)]
            if rows:
                unmapped.extend(field['label']+'.'+column['label']+' ('+column['path']+')'
                    for column in field['columns']
                    if all(lookup(row,column['path'],absent) is absent for row in rows))
        if unmapped:
            raise ValueError('API 真实样例中所有记录都不存在已配置字段路径：'+('、'.join(unmapped))+'. 请按 probe_api 的 item_fields 或 inspect_api_sample 修正路径；确属可缺失业务字段才明确设 required=false。')
    missing=[field['label'] for field in coverage if field['present']<field['total']]
    issues=[{'title':item['title'],'message':item.get('metadata',{}).get('collection_warning')} for item in payload['items'] if item.get('metadata',{}).get('collection_warning')]
    preview['warnings']=issues
    if issues:preview['warning']=str(len(issues))+'条记录未完全按规则解析，已保留原文和采集提示，可直接审核或继续对话调整。'
    elif missing:preview['warning']='以下字段存在缺失：'+('、'.join(missing))+'。已保留实际内容，请查看样例。'
    return preview


def document_text(url,focus='',*,private=False):
    status,headers,body=read_url(url,allow_private=private)
    if status!=200:raise ValueError('文档返回 HTTP '+str(status))
    text=body.decode('utf-8',errors='replace')
    if 'html' in headers.get('content-type',''):
        parser=PageText();parser.feed(text);text=''.join(parser.parts)
    text=re.sub(r'\n{3,}','\n\n',text);start=max(0,text.casefold().find(focus.casefold())-1000) if focus else 0
    return {'url':url,'text':text[start:start+10000],'truncated':len(text)>start+10000}
