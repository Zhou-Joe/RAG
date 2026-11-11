from __future__ import annotations

"""连接器的可信 JSON 转换运行时；仅依赖标准库，可随脚本独立分发。"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit,urlencode,urlunsplit,parse_qsl
from urllib.request import Request,build_opener,HTTPRedirectHandler

SCHEMA='fos-rag-feed/v1'
MAX_ITEMS=50_000
MAX_BYTES=500_000_000
MISSING=object()
_SCALE={'seconds':1,'minutes':60,'hours':3600,'days':86400}


def with_collection_date(spec):
    """Generated connectors always use the collector's local date, never a website field."""
    fields=[field for field in spec['body_fields'] if field['label']!='采集日期']
    return {**spec,'body_fields':fields+[{'label':'采集日期','source':'today'}]}


def lookup(data,path,default=MISSING):
    if path=='':return data
    parts=[part.replace('~1','/').replace('~0','~') for part in path[1:].split('/')] if path.startswith('/') else path.split('.')
    try:
        for part in parts:data=data[int(part)] if isinstance(data,list) and part.isdecimal() else data[part]
        return data
    except (KeyError,IndexError,TypeError,ValueError):
        if default is MISSING:raise ValueError('API 响应缺少字段：'+path) from None
        return default


def _moment(value,kind):
    """把单点时间换算成相对秒；hhmm 只支持一天内的 HH:MM（可选 AM/PM）。"""
    if value is None or value=='':return None
    if kind=='hhmm':
        match=re.fullmatch(r'\s*(\d{1,2}):(\d{2})(?:\s*([APap])\.?[Mm]?\.?)?\s*',str(value))
        if not match:raise ValueError('时间格式无效（应为 HH:MM）：'+str(value))
        hour,minute,meridiem=int(match.group(1)),int(match.group(2)),match.group(3)
        if meridiem:
            if meridiem.upper()=='P' and hour<12:hour+=12
            elif meridiem.upper()=='A' and hour==12:hour=0
        if hour>23 or minute>59:raise ValueError('时间超出一天范围：'+str(value))
        return hour*3600+minute*60
    try:
        if kind=='unix_millis':return float(value)/1000
        if kind=='unix_seconds':return float(value)
    except (TypeError,ValueError):raise ValueError('时间戳不是数字：'+str(value)) from None
    text=str(value).strip()
    if text.endswith(('Z','z')):text=text[:-1]+'+00:00'
    try:stamp=datetime.fromisoformat(text)
    except ValueError:raise ValueError('时间不是有效的 ISO 8601：'+str(value)) from None
    return stamp.timestamp() if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc).timestamp()


def duration_value(start,end,computed):
    """按 computed 配置计算两个时间点的差值；任一端缺失返回 None，格式错误抛出。"""
    begin=_moment(start,computed.get('format','iso8601'));finish=_moment(end,computed.get('format','iso8601'))
    if begin is None or finish is None:return None
    delta=finish-begin
    if delta<0 and computed.get('format','iso8601')=='hhmm':delta+=86400  # 跨零点，如 22:00 开始、06:00 结束
    value=delta/_SCALE[computed.get('unit','minutes')]
    return int(value) if float(value).is_integer() else round(value,2)


def field_value(record,field,context=None):
    """取单个配置字段的值：路径字段直接提取，computed 字段算时间差；缺失返回 None。"""
    if field.get('source') in ('today','now'):
        context=context or {};stamp=context.get('collected_at') or datetime.now(ZoneInfo(context.get('timezone','Asia/Shanghai')))
        return stamp.date().isoformat() if field['source']=='today' else stamp.isoformat()
    computed=field.get('computed')
    if computed is None:return lookup(record,field['path'],None)
    try:from .connector_calculations import evaluate_computed
    except ImportError:from connector_calculations import evaluate_computed
    return evaluate_computed(record,computed,context)


def field_exempt(record,field):
    """仅在原始状态明确符合声明的例外时，允许不适用字段留空。"""
    rule=field.get('required_unless')
    if not rule:return False
    value=lookup(record,rule['path'],None)
    return isinstance(value,str) and bool(re.search(rule['regex'],value,re.I))


def validate_spec(spec):
    allowed={'name','url','query','records_path','single_record','id_paths','title_path','title_prefix','body_fields','metadata_paths','ingest_mode',
             'updated_at_path','updated_at_format','source_url_path','deleted_path','next_cursor_path','cursor_param','cron','timezone','header_env',
             'source_type','browser','on_error','write_mode'}
    if not isinstance(spec,dict) or not spec.keys()<=allowed:raise ValueError('连接器配置包含不支持的字段。')
    if not isinstance(spec.get('name'),str) or not spec['name'].strip() or len(spec['name'])>100:raise ValueError('请提供连接器名称。')
    if spec.get('source_type','api') not in ('api','browser'):raise ValueError('source_type 仅支持 api 或 browser。')
    if spec.get('ingest_mode','tracker') not in ('tracker','documents'):raise ValueError('ingest_mode 仅支持 tracker（结构化记录）或 documents（每条记录一份文档）。')
    if spec.get('write_mode','append') not in ('append','upsert'):raise ValueError('write_mode 仅支持 append（追加保留历史）或 upsert（覆盖相同 ID）。')
    if spec.get('source_type')=='browser':
        try:from .browser_runtime import validate_plan
        except ImportError:from browser_runtime import validate_plan
        validate_plan(spec.get('browser'))
    url=urlsplit(spec.get('url',''))
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or (url.fragment and spec.get('source_type')!='browser'):raise ValueError('请提供明确的 HTTP(S) URL；网页模式支持完整的 # 路由，API模式不能带片段。')
    if spec.get('on_error','raw') not in ('raw','fail'):raise ValueError('on_error仅支持raw（保留原文）或fail。')
    if not isinstance(spec.get('records_path',''),str):raise ValueError('records_path 必须是 JSON 路径。')
    if 'single_record' in spec and not isinstance(spec['single_record'],bool):raise ValueError('single_record 必须是布尔值。')
    ids=spec.get('id_paths');fields=spec.get('body_fields')
    if not isinstance(ids,list) or not 1<=len(ids)<=4 or any(not isinstance(path,str) or not path for path in ids):raise ValueError('id_paths 必须明确指定1至4个稳定 ID 字段。')
    if not isinstance(fields,list) or not 1<=len(fields)<=31:raise ValueError('请指定1至30个业务字段及采集日期。')
    for field in fields:
        if not isinstance(field,dict) or not field.keys()<={'label','path','unit','format','required','columns','computed','required_unless','source'} or not isinstance(field.get('label'),str) or not field['label']:raise ValueError('正文字段必须包含 label，并指定 path、source 或 computed。')
        rule=field.get('required_unless')
        if rule is not None:
            if not isinstance(rule,dict) or set(rule)!={'path','regex'} or not isinstance(rule['path'],str) or not 1<=len(rule['path'])<=1000 or not isinstance(rule['regex'],str) or not 1<=len(rule['regex'])<=300:raise ValueError('required_unless 必须指定实际状态字段 path 和有限 regex。')
            if re.search(r'\\[1-9]|\(\?[=!<]|\([^)]*[+*{][^)]*\)[+*{]',rule['regex']):raise ValueError('状态条件包含不支持的复杂正则。')
            try:re.compile(rule['regex'])
            except re.error as exc:raise ValueError('required_unless 正则无效。') from exc
        computed=field.get('computed');by_path=isinstance(field.get('path'),str)
        source=field.get('source')
        if source not in (None,'today','now'):raise ValueError('正文source仅支持today（采集日期）或now（采集时间）。')
        if (computed is not None and (by_path or source)) or (computed is None and not by_path and not source):raise ValueError('正文字段需指定path、source或computed，computed不能与其他取值方式混用。')
        if computed is not None:
            try:from .connector_calculations import validate_computed
            except ImportError:from connector_calculations import validate_computed
            validate_computed(computed)
            if field.get('format','auto')!='auto':raise ValueError('computed 字段不支持 table 等特殊格式。')
        if field.get('format','auto') not in ('auto','text','json','table'):raise ValueError('不支持的字段格式。')
        if len(field['label'])>200 or (by_path and len(field['path'])>1000) or not isinstance(field.get('unit',''),str) or len(field.get('unit',''))>80:raise ValueError('字段名称、路径或单位过长或格式无效。')
        if 'required' in field and not isinstance(field['required'],bool):raise ValueError('required 必须是布尔值。')
        if field.get('format')=='table':
            columns=field.get('columns')
            if not isinstance(columns,list) or not 1<=len(columns)<=15 or any(not isinstance(c,dict) or set(c)!={'label','path'} or not isinstance(c['label'],str) or not isinstance(c['path'],str) for c in columns):raise ValueError('表格字段必须指定 columns 的 label/path。')
    query=spec.get('query',{});headers=spec.get('header_env',{})
    if sum(field['label']!='采集日期' for field in fields)>30:raise ValueError('业务字段最多30个，另保留采集日期。')
    if not isinstance(query,dict) or len(query)>20 or any(not isinstance(k,str) or not isinstance(v,(str,int,float,bool)) for k,v in query.items()):raise ValueError('query 仅支持最多20个简单查询参数。')
    if not isinstance(headers,dict) or any(not isinstance(k,str) or not isinstance(v,str) or not re.fullmatch('FOS_FEED_[A-Z0-9_]+',v) for k,v in headers.items()):raise ValueError('请求头只能引用 FOS_FEED_ 环境变量。')
    for key in ('metadata_paths',):
        value=spec.get(key,{})
        if not isinstance(value,dict) or len(value)>20 or any(not isinstance(k,str) or not isinstance(v,str) for k,v in value.items()):raise ValueError(key+' 格式无效。')
    for key in ('title_path','title_prefix','updated_at_path','source_url_path','deleted_path','next_cursor_path','cursor_param','cron','timezone'):
        if key in spec and (not isinstance(spec[key],str) or len(spec[key])>2000):raise ValueError(key+' 必须是短字符串。')
    if spec.get('updated_at_format','iso8601') not in ('iso8601','unix_seconds','unix_millis'):raise ValueError('不支持的时间戳格式。')
    if len(json.dumps(spec,ensure_ascii=False,allow_nan=False))>40000:raise ValueError('连接器配置过大。')
    return spec


def request_url(spec,cursor=''):
    parts=urlsplit(spec['url']);query=dict(parse_qsl(parts.query,keep_blank_values=True));query.update(spec.get('query',{}))
    if cursor and spec.get('cursor_param'):query[spec['cursor_param']]=cursor
    return urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(query),''))


def _value(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True) if isinstance(value,(dict,list)) else str(value)


def _cell(value):return _value(value).replace('|','\\|').replace('\n',' ').replace('\r',' ')


def build_batch(spec,data):
    validate_spec(spec)
    records=lookup(data,spec.get('records_path',''))
    if spec.get('single_record',False):records=[records]
    if not isinstance(records,list):raise ValueError('records_path 没有定位到数组；单对象接口请使用 single_record=true。')
    if len(records)>MAX_ITEMS:raise ValueError('API 返回超过50000条，请在 query 中设置分页大小或时间范围。')
    items=[];tolerant=spec.get('on_error','raw')=='raw'
    document_mode=spec.get('ingest_mode','tracker')=='documents'
    context={'timezone':spec.get('timezone','Asia/Shanghai'),'collected_at':datetime.now(ZoneInfo(spec.get('timezone','Asia/Shanghai'))),'records':records}
    if spec.get('source_type')=='browser' and isinstance(data,dict) and data.get('__fos_collected_at'):
        stamp=datetime.fromisoformat(data['__fos_collected_at'])
        if stamp.tzinfo:context['collected_at']=stamp.astimezone(ZoneInfo(context['timezone']))
    for record in records:
        identities=[lookup(record,path) for path in spec['id_paths']]
        if any(not isinstance(value,(str,int)) or isinstance(value,bool) or not str(value).strip() for value in identities):raise ValueError('稳定 ID 字段为空或不是字符串/整数。')
        key=str(identities[0]) if len(identities)==1 else json.dumps(identities,ensure_ascii=False,separators=(',',':'))
        if len(key)>200:key='id-'+hashlib.sha256(key.encode()).hexdigest()
        title=lookup(record,spec['title_path'],None) if spec.get('title_path') else None
        title=title or spec.get('title_prefix',spec['name'])+' '+key
        title=str(title).replace('\n',' ').replace('\r',' ')[:200]
        paragraphs=[];field_values={};capture=record.get('__fos_capture',{}) if isinstance(record,dict) else {}
        warnings=list(capture.get('warnings',[]));raws={}
        for field in spec['body_fields']:
            # The system collection date belongs to the fetch, not the article.
            # Keeping it in the document body creates a new file every day even
            # when the source text has not changed.
            if document_mode and field['label']=='采集日期' and field.get('source')=='today':
                continue
            problem=''
            try:value=field_value(record,field,context)
            except (ValueError,TypeError,OverflowError) as exc:
                if not tolerant:raise
                value=None;problem=str(exc)
            if value is None or tolerant and value in ('',[]):
                if field_exempt(record,field):
                    field_values[field['label']]=''
                    continue
                if not tolerant:
                    if field.get('required',True):raise ValueError('记录「'+title+'」采集字段缺失：'+(field.get('path') or field['label']))
                    continue
                computed=field.get('computed') or {}
                if computed:
                    try:from .connector_calculations import calculation_inputs
                    except ImportError:from connector_calculations import calculation_inputs
                    raw=calculation_inputs(record,computed)
                else:raw=capture.get('raw',{}).get(field.get('path')) or capture.get('text') or record
                raw_limit=(min(24000,max(1000,80000//len(spec['body_fields']))) if document_mode
                           else min(360,max(40,4000//len(spec['body_fields']))))
                value=_value(raw)[:raw_limit]
                warnings.append(field['label']+'：'+(problem or '未能按原规则提取，保留抓到的原文'))
                raws[field['label']]=value
            label=field['label'];kind=field.get('format','auto')
            if kind=='table':
                if not isinstance(value,list):
                    if not tolerant:raise ValueError('表格字段不是数组：'+field['path'])
                    warnings.append(label+'：表格结构变化，保留原值');field_values[label]=_value(value)[:1000];paragraphs.append('## '+label+'\n\n'+_value(value));continue
                columns=field['columns'];lines=['| '+' | '.join(_cell(c['label']) for c in columns)+' |','| '+' | '.join('---' for _ in columns)+' |']
                for row in value[:100]:lines.append('| '+' | '.join(_cell(lookup(row,c['path'],'')) if tolerant else _cell(lookup(row,c['path'])) for c in columns)+' |')
                if tolerant and any(lookup(row,c['path'],None) is None for row in value for c in columns):
                    warnings.append(label+'：明细列发生变化，保留原始明细');raws[label]=value
                if len(value)>100:raise ValueError('明细表超过100行，请缩小 API 查询范围。')
                paragraphs.append('## '+label+'\n\n'+'\n'.join(lines))
                field_values[label]='\n'.join(lines)
            else:
                paragraphs.append('## '+label+'\n\n'+_value(value)+((' '+field['unit']) if field.get('unit') else ''))
                field_values[label]=_value(value) if isinstance(value,(dict,list)) else value
        item={'id':key,'title':title,'content':'\n\n'.join(paragraphs),'format':'markdown',
              'metadata':{name:lookup(record,path,'') if tolerant else lookup(record,path) for name,path in spec.get('metadata_paths',{}).items()}}
        if spec.get('source_url_path'):
            url=lookup(record,spec['source_url_path'],None) if tolerant else lookup(record,spec['source_url_path'])
            parts=urlsplit(url) if isinstance(url,str) else None
            if tolerant and (not parts or parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password):
                warnings.append('来源链接缺失或格式变化，使用采集页面地址');url=spec['url']
            item['source_url']=url
        if spec.get('updated_at_path'):
            stamp=lookup(record,spec['updated_at_path'],None) if tolerant else lookup(record,spec['updated_at_path'])
            try:
                kind=spec.get('updated_at_format','iso8601')
                if kind!='iso8601':stamp=datetime.fromtimestamp(float(stamp)/(1000 if kind=='unix_millis' else 1),timezone.utc).isoformat()
                if tolerant and (not isinstance(stamp,str) or not datetime.fromisoformat(stamp.replace('Z','+00:00')).tzinfo):raise ValueError('时间缺失或没有时区')
                item['updated_at']=stamp
            except (ValueError,TypeError,OverflowError,OSError):
                if not tolerant:raise
                warnings.append('来源更新时间格式变化，使用本次采集时间');item['updated_at']=context['collected_at'].isoformat()
        if warnings:
            warning='；'.join(dict.fromkeys(str(value) for value in warnings))[:1800]
            evidence=json.dumps(raws or capture.get('raw') or record,ensure_ascii=False,default=str)[:1800]
            field_values['采集提示']=warning;field_values['原始内容']=evidence
            item['metadata']['collection_warning']=warning
            item['content']+='\n\n## 采集提示\n\n'+warning+'\n\n## 原始内容\n\n'+evidence
        if not item['content'].strip():
            item['content']=json.dumps(record,ensure_ascii=False,default=str)[:100000 if document_mode else 5000]
        # Feed protocol's structured fields cap is 20K. A document's long raw
        # text lives in content and must not be copied into tracker fields.
        if field_values and not document_mode:item['fields']=field_values
        if len(item['content'])>100000:raise ValueError('生成的单条报告超过100000字符。')
        items.append(item)
    deleted=lookup(data,spec['deleted_path']) if spec.get('deleted_path') else []
    if not isinstance(deleted,list) or any(not isinstance(key,(str,int)) or isinstance(key,bool) for key in deleted):raise ValueError('deleted_path 只能指向显式删除 ID 的数组。')
    if len(items)+len(deleted)>MAX_ITEMS:raise ValueError('单批写入与删除总数不能超过50000。')
    payload={'schema':SCHEMA,'items':items,'deleted':[str(key) for key in deleted]}
    if spec.get('next_cursor_path'):
        cursor=lookup(data,spec['next_cursor_path'],None)
        if cursor is not None:
            if not isinstance(cursor,(str,int)) or isinstance(cursor,bool):raise ValueError('游标必须是字符串或整数。')
            payload['cursor']=str(cursor)
    payload['batch_id']='connector-'+hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return payload


def main(spec):
    """独立脚本入口：预览或推送，确认服务器发布成功后才保存本地游标。"""
    parser=argparse.ArgumentParser();parser.add_argument('--push',action='store_true');parser.add_argument('--preview',action='store_true');parser.add_argument('--state-file',default='.connector-state.json');parser.add_argument('--allow-private',action='store_true')
    args=parser.parse_args();validate_spec(spec);path=Path(args.state_file)
    state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    headers={}
    for key,env in spec.get('header_env',{}).items():
        if not os.environ.get(env):raise ValueError('Missing environment variable: '+env)
        headers[key]=os.environ[env]
    if spec.get('source_type')=='browser':
        from browser_runtime import BrowserSession
        browser=BrowserSession(approved_urls=[spec['url']],headers=headers,credential_origin=spec['url'],allow_private=args.allow_private,
            login={'username':os.environ.get('FOS_FEED_LOGIN_USERNAME',''),'password':os.environ.get('FOS_FEED_LOGIN_PASSWORD','')},timezone=spec.get('timezone','Asia/Shanghai'))
        try:data=browser.call('collect',spec=spec)
        finally:browser.close()
    else:
        with build_opener(NoRedirect()).open(Request(request_url(spec,state.get('cursor','')),headers={'Accept':'application/json','Accept-Encoding':'identity',**headers}),timeout=30) as response:raw=response.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('API response exceeds 500MB.')
        data=json.loads(raw)
    payload=build_batch(spec,data)
    if args.push:
        from feed_client import push
        result=push(os.environ['FOS_FEED_INGEST_URL'],os.environ['FOS_FEED_PUSH_TOKEN'],payload)
        if result['run']['status']!='completed':raise RuntimeError('Run not published; checkpoint unchanged.')
        if 'cursor' in payload:
            temp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp');temp.write_text(json.dumps({'cursor':payload['cursor']}),encoding='utf-8');os.replace(temp,path)
        print(json.dumps(result,ensure_ascii=False))
    else:print(json.dumps(payload,ensure_ascii=False))
