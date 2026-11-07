"""Reviewed report rules over one feed's records; no model-generated executable code."""
from __future__ import annotations

import hashlib
import io
import json
import secrets
import subprocess
import sys
from datetime import timedelta
from html import escape
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import access
from .feed_cron import CronSchedule
from .models import FeedReportPlan, FeedReportDelivery, KnowledgeFeed, TrackerRow

MAX_ROWS = 5000

def workflow_url(value):
    """Only Microsoft HTTPS workflow invocation endpoints, never interactive share links."""
    if not isinstance(value,str) or len(value)>8000:raise ValueError('请提供有效的工作流 HTTP 触发地址。')
    try:
        parsed=urlsplit(value);host=(parsed.hostname or '').lower();port=parsed.port
    except ValueError:raise ValueError('工作流地址无效。') from None
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.fragment or port not in (None,443)
        or not any(host.endswith(suffix) for suffix in ('.environment.api.powerplatform.com','.logic.azure.com','.logic.azure.cn'))
        or '/triggers/' not in parsed.path or not parsed.path.endswith('/invoke') or not parsed.query):
        raise ValueError('请粘贴 Microsoft 工作流的 HTTP 触发地址；RunFromLink/分享链接不能用于定时发送。')
    return value

def public_base(value):
    if not isinstance(value,str) or len(value)>500:raise ValueError('请填写报告站点的公开地址。')
    value=value.strip()
    try:parsed=urlsplit(value);parsed.port
    except ValueError:raise ValueError('报告站点地址无效。') from None
    if any(ord(char)<32 for char in value) or '\\' in value:raise ValueError('报告站点地址无效。')
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('请填写报告站点的完整公开地址，例如 https://reports.example.com。')
    import ipaddress
    host=parsed.hostname.rstrip('.').lower()
    try:address=ipaddress.ip_address(host);local=address.is_loopback or address.is_unspecified or bool(getattr(address,'ipv4_mapped',None) and (address.ipv4_mapped.is_loopback or address.ipv4_mapped.is_unspecified))
    except ValueError:local=host=='localhost' or host.endswith('.localhost')
    if local:raise ValueError('收件人无法打开本机地址，请使用本站的实际对外地址。')
    return value.rstrip('/')


def site_address(config=None):
    """Resolve the app address for both interactive setup and background delivery."""
    config=config or {}
    saved=config.get('public_base','')
    source=config.get('public_base_source','manual')
    canonical=getattr(settings,'FEED_REPORT_PUBLIC_BASE','')
    if saved and source=='manual':base,source=public_base(saved),'manual'
    elif canonical:base,source=public_base(canonical),'deployment'
    elif saved:base=public_base(saved)
    else:return {'available':False,'public_base':'','source':'','reason':'当前仅有本机地址，无法推断反向代理或端口映射的公开入口。请管理员配置站点公开地址，或从实际对外地址打开本站。'}
    return {'available':True,'public_base':base,'source':source,'reason':''}


def remember_request_site(user,feed,request):
    """Capture Django's validated external origin, never arbitrary forwarded headers."""
    from django.core.exceptions import DisallowedHost
    from .feed_credentials import _cipher
    require_feed(user,feed)
    if getattr(settings,'FEED_REPORT_PUBLIC_BASE',''):return
    try:
        base=public_base(request.scheme+'://'+request.get_host()+request.META.get('SCRIPT_NAME','').rstrip('/'))
    except (ValueError,DisallowedHost):return
    with transaction.atomic():
        plan,_=FeedReportPlan.objects.select_for_update().get_or_create(feed=feed,defaults={'created_by':user})
        config=destination(plan)
        if config.get('public_base') and config.get('public_base_source','manual')=='manual':return
        if config.get('public_base')==base and config.get('public_base_source')=='request':return
        config.update(public_base=base,public_base_source='request')
        plan.destination=_cipher(create=True).encrypt(json.dumps(config).encode()).decode()
        plan.save(update_fields=['destination','updated_at'])


def delivery_context(job):
    feed=source_feed(job,required=False)
    return summary(feed) if feed else {}


def require_feed(user,feed):
    if not user or not user.is_active or not access.is_manager(user) or not access.can_manage_kb(user,feed.kb):
        raise ValueError('没有此自动任务的管理权限。')

def source_feed(job,*,required=True):
    seen=set();candidate=job
    while candidate and candidate.pk not in seen and len(seen)<100:
        seen.add(candidate.pk)
        if candidate.user_id!=job.user_id:raise ValueError('原任务不属于当前账号。')
        if candidate.feed_id:
            feed=KnowledgeFeed.objects.select_related('kb','created_by').get(pk=candidate.feed_id)
            require_feed(job.user,feed);return feed
        candidate=candidate.parent
    if required:raise ValueError('请先启用采集任务，再在同一对话中配置报告。')
    return None

def destination(plan):
    from .feed_credentials import _cipher
    try:return json.loads(_cipher().decrypt(plan.destination.encode())) if plan.destination else {}
    except Exception:raise ValueError('报告发送设置无法解密，请重新保存工作流地址。') from None

def workflow_bindings(config):
    """Capability URLs stay encrypted and are bound to an explicit delivery type."""
    bindings={key:value for key,value in config.get('workflows',{}).items() if key in ('email','teams') and isinstance(value,str) and value}
    kind=config.get('kind');url=config.get('url')
    if kind in ('email','teams') and url and config.get('url_kind',kind)==kind:bindings.setdefault(kind,url)
    return bindings


def selected_workflow(config):
    kind=config.get('kind');url=workflow_bindings(config).get(kind)
    if not url:raise ValueError('请提供'+('Teams频道' if kind=='teams' else '邮件')+'工作流的HTTP触发链接；不能复用其他类型的链接。')
    return workflow_url(url)


def configure(user,feed,data):
    from .feed_credentials import _cipher
    require_feed(user,feed)
    plan,_=FeedReportPlan.objects.get_or_create(feed=feed,defaults={'created_by':user})
    old=destination(plan);kind=data.get('kind',old.get('kind','email'))
    if kind not in ('email','teams'):raise ValueError('请选择邮件或 Teams 卡片。')
    bindings=workflow_bindings(old)
    supplied=data.get('url') or old.get('pending_url')
    if supplied:bindings[kind]=workflow_url(supplied)
    url=bindings.get(kind,'')
    address=site_address({'public_base':data['public_base']} if data.get('public_base') else old)
    recipient=data.get('recipient',old.get('recipient','')).strip()
    if kind=='email' and recipient:
        try:validate_email(recipient)
        except ValidationError:raise ValueError('请填写有效的收件人邮箱。') from None
        if any(c in recipient for c in '\r\n'):raise ValueError('收件人地址不能换行。')
    config={'kind':kind,'url':url,'url_kind':kind if url else '', 'workflows':bindings,
        'public_base':address['public_base'],'public_base_source':address['source'],'recipient':recipient}
    plan.destination=_cipher(create=True).encrypt(json.dumps(config).encode()).decode()
    plan.enabled=False;plan.created_by=user;plan.save()
    return plan


def summary(feed):
    plan=FeedReportPlan.objects.filter(feed=feed).first()
    data=destination(plan) if plan else {};address=site_address(data)
    last=plan.deliveries.exclude(status='preview').order_by('-created_at').first() if plan else None
    bindings=workflow_bindings(data);selected=bindings.get(data.get('kind'),'')
    missing=[]
    if not selected and not (not data.get('kind') and data.get('pending_url')):missing.append('workflow_url')
    if not data.get('kind'):missing.append('kind')
    if not address['available']:missing.append('public_base')
    if data.get('kind')=='email' and not data.get('recipient'):missing.append('recipient')
    return {'configured':not missing,'enabled':bool(plan and plan.enabled),'workflow_saved':bool(selected),'workflow_type':data.get('kind') if selected else '',
        'saved_workflow_types':sorted(bindings),'untyped_workflow_saved':bool(data.get('pending_url')),'missing':missing,'kind':data.get('kind'),
        'recipient':data.get('recipient','') if data.get('kind')=='email' else '','public_base':address['public_base'],'public_base_source':address['source'],
        'site_address_available':address['available'],'site_address_note':address['reason'],
        'title':plan.spec.get('title','') if plan else '',
        'schedule':plan.spec.get('schedule',{'mode':plan.schedule_mode,'cron':plan.cron,'timezone':plan.timezone}) if plan else {},
        'next_run_at':plan.next_run_at.isoformat() if plan and plan.next_run_at else None, 'last_status':last.status if last else '',
        'last_error':last.error if last else ''}


def available(feed):
    from django.db.models import Count,Min,Max
    qs=feed.tracker_rows.filter(status='done')
    fields={'_collected_at','_source_id','_record'}
    profiles={}
    for values in qs.values_list('values',flat=True).iterator(chunk_size=500):
        fields.update(values)
        for name,value in values.items():
            profile=profiles.setdefault(name,{'types':{},'samples':[]});kind=type(value).__name__
            profile['types'][kind]=profile['types'].get(kind,0)+1
            sample=value if type(value) in (int,float,bool,type(None)) else str(value)[:160]
            if sample not in profile['samples'] and len(profile['samples'])<5:profile['samples'].append(sample)
    stats=qs.aggregate(rows=Count('pk'),first_collected_at=Min('updated_at'),last_collected_at=Max('updated_at'))
    return {'fields':sorted(fields),'field_profiles':profiles,'sample':list(qs.order_by('-updated_at').values_list('values',flat=True)[:5]),
        'dataset':{key:value.isoformat() if hasattr(value,'isoformat') else value for key,value in stats.items()},
        'write_mode':feed.write_mode,'report':summary(feed),'previous_report':getattr(getattr(feed,'report_plan',None),'spec',{}),
        'history_note':'仅能使用当前仍保存的记录；覆盖更新模式不保留已覆盖的旧版本。' if feed.write_mode=='upsert' else '可按范围处理此任务保存的全部历史快照。'}


def report_schedule(value,feed):
    from .feed_cron import CronSchedule
    if not isinstance(value,dict) or value.get('mode') not in ('cron','manual','after_collection'):
        raise ValueError('请确认报告何时发送：独立的发送时间和频率，或仅按需发送；不要默认沿用采集计划。')
    mode=value['mode'];zone=value.get('timezone') or feed.timezone
    if mode=='cron':
        cron=value.get('cron','');schedule=CronSchedule(cron,zone);schedule.next_after(timezone.now())
        return {'mode':'cron','cron':cron.strip(),'timezone':zone}
    try:ZoneInfo(zone)
    except (ValueError,TypeError,KeyError):raise ValueError('报告时区无效。') from None
    return {'mode':mode,'timezone':zone}


def normalize(spec,feed,*,legacy=False):
    if not isinstance(spec,dict):raise ValueError('报告配置必须是对象。')
    title=spec.get('title','').strip()
    if not title or len(title)>150:raise ValueError('报告标题需为1至150字。')
    columns=spec.get('columns');fields=set(available(feed)['fields'])
    if not isinstance(columns,list) or not 1<=len(columns)<=30:raise ValueError('请选择1至30个报告字段。')
    clean=[];labels=set()
    from .connector_calculations import validate_computed
    for column in columns:
        if not isinstance(column,dict):raise ValueError('报告字段格式无效。')
        label=column.get('label','').strip()
        if not label or len(label)>80 or label in labels:raise ValueError('报告列名需有效且不重复。')
        labels.add(label)
        if 'computed' in column:clean.append({'label':label,'computed':validate_computed(column['computed'])})
        else:
            field=column.get('field')
            if field not in fields:raise ValueError('报告引用了未知字段：'+str(field))
            clean.append({'label':label,'field':field})
    scope=spec.get('range',{'mode':'latest'} if legacy else None)
    if not isinstance(scope,dict) or scope.get('mode') not in ('latest','today','yesterday','days','dates','last_week','last_month','all'):
        raise ValueError('请确认报告数据范围：全部历史、指定日期、最近若干天、上周、上月或最新一次。')
    scope={**scope,'field':scope.get('field') or '_collected_at'}
    if scope['field'] not in fields:raise ValueError('报告日期字段不存在。')
    if scope['mode']=='days' and (type(scope.get('days')) is not int or not 1<=scope['days']<=3660):raise ValueError('历史范围需为1至3660天。')
    if 'include_today' in scope and type(scope['include_today']) is not bool:raise ValueError('是否包含今天需为布尔值。')
    if scope['mode']=='dates':
        from datetime import date
        try:
            start=date.fromisoformat(scope['start']);end=date.fromisoformat(scope['end'])
            if start>end:raise ValueError()
        except (KeyError,TypeError,ValueError):raise ValueError('请填写有效的开始、结束日期。') from None
    filters=spec.get('filters',[])
    if not isinstance(filters,list) or len(filters)>20:raise ValueError('筛选条件过多。')
    for condition in filters:
        if not isinstance(condition,dict) or condition.get('field') not in fields or condition.get('op') not in ('eq','contains','gte','lte') or not isinstance(condition.get('value'),(str,int,float)):
            raise ValueError('报告筛选条件无效。')
    group=spec.get('group_by',[]);aggregates=spec.get('aggregates',[])
    if not isinstance(group,list) or len(group)>5 or len(set(group))!=len(group) or any(label not in labels for label in group):raise ValueError('分组需引用不重复的报告列名。')
    if not isinstance(aggregates,list) or len(aggregates)>20:raise ValueError('汇总格式无效。')
    for item in aggregates:
        if not isinstance(item,dict) or item.get('field') not in labels or item.get('op') not in ('count','sum','avg','min','max') or not isinstance(item.get('label'),str) or not 1<=len(item['label'])<=80 or item['label'] in group:
            raise ValueError('汇总需包含有效的字段、运算和唯一名称。')
    if len({a['label'] for a in aggregates})!=len(aggregates) or group and not aggregates:raise ValueError('分组需设置汇总计算，汇总列名不能重复。')
    schedule=report_schedule(spec.get('schedule',{'mode':'after_collection'} if legacy else None),feed)
    return {'kind':'report','title':title,'columns':clean,'range':scope,'filters':filters,'group_by':group,'aggregates':aggregates,'schedule':schedule}


def date_window(scope,at,zone):
    from datetime import date
    today=timezone.localtime(at,zone).date();mode=scope['mode']
    if mode in ('all','latest'):return None
    if mode=='dates':return date.fromisoformat(scope['start']),date.fromisoformat(scope['end'])
    if mode=='yesterday':return today-timedelta(days=1),today-timedelta(days=1)
    if mode=='last_week':
        end=today-timedelta(days=today.weekday()+1);return end-timedelta(days=6),end
    if mode=='last_month':
        end=today.replace(day=1)-timedelta(days=1);return end.replace(day=1),end
    end=today if scope.get('include_today',True) else today-timedelta(days=1)
    return end-timedelta(days=scope.get('days',1)-1),end


def table(feed,spec,*,run=None,as_of=None,allow_empty=False,diagnostics=None):
    import math
    from datetime import datetime,date,time
    from .connector_calculations import evaluate_computed,calculation_inputs,_number
    qs=feed.tracker_rows.filter(status='done').select_related('document').order_by('updated_at','pk')
    scope=spec['range'];zone=ZoneInfo(spec.get('schedule',{}).get('timezone') or feed.timezone)
    window=date_window(scope,as_of or timezone.now(),zone)
    if scope['mode']=='latest' and feed.write_mode=='append':
        latest=run or feed.runs.filter(status='completed').order_by('-finished_at','-started_at').first()
        if latest:qs=qs.filter(feed_run=latest)
        else:qs=qs.none()
    if window and scope['field']=='_collected_at':
        start,end=window
        qs=qs.filter(updated_at__gte=datetime.combine(start,time.min,zone),updated_at__lt=datetime.combine(end+timedelta(days=1),time.min,zone))
    result=[];buckets={};matched=0
    for row in qs.iterator(chunk_size=500):
        record={**row.values,'_record':row.record_name,'_source_id':row.external_id,'_collected_at':timezone.localtime(row.updated_at,zone).isoformat()}
        if window and scope['field']!='_collected_at':
            value=record.get(scope['field'])
            if value in (None,''):continue
            try:
                text=str(value);stamp=datetime.fromisoformat(text.replace('Z','+00:00')) if len(text)>10 else None
                day=(stamp.astimezone(zone) if stamp and stamp.tzinfo else stamp).date() if stamp else date.fromisoformat(text)
            except (TypeError,ValueError):raise ValueError('日期字段含非ISO日期，请选择有效的日期字段，或使用采集日期。') from None
            if not window[0]<=day<=window[1]:continue
        passed=True
        for condition in spec['filters']:
            left=str(record.get(condition['field'],''));right=str(condition['value']);op=condition['op']
            if op in ('gte','lte') and isinstance(condition['value'],(int,float)):
                try:passed=passed and (float(left)>=float(right) if op=='gte' else float(left)<=float(right))
                except ValueError:passed=False
            else:passed=passed and {'eq':lambda:left==right,'contains':lambda:right.casefold() in left.casefold(),'gte':lambda:left>=right,'lte':lambda:left<=right}[op]()
        if not passed:continue
        output={column['label']:evaluate_computed(record,column['computed']) if 'computed' in column else record.get(column['field'],'') for column in spec['columns']}
        if diagnostics is not None:
            for column in spec['columns']:
                if 'computed' not in column:continue
                state=diagnostics.setdefault(column['label'],{'converted':0,'missing':0,'examples':[]})
                value=output[column['label']];state['missing' if value is None else 'converted']+=1
                if len(state['examples'])<5:state['examples'].append({'input':calculation_inputs(record,column['computed']),'output':value})
        matched+=1
        if spec['aggregates']:
            keys=tuple(str(output.get(key,'')) for key in spec['group_by'])
            if keys not in buckets:
                if len(buckets)>=MAX_ROWS:raise ValueError('报告分组过多，请减少分组字段或日期范围。')
                buckets[keys]=[{'count':0,'sum':0,'min':None,'max':None} for _ in spec['aggregates']]
            for aggregate,state in zip(spec['aggregates'],buckets[keys]):
                value=output.get(aggregate['field'])
                if value in ('',None):continue
                state['count']+=1
                if aggregate['op']=='count':continue
                try:number=_number(value)
                except (TypeError,ValueError):raise ValueError('汇总列「'+aggregate['field']+'」包含无法直接转换的值「'+str(value)[:80]+'」。请用parse_number正则/分隔符/换算规则处理，并用preview_report_calculation验证真实样例；不能直接丢弃数字字符串。') from None
                if not math.isfinite(number):raise ValueError('汇总字段包含无效数值。')
                state['sum']+=number;state['min']=number if state['min'] is None else min(state['min'],number);state['max']=number if state['max'] is None else max(state['max'],number)
        else:
            if len(result)>=MAX_ROWS:raise ValueError('原始明细报告超过5000条，请缩小范围，或改用汇总报告处理全部历史。')
            result.append(output)
    if not matched and not allow_empty:raise ValueError('所选范围没有可报告的数据。')
    if spec['aggregates']:
        for keys,states in buckets.items():
            output=dict(zip(spec['group_by'],keys))
            for aggregate,state in zip(spec['aggregates'],states):
                op=aggregate['op'];count=state['count']
                output[aggregate['label']]=state['sum']/count if op=='avg' and count else state[op] if op=='count' or count else ''
            result.append(output)
    headers=spec['group_by']+[a['label'] for a in spec['aggregates']] if spec['aggregates'] else [c['label'] for c in spec['columns']]
    return headers,result


def workbook(title,headers,rows,audit=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font,PatternFill,Alignment
    from openpyxl.utils import get_column_letter
    if sum(len(str(value)) for row in rows for value in row.values())>2_000_000:raise ValueError('报告文本超过大小限制，请减少字段或日期范围。')
    book=Workbook();sheet=book.active;sheet.title='Report';sheet.append(headers)
    for row in rows:
        sheet.append([row.get(header,'') if not isinstance(row.get(header,''),(dict,list)) else json.dumps(row[header],ensure_ascii=False) for header in headers])
    if audit:
        quality=book.create_sheet('Transformations');quality.append(['Field','Valid','Missing / unparsed','Original input sample','Converted sample'])
        for name,state in audit.items():
            for sample in state['examples'] or [{'input':{},'output':None}]:
                value=sample['output'];value=json.dumps(value,ensure_ascii=False) if isinstance(value,(list,dict)) else value
                quality.append([name,state['converted'],state['missing'],json.dumps(sample['input'],ensure_ascii=False)[:2000],value])
        quality.freeze_panes='A2'
        for row in quality:
            for cell in row:
                if isinstance(cell.value,str):cell.data_type='s'
        for column in ('A','D','E'):quality.column_dimensions[column].width=35
    for row in sheet:
        for cell in row:
            if isinstance(cell.value,str):cell.data_type='s' # Treat untrusted source text as text, never a formula.
            cell.alignment=Alignment(vertical='top',wrap_text=True)
    for cell in sheet[1]:cell.fill=PatternFill('solid',fgColor='0052D9');cell.font=Font(color='FFFFFF',bold=True)
    sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
    for i,header in enumerate(headers,1):sheet.column_dimensions[get_column_letter(i)].width=min(45,max(16,len(header)*2+4))
    sheet.row_dimensions[1].height=28;book.properties.title=title
    stream=io.BytesIO();book.save(stream);return stream.getvalue()

def preview_time(spec):
    return timezone.now()


def preview(job,spec):
    feed=source_feed(job);spec=normalize(spec,feed);at=preview_time(spec);audit={};headers,rows=table(feed,spec,as_of=at,allow_empty=True,diagnostics=audit)
    return spec,{'valid':True,'report':True,'items':len(rows),'headers':headers,'preview':rows[:10],
        'delivery':summary(feed),'transformation_audit':audit,'source_feed':feed.name,'preview_as_of':at.isoformat(),'preview_timezone':spec['schedule']['timezone'],
        'next_send_at':CronSchedule(spec['schedule']['cron'],spec['schedule']['timezone']).next_after(at).isoformat() if spec['schedule']['mode']=='cron' else None,'warning':'目标日期范围目前暂无数据；可确认规则，发送时将重新读取已保存的数据。' if not rows else '预览使用当前已有数据；发送时会重新计算选定日期范围。','schedule':spec['schedule'],'range':spec['range'],'processing':'aggregate' if spec['aggregates'] else 'raw','note':'报告使用已保存的数据，按自己的发送计划运行；不触发或修改采集任务。'}

def approve(job,user):
    from .models import ConnectorDraft
    from .connector_storage import retain_chain
    with transaction.atomic():
        current=ConnectorDraft.objects.select_for_update().get(pk=job.pk,user=user)
        if current.status=='enabled':return source_feed(current)
        if current.status!='ready' or current.spec.get('kind')!='report':raise ValueError('请先生成报告预览。')
        feed=source_feed(current);plan=FeedReportPlan.objects.select_for_update().get(feed=feed)
        config=destination(plan);selected_workflow(config);address=site_address(config)
        if not address['available']:raise ValueError(address['reason'])
        missing=summary(feed)['missing']
        if missing:raise ValueError('请先补充发送设置：'+'、'.join({'workflow_url':'工作流链接','kind':'邮件或Teams','public_base':'本站公开地址','recipient':'收件邮箱'}[key] for key in missing))
        plan.spec=normalize(current.spec,feed);schedule=plan.spec['schedule']
        plan.schedule_mode=schedule['mode'];plan.cron=schedule.get('cron','');plan.timezone=schedule['timezone']
        from .feed_cron import CronSchedule
        plan.next_run_at=CronSchedule(plan.cron,plan.timezone).next_after(timezone.now()) if plan.schedule_mode=='cron' else None
        plan.enabled=plan.schedule_mode!='manual';plan.created_by=user;plan.save()
        current.feed=feed;current.status='enabled';current.save(update_fields=['feed','status','updated_at']);retain_chain(current)
    return feed

def download_link(delivery,base):
    token=signing.dumps({'id':str(delivery.pk),'nonce':secrets.token_hex(16)},salt='feed-report')
    delivery.token_digest=hashlib.sha256(token.encode()).hexdigest();delivery.save(update_fields=['token_digest'])
    path=reverse('kb:report_public_download',args=[token]);parsed=urlsplit(base);prefix=parsed.path.rstrip('/')
    if prefix and path.startswith(prefix+'/'):return parsed.scheme+'://'+parsed.netloc+path
    return base.rstrip('/')+path

def payload(config,spec,headers,rows,url,audit=None):
    title=spec['title'];count=len(rows)
    quality='；'.join(name+'：'+str(state['converted'])+'条有效，'+str(state['missing'])+'条缺失/未解析，原始数据保留' for name,state in (audit or {}).items())
    if config['kind']=='email':
        body='<h2>'+escape(title)+'</h2><p>EnGenie report · '+str(count)+' records</p><table border="1" cellpadding="6"><tr>'+''.join('<th>'+escape(h)+'</th>' for h in headers)+'</tr>'
        for row in rows[:10]:body+='<tr>'+''.join('<td>'+escape(str(row.get(h,''))[:1000])+'</td>' for h in headers)+'</tr>'
        body+='</table>'
        if quality:body+='<p>'+escape(quality)+'</p>'
        body+='<p><a href="'+escape(url,quote=True)+'">Download complete Excel report / 下载 Excel 报告</a></p><p>Link expires in 7 days. / 链接7天有效。</p>'
        return {'t':config['recipient'],'s':title,'b':body}
    card={'type':'AdaptiveCard','$schema':'http://adaptivecards.io/schemas/adaptive-card.json','version':'1.4',
        'body':[{'type':'TextBlock','text':title,'weight':'Bolder','size':'Large','wrap':True},
                {'type':'TextBlock','text':f'{count} records · EnGenie','wrap':True}],
        'actions':[{'type':'Action.OpenUrl','title':'Download Excel / 下载报告','url':url}]}
    if quality:card['body'].append({'type':'TextBlock','text':quality[:2000],'wrap':True})
    for row in rows[:3]:card['body'].append({'type':'FactSet','facts':[{'title':str(h)[:100],'value':str(row.get(h,''))[:300]} for h in headers[:8]]})
    return {'type':'message','attachments':[{'contentType':'application/vnd.microsoft.card.adaptive','contentUrl':None,'content':card}]}

def post_workflow(config,body):
    from fos_rag.isolated_model import child_environment
    workflow_url(config['url'])
    result=subprocess.run([sys.executable,'-X','utf8','-m','kb.workflow_worker'],
        input=json.dumps({'url':config['url'],'body':body},ensure_ascii=False),text=True,encoding='utf-8',
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=child_environment(),cwd=settings.BASE_DIR,timeout=40,check=False)
    try:reply=json.loads(result.stdout or '{}')
    except ValueError:reply={}
    if result.returncode or not reply.get('ok'):raise ValueError('工作流未确认接收（'+str(reply.get('status','连接超时或失败'))+'）；为避免重复发送，不会自动重试。')
    return reply['status']

def send(plan,*,run=None,spec=None,request_key=None,delivery=None,as_of=None):
    try:require_feed(plan.created_by,plan.feed)
    except ValueError as exc:
        if delivery is None:raise
        delivery.status='failed';delivery.error=str(exc);delivery.save(update_fields=['status','error']);return delivery
    if run is not None and (not plan.enabled or run.status!='completed' or run.feed_id!=plan.feed_id):return None
    scheduled=delivery is not None
    if delivery is None:
        key=request_key or ('run:'+str(run.pk) if run else 'test:'+secrets.token_hex(16))
        delivery,created=FeedReportDelivery.objects.get_or_create(plan=plan,request_key=key,defaults={'run':run})
        if not created:return delivery
    if not FeedReportDelivery.objects.filter(pk=delivery.pk,status='pending').update(status='preparing'):return FeedReportDelivery.objects.get(pk=delivery.pk)
    try:
        spec=normalize(spec or plan.spec,plan.feed,legacy=plan.schedule_mode=='after_collection')
        if as_of is None and run is None and not scheduled:as_of=preview_time(spec)
        audit={};headers,rows=table(plan.feed,spec,run=run,as_of=as_of,diagnostics=audit)
        config=destination(plan);config['url']=selected_workflow(config);address=site_address(config)
        if not address['available']:raise ValueError(address['reason'])
        base=address['public_base']
        delivery.content=workbook(spec['title'],headers,rows,audit);delivery.filename='EnGenie-report.xlsx'
        delivery.expires_at=timezone.now()+timedelta(days=7);delivery.save()
        link=download_link(delivery,base);body=payload(config,spec,headers,rows,link,audit)
        if scheduled and not FeedReportPlan.objects.filter(pk=plan.pk,enabled=True,updated_at=plan.updated_at).exists():raise ValueError('报告配置已变更或停用，本次发送已取消。')
        delivery.status='sending';delivery.save(update_fields=['status'])
        post_workflow(config,body);delivery.status='accepted'
    except Exception as exc:
        delivery.status='unknown' if delivery.status=='sending' else 'failed'
        delivery.error=str(exc)[:400] if isinstance(exc,ValueError) else '报告发送未完成（'+type(exc).__name__+'）。'
    delivery.save(update_fields=['status','error']);return delivery

def after_run(run):
    plan=FeedReportPlan.objects.filter(feed=run.feed,enabled=True,schedule_mode='after_collection').first()
    if plan:send(plan,run=run)

def run_due(now=None):
    """Claim each report occurrence once; never run or depend on a collection."""
    from .feed_cron import CronSchedule
    now=now or timezone.now();results=[]
    stale=now-timedelta(minutes=10)
    FeedReportDelivery.objects.filter(status='sending',created_at__lte=stale).update(status='unknown',error='发送进程中断，结果未知；请检查工作流记录，不自动重试。')
    FeedReportDelivery.objects.filter(status='preparing',created_at__lte=stale).update(status='failed',error='报告生成进程中断；本次未发送。')
    ids=list(FeedReportPlan.objects.filter(enabled=True,schedule_mode='cron',next_run_at__lte=now).order_by('next_run_at','pk').values_list('pk',flat=True)[:100])
    for plan_id in ids:
        with transaction.atomic():
            plan=FeedReportPlan.objects.select_for_update().select_related('feed__kb','created_by').filter(pk=plan_id).first()
            if plan is None:continue
            due=plan.next_run_at
            if not plan.enabled or plan.schedule_mode!='cron' or due is None or due>now:continue
            error=''
            try:
                require_feed(plan.created_by,plan.feed)
                next_at=CronSchedule(plan.cron,plan.timezone).next_after(now)
            except ValueError as exc:next_at=None;error=str(exc)
            moved=FeedReportPlan.objects.filter(pk=plan.pk,enabled=True,schedule_mode='cron',next_run_at=due).update(next_run_at=next_at,**({'enabled':False} if error else {}))
            if not moved:continue
            delivery,created=FeedReportDelivery.objects.get_or_create(plan=plan,request_key='schedule:'+due.isoformat(),defaults={'scheduled_for':due})
        if not created:continue
        if error:
            delivery.status='failed';delivery.error=error;delivery.save(update_fields=['status','error'])
        else:delivery=send(plan,delivery=delivery,as_of=due)
        results.append(delivery)
    return results


def watch_reports(stop,interval=30,report_error=None):
    from django.db import close_old_connections
    while not stop.is_set():
        close_old_connections()
        try:run_due()
        except Exception as exc:
            if report_error:report_error('Report scheduler tick failed: '+type(exc).__name__)
        finally:close_old_connections()
        stop.wait(interval)


def workflow_question_kind(field):
    import re
    if field.get('workflow_kind') in ('email','teams'):return field['workflow_kind']
    key=field.get('key','').lower()
    if re.search(r'(^|_)(teams?|channel)(_|$)',key):return 'teams'
    if re.search(r'(^|_)(email|mail)(_|$)',key):return 'email'
    label=field.get('label','').lower().split('。',1)[0]
    teams=bool(re.search(r'teams|频道',label));email=bool(re.search(r'email|邮件|邮箱',label))
    return ('teams' if teams else 'email') if teams!=email else None


def is_workflow_question(field):
    key=field.get('key','').lower();label=field.get('label','').lower()
    return ('workflow' in key or 'webhook' in key) and ('url' in key or 'link' in key) or any(word in label for word in ('工作流','webhook','workflow')) and any(word in label for word in ('链接','地址','url','link'))


@transaction.atomic
def capture_question_answers(user,job,feedback,answers):
    """Use saved field metadata, not model-written labels, to bind sensitive answers."""
    import re
    if not isinstance(answers,list) or not 1<=len(answers)<=8:raise ValueError('问题回答格式无效。')
    fields={field['key']:field for field in job.preview.get('question_fields',[])}
    if not fields:raise ValueError('这些问题已失效，请刷新对话后回答。')
    seen=set()
    for answer in answers:
        if not isinstance(answer,dict) or answer.get('key') not in fields or answer['key'] in seen:raise ValueError('回答字段不属于当前问题或重复。')
        seen.add(answer['key']);field=fields[answer['key']];value=answer.get('value')
        if not isinstance(value,str) or len(value)>8000:raise ValueError('回答内容过长或格式无效。')
        value=value.strip()
        if field.get('required',True) and not value:raise ValueError('请填写：'+field['label'])
        safe=value
        if value and is_workflow_question(field):
            safe=capture_feedback(user,source_feed(job),value,kind_hint=workflow_question_kind(field))
        if value:feedback=feedback.replace(value,safe)
        # Examples embedded in a question label are not the user's capability URL.
        label=field['label'];safe_label=re.sub(r'https?://[^\s<>）)]+','[示例地址]',label)
        feedback=feedback.replace(label,safe_label)
    for key,field in fields.items():
        if field.get('required',True) and key not in seen:raise ValueError('请填写：'+field['label'])
    return feedback


def redundant_workflow_question(field,delivery):
    if not is_workflow_question(field):return False
    expected=workflow_question_kind(field)
    return bool(delivery.get('untyped_workflow_saved') or expected in delivery.get('saved_workflow_types',[]) if expected else delivery.get('workflow_saved') or delivery.get('untyped_workflow_saved'))


def capture_feedback(user,feed,text,*,kind_hint=None):
    """Extract capability URLs before storing chat text or sending it to a model."""
    import re
    from .feed_credentials import _cipher
    matches=re.findall(r'https://[^\s<>\]）)]+',text)
    urls=[url for url in matches if any(host in urlsplit(url).netloc for host in ('.environment.api.powerplatform.com','.logic.azure.com','.logic.azure.cn'))]
    if not urls:return text
    require_feed(user,feed);plan,_=FeedReportPlan.objects.get_or_create(feed=feed,defaults={'created_by':user})
    config=destination(plan)
    bindings=workflow_bindings(config);url=workflow_url(urls[-1])
    email_hint=bool(re.search(r'email|邮件|邮箱',text,re.I));teams_hint=bool(re.search(r'teams|频道|群聊',text,re.I))
    kind=kind_hint if kind_hint in ('email','teams') else ('email' if email_hint else 'teams') if email_hint!=teams_hint else None
    if kind:config['kind']=kind
    if kind in ('email','teams'):
        bindings[kind]=url;config.update(url=url,url_kind=kind);config.pop('pending_url',None)
    else:config.update(pending_url=url)
    config['workflows']=bindings
    email=re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',text)
    if email:config['recipient']=email.group()
    plan.destination=_cipher(create=True).encrypt(json.dumps(config).encode()).decode();plan.enabled=False;plan.save()
    for url in urls:text=text.replace(url,'[工作流触发地址已加密保存]')
    return text

def question_fields(questions):
    """Validate a small, declarative question form; never accept model HTML."""
    import re
    if not isinstance(questions,list) or not 1<=len(questions)<=8:raise ValueError('请只询问1至8项缺失信息。')
    result=[];keys=set()
    for index,item in enumerate(questions):
        if not isinstance(item,dict):raise ValueError('问题格式无效。')
        key=item.get('key','answer_'+str(index));label=item.get('label');kind=item.get('type','text')
        if not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,39}',key) or key in keys:raise ValueError('问题字段名无效或重复。')
        if not isinstance(label,str) or not 1<=len(label.strip())<=200:raise ValueError('请提供简短明确的问题。')
        if kind not in ('text','email','url','date','number','select','textarea'):raise ValueError('问题输入类型无效。')
        field={'key':key,'label':label.strip(),'type':kind,'required':item.get('required',True) is not False}
        if item.get('workflow_kind') in ('email','teams'):field['workflow_kind']=item['workflow_kind']
        if kind=='select':
            options=item.get('options')
            if not isinstance(options,list) or not 1<=len(options)<=12 or any(not isinstance(value,str) or not 1<=len(value)<=100 for value in options):raise ValueError('请选择1至12项有效选项。')
            field['options']=list(dict.fromkeys(options))
        keys.add(key);result.append(field)
    return result
