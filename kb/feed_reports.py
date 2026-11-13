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
    if not access.can_edit_feed(user,feed) or getattr(settings,'FEED_REPORT_PUBLIC_BASE',''):return
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
    return summary(feed,delivery_selection(job) if job.spec.get('kind')=='report' else None) if feed else {}


def require_feed(user,feed):
    if not user or not user.is_active or not access.can_use_kbs(user) or not access.can_manage_kb(user,feed.kb):
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
    if not url:raise ValueError('请先由任务创建者在「自主数据引擎 → 编辑配置」保存'+('Teams频道' if kind=='teams' else '邮件')+'工作流 URL，再返回对话继续。')
    return workflow_url(url)


@transaction.atomic
def configure(user,feed,data):
    from .feed_credentials import _cipher
    require_feed(user,feed);access.require_feed_builder(user,feed)
    plan,_=FeedReportPlan.objects.select_for_update().get_or_create(feed=feed,defaults={'created_by':user})
    old=destination(plan);kind=data.get('kind',old.get('kind','email'))
    if kind not in ('email','teams'):raise ValueError('请选择邮件或 Teams 卡片。')
    bindings=workflow_bindings(old)
    supplied=data.get('url') or (old.get('pending_url') if kind not in bindings else None)
    if supplied:bindings[kind]=workflow_url(supplied)
    url=bindings.get(kind,'')
    address=site_address({'public_base':data['public_base']} if data.get('public_base') else old)
    recipient=data.get('recipient',old.get('recipient','')).strip()
    if kind=='email' and recipient:
        try:validate_email(recipient)
        except ValidationError:raise ValueError('请填写有效的收件人邮箱。') from None
        if any(c in recipient for c in '\r\n'):raise ValueError('收件人地址不能换行。')
    if 'html_attachments_supported' in data and type(data['html_attachments_supported']) is not bool:
        raise ValueError('HTML 附件支持设置必须为布尔值。')
    attachment_support=(data['html_attachments_supported'] if 'html_attachments_supported' in data else
                        old.get('html_attachments_supported',False) if bindings.get('email')==workflow_bindings(old).get('email') else False)
    config={'kind':kind,'url':url,'url_kind':kind if url else '', 'workflows':bindings,
        'public_base':address['public_base'],'public_base_source':address['source'],'recipient':recipient,
        'html_attachments_supported':bool(attachment_support)}
    plan.destination=_cipher(create=True).encrypt(json.dumps(config).encode()).decode()
    plan.enabled=False;plan.created_by=user;plan.save()
    return plan


@transaction.atomic
def save_workflow_settings(user,feed,data):
    """Update explicit per-feed bindings without touching collection or report rules."""
    access.require_feed_builder(user,feed)
    replacements={}
    for kind in ('email','teams'):
        value=data.get(kind+'_workflow_url','')
        if not isinstance(value,str):raise ValueError('工作流 URL 格式无效。')
        if value.strip():replacements[kind]=workflow_url(value.strip())
    flag_supplied='html_attachments_supported' in data
    if flag_supplied and type(data['html_attachments_supported']) is not bool:
        raise ValueError('HTML 附件支持设置必须为布尔值。')
    if not replacements and not flag_supplied:return False
    if not replacements and not data.get('html_attachments_supported') and not FeedReportPlan.objects.filter(feed=feed).exists():return False
    from .feed_credentials import _cipher
    plan,_=FeedReportPlan.objects.select_for_update().get_or_create(feed=feed,defaults={'created_by':user})
    config=destination(plan);bindings=workflow_bindings(config)
    changed={kind for kind,value in replacements.items() if bindings.get(kind)!=value}
    support=(data['html_attachments_supported'] if flag_supplied else
             False if 'email' in changed else bool(config.get('html_attachments_supported')))
    if not changed and not config.get('pending_url') and support==bool(config.get('html_attachments_supported')):return False
    bindings.update(replacements);config['workflows']=bindings
    config['html_attachments_supported']=support
    config.pop('pending_url',None)
    kind=config.get('kind')
    if kind in changed:config.update(url=bindings[kind],url_kind=kind)
    plan.destination=_cipher(create=True).encrypt(json.dumps(config).encode()).decode()
    plan.save(update_fields=['destination','updated_at'])
    return True


def delivery_selection(job):
    saved=job.spec.get('delivery') or job.preview.get('delivery') or {}
    if saved.get('kind') not in ('email','teams'):return {}
    result={'kind':saved['kind']}
    if saved['kind']=='email' and 'recipient' in saved:result['recipient']=saved['recipient']
    return result


def select_destination(config,selection=None):
    if not selection:return config
    kind=selection.get('kind',config.get('kind'))
    if kind not in ('email','teams'):raise ValueError('请选择邮件或 Teams 卡片。')
    bindings=workflow_bindings(config)
    result={**config,'kind':kind,'workflows':bindings,'url':bindings.get(kind,''),'url_kind':kind}
    if kind=='email' and 'recipient' in selection:
        recipient=selection['recipient']
        if not isinstance(recipient,str):raise ValueError('请填写有效的收件人邮箱。')
        recipient=recipient.strip()
        if recipient:
            try:validate_email(recipient)
            except ValidationError:raise ValueError('请填写有效的收件人邮箱。') from None
            if any(c in recipient for c in '\r\n'):raise ValueError('收件人地址不能换行。')
        result['recipient']=recipient
    return result


def html_attachment_hint():
    return '请先在现有 Power Automate 邮件工作流中将 attachments 数组的 Name 和 ContentBytes 映射到邮件附件，然后在发送设置勾选支持 HTML 附件。'


def require_html_attachment_mapping(config,digest):
    if digest.get('enabled') and digest.get('email_format')=='html_attachment':
        if config.get('kind')!='email':raise ValueError('HTML 附件仅支持邮件发送；Teams 请使用正文和链接。')
        if not config.get('html_attachments_supported'):raise ValueError(html_attachment_hint())


def workflow_setup_hint(delivery,kind=None):
    if 'html_attachment_mapping' in delivery.get('missing',[]):return html_attachment_hint()
    kind=kind or delivery.get('kind')
    label='Teams' if kind=='teams' else '邮件' if kind=='email' else '对应发送方式的'
    return '请先由任务创建者在「自主数据引擎 → 编辑配置」填写'+label+'工作流 URL 并保存，再返回此对话继续。无需在聊天中粘贴链接。'


def summary(feed,selection=None,*,digest=None):
    from .feed_names import current_name
    plan=FeedReportPlan.objects.filter(feed=feed).first()
    data=select_destination(destination(plan) if plan else {},selection);address=site_address(data)
    last=plan.deliveries.exclude(status='preview').order_by('-created_at').first() if plan else None
    bindings=workflow_bindings(data);selected=bindings.get(data.get('kind'),'')
    missing=[]
    if not selected and (data.get('kind') or not bindings) and not (not data.get('kind') and data.get('pending_url')):missing.append('workflow_url')
    if not data.get('kind'):missing.append('kind')
    requested_digest=(plan.spec.get('digest',{}) if plan else {}) if digest is None else digest
    html_mode=bool(requested_digest.get('enabled') and requested_digest.get('email_format')=='html_attachment' and data.get('kind')=='email')
    if not address['available'] and not html_mode:missing.append('public_base')
    if data.get('kind')=='email' and not data.get('recipient'):missing.append('recipient')
    digest=(plan.spec.get('digest',{}) if plan else {}) if digest is None else digest
    if digest.get('enabled') and digest.get('email_format')=='html_attachment' and data.get('kind')=='email' and not data.get('html_attachments_supported'):
        missing.append('html_attachment_mapping')
    return {'configured':not missing,'enabled':bool(plan and plan.enabled),'workflow_saved':bool(selected),'workflow_type':data.get('kind') if selected else '',
        'settings_url':reverse('kb:feeds')+'?kb='+feed.kb.slug+'&edit='+str(feed.pk),'workflow_setup_hint':workflow_setup_hint({**data,'missing':missing}),
        'html_attachments_supported':bool(data.get('html_attachments_supported')),
        'saved_workflow_types':sorted(bindings),'untyped_workflow_saved':bool(data.get('pending_url')),'missing':missing,'kind':data.get('kind'),
        'recipient':data.get('recipient','') if data.get('kind')=='email' else '','public_base':address['public_base'],'public_base_source':address['source'],
        'site_address_available':address['available'],'site_address_note':address['reason'],
        'title':(plan.spec.get('title') or current_name(feed)) if plan else '',
        'schedule':plan.spec.get('schedule',{'mode':plan.schedule_mode,'cron':plan.cron,'timezone':plan.timezone}) if plan else {},
        'trigger':plan.spec.get('trigger') if plan else None,
        'digest':plan.spec.get('digest',{'enabled':False}) if plan else {'enabled':False},
        'trigger_state':plan.trigger_state if plan else {},
        'next_run_at':plan.next_run_at.isoformat() if plan and plan.next_run_at else None, 'last_status':last.status if last else '',
        'last_error':last.error if last else ''}


def report_visible_fields(feed):
    """An explicit business/table field selection also bounds EnGenie input."""
    from .active_fields import field_selection_owner, visible_fields
    kb = feed.kb
    owner = field_selection_owner(kb)
    if owner is None or owner.active_fields is None:
        return None
    tracker = getattr(kb, 'tracker', None) or getattr(owner, 'tracker', None)
    return set(visible_fields(kb, tracker))


def project_report_values(values, selected):
    return dict(values) if selected is None else {key: value for key, value in values.items() if key in selected}


def validate_report_calculation(computed, feed, *, selected=None):
    """Check record-root references, preserving nested operations on visible data."""
    from .connector_calculations import validate_computed
    validate_computed(computed)
    selected = report_visible_fields(feed) if selected is None else selected
    if selected is None:
        return computed
    allowed = selected | {'_record', '_source_id', '_collected_at'}

    def path(value, nested):
        if value in ('', '$root', '$records'):
            return
        rooted = value.startswith('$root.')
        if rooted:
            value = value[6:]
        if nested and not rooted:
            return
        root = (value[1:].split('/', 1)[0].replace('~1', '/').replace('~0', '~')
                if value.startswith('/') else value.split('.', 1)[0])
        if root not in allowed:
            raise ValueError('The calculation references an unavailable field. Unhide the required field before using it.')

    def visit(node, nested=False):
        if not isinstance(node, dict) or 'literal' in node:
            return
        if 'field' in node:
            path(node['field'], nested)
            return
        if node.get('op') == 'duration' and 'args' not in node:
            path(node['start'], nested); path(node['end'], nested)
        for index, arg in enumerate(node.get('args', [])):
            visit(arg, nested or node.get('op') in ('map', 'filter') and index == 1)
        if 'aggregate' in node:
            visit(node['aggregate'], True)

    visit(computed)
    return computed


def validate_report_visibility(feed, spec):
    selected = report_visible_fields(feed)
    if selected is None:
        return
    allowed = selected | {'_record', '_source_id', '_collected_at'}
    for column in spec.get('columns', []):
        if 'computed' in column:
            validate_report_calculation(column['computed'], feed, selected=selected)
        elif column.get('field') not in allowed:
            raise ValueError('The report references an unavailable field. Unhide the required field or update the report.')
    references = [spec.get('range', {}).get('field', '_collected_at')]
    references.extend(item.get('field') for item in spec.get('filters', []))
    if any(reference not in allowed for reference in references):
        raise ValueError('The report references an unavailable field. Unhide the required field or update the report.')


def visible_report_spec(feed, spec):
    if not isinstance(spec, dict):
        return {}
    try:
        validate_report_visibility(feed, spec)
    except (ValueError, TypeError, KeyError):
        return {}
    return spec


def engenie_report_context(feed, context):
    """Do not replay old raw examples or script fields into a restricted KB chat."""
    if report_visible_fields(feed) is None:
        return context
    result = {**context, 'conversation': [], 'previous_connector': {}, 'previous_examples': {},
              'previous_report': visible_report_spec(feed, context.get('previous_report', {}))}
    delivery = dict(result.get('report_delivery') or {})
    for key in ('digest', 'trigger', 'trigger_state', 'last_error'):
        delivery.pop(key, None)
    result['report_delivery'] = delivery
    return result


def available(feed):
    from django.db.models import Count,Min,Max
    qs=feed.tracker_rows.filter(status='done')
    selected = report_visible_fields(feed)
    fields={'_collected_at','_source_id','_record'}
    profiles={}
    for values in qs.values_list('values',flat=True).iterator(chunk_size=500):
        values = project_report_values(values, selected)
        fields.update(values)
        for name,value in values.items():
            profile=profiles.setdefault(name,{'types':{},'samples':[]});kind=type(value).__name__
            profile['types'][kind]=profile['types'].get(kind,0)+1
            sample=value if type(value) in (int,float,bool,type(None)) else str(value)[:160]
            if sample not in profile['samples'] and len(profile['samples'])<5:profile['samples'].append(sample)
    stats=qs.aggregate(rows=Count('pk'),first_collected_at=Min('updated_at'),last_collected_at=Max('updated_at'))
    prior = getattr(getattr(feed,'report_plan',None),'spec',{})
    previous_report = visible_report_spec(feed, prior)
    delivery = summary(feed)
    if selected is not None and prior and not previous_report:
        delivery = {key: value for key, value in delivery.items() if key not in ('digest', 'trigger', 'trigger_state', 'last_error')}
    return {'fields':sorted(fields),'field_profiles':profiles,'sample':[project_report_values(values, selected) for values in qs.order_by('-updated_at').values_list('values',flat=True)[:5]],
        'dataset':{key:value.isoformat() if hasattr(value,'isoformat') else value for key,value in stats.items()},
        'write_mode':feed.write_mode,'report':delivery,'previous_report':previous_report,
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


def output_headers(spec):
    return (list(spec.get('group_by', [])) + [item['label'] for item in spec.get('aggregates', [])]
            if spec.get('aggregates') else [column['label'] for column in spec.get('columns', [])])


def normalize(spec,feed,*,legacy=False):
    if not isinstance(spec,dict):raise ValueError('报告配置必须是对象。')
    from .feed_names import current_name
    from .report_messages import report_title,normalize_teams_card
    title=report_title(spec.get('title'),current_name(feed))
    columns=spec.get('columns');fields=set(available(feed)['fields'])
    if not isinstance(columns,list) or not 1<=len(columns)<=30:raise ValueError('请选择1至30个报告字段。')
    clean=[];labels=set()
    for column in columns:
        if not isinstance(column,dict):raise ValueError('报告字段格式无效。')
        label=column.get('label','').strip()
        if not label or len(label)>80 or label in labels:raise ValueError('报告列名需有效且不重复。')
        labels.add(label)
        if 'computed' in column:clean.append({'label':label,'computed':validate_report_calculation(column['computed'],feed)})
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
    result={'kind':'report','title':title,'columns':clean,'range':scope,'filters':filters,'group_by':group,'aggregates':aggregates,'schedule':schedule}
    if 'teams_card' in spec:
        result['teams_card']=normalize_teams_card(spec['teams_card'],output_headers(result))
    if 'digest' in spec:
        from .report_digest import normalize_digest
        result['digest']=normalize_digest(spec['digest'],output_headers(result))
    if 'trigger' in spec:
        from .report_conditions import normalize_trigger
        if schedule['mode']=='after_collection':raise ValueError('条件触发报告需使用独立的检查频率或按需检查。')
        headers=group+[item['label'] for item in aggregates] if aggregates else [column['label'] for column in clean]
        result['trigger']=normalize_trigger(spec['trigger'],headers)
    if spec.get('delivery'):
        delivery=select_destination({},spec['delivery']);result['delivery']={'kind':delivery['kind']}
        if delivery['kind']=='email' and 'recipient' in delivery:result['delivery']['recipient']=delivery['recipient']
    if result.get('digest',{}).get('email_format')=='html_attachment':
        kind=result.get('delivery',{}).get('kind')
        if kind is None:
            plan=FeedReportPlan.objects.filter(feed=feed).first()
            kind=destination(plan).get('kind') if plan else None
        if kind=='teams':raise ValueError('HTML 附件仅支持邮件发送；Teams 请使用正文和链接。')
    return result


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
    validate_report_visibility(feed, spec)
    selected = report_visible_fields(feed)
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
        values = project_report_values(row.values, selected)
        record_name = row.record_name if selected is None else next((str(value) for value in values.values() if value not in (None, '', [], {})), 'Record ' + str(row.pk))
        record={**values,'_record':record_name,'_source_id':row.external_id,'_collected_at':timezone.localtime(row.updated_at,zone).isoformat()}
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


def workbook(title,headers,rows,audit=None,digest=None):
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
    if digest:
        from .report_digest import fact_lines
        digest_sheet=book.create_sheet('EnGenie Digest')
        digest_sheet.append(['EnGenie Digest',digest.get('generated_at','')])
        digest_sheet.append([digest.get('text','')])
        for line in fact_lines(digest.get('facts',{}),digest.get('language','en')):
            digest_sheet.append([line])
        for row in digest_sheet:
            for cell in row:
                if isinstance(cell.value,str):cell.data_type='s'
                cell.alignment=Alignment(vertical='top',wrap_text=True)
        digest_sheet.column_dimensions['A'].width=100
        digest_sheet.freeze_panes='A2'
        facts=digest.get('facts',{})
        aggregates=book.create_sheet('Digest Aggregates')
        aggregates.append(['Scope','Group','Metric','Field','Value','Missing','Non-numeric'])
        def add_digest_facts(scope,item,keys=None):
            group=json.dumps(keys,ensure_ascii=False,default=str) if keys else ''
            aggregates.append([scope,group,'row_count','',item.get('row_count'),None,None])
            for metric in item.get('metrics',[]):
                quality=metric.get('quality',{})
                aggregates.append([scope,group,metric['label'],metric.get('field',''),metric.get('value'),quality.get('missing'),quality.get('non_numeric')])
            for field,stats in item.get('numeric_fields',{}).items():
                for name in ('count','sum','avg','min','max'):
                    aggregates.append([scope,group,name,field,stats.get(name),stats.get('missing'),stats.get('non_numeric')])
        add_digest_facts('final_report_output',facts)
        for group in facts.get('groups',[]):add_digest_facts('group',group,group.get('keys'))
        aggregates.append(['context','','omitted_groups','',facts.get('omitted_groups',0),None,None])
        aggregates.append(['context','','omitted_row_count','',facts.get('omitted_row_count',0),None,None])
        for row in aggregates:
            for cell in row:
                if isinstance(cell.value,str):cell.data_type='s'
                cell.alignment=Alignment(vertical='top',wrap_text=True)
        aggregates.freeze_panes='A2'
        for column in ('A','B','C','D','E'):aggregates.column_dimensions[column].width=35
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


def report_fingerprint(headers,rows):
    value=json.dumps({'headers':headers,'rows':rows},ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str,allow_nan=False)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def preview(job,spec):
    feed=source_feed(job);spec=normalize(spec,feed);at=preview_time(spec);audit={};headers,rows=table(feed,spec,as_of=at,allow_empty=True,diagnostics=audit)
    delivery=summary(feed,spec.get('delivery'),digest=spec.get('digest',{'enabled':False}))
    if delivery.get('kind'):
        spec['delivery']={'kind':delivery['kind']}
        if delivery['kind']=='email':spec['delivery']['recipient']=delivery['recipient']
    trigger=spec.get('trigger')
    if trigger:
        from .report_conditions import evaluate_trigger
        evaluation=evaluate_trigger(trigger,rows)
    else:evaluation=None
    digest_preview={}
    if spec.get('digest',{}).get('enabled'):
        from .report_digest import generate_digest,build_facts
        if not rows or evaluation is not None and not evaluation['matched']:
            digest_preview={'enabled':True,'status':'skipped','reason':'no_data' if not rows else 'condition_not_met',
                            'facts':build_facts(headers,rows,spec['digest'])}
        else:
            digest_preview=generate_digest(spec,headers,rows,as_of=at,audit=audit)
        if spec['digest'].get('include_source_links'):
            from .report_presentation import extract_source_links
            digest_preview['source_links']=extract_source_links(headers,rows)
        digest_preview['report_fingerprint']=report_fingerprint(headers,rows)
        if spec['digest'].get('email_format') in ('html_attachment','html_link'):
            digest_preview['html']={'filename':'EnGenie-report.html','download_url':reverse('kb:report_preview_download',args=[job.pk])+'?format=html'}
    from .report_messages import teams_card_summary
    from .feed_names import current_name
    card_preview=teams_card_summary(spec,headers,rows) if delivery.get('kind')=='teams' else None
    if card_preview is not None:card_preview['will_send']=evaluation['matched'] if evaluation is not None else bool(rows)
    delivery['title']=spec['title']
    next_at=CronSchedule(spec['schedule']['cron'],spec['schedule']['timezone']).next_after(at).isoformat() if spec['schedule']['mode']=='cron' else None
    return spec,{'valid':True,'report':True,'items':len(rows),'headers':headers,'preview':rows[:10],
        'delivery':delivery,'teams_card_preview':card_preview,'transformation_audit':audit,'source_feed':current_name(feed),'preview_as_of':at.isoformat(),'preview_timezone':spec['schedule']['timezone'],
        'trigger':trigger,'trigger_evaluation':evaluation,'next_check_at':next_at if trigger else None,
        'digest':spec.get('digest',{'enabled':False}),'digest_preview':digest_preview,
        'next_send_at':None if trigger else next_at,'warning':'目标日期范围目前暂无数据；可确认规则，发送时将重新读取已保存的数据。' if not rows else '预览使用当前已有数据；发送时会重新计算选定日期范围。','schedule':spec['schedule'],'range':spec['range'],'processing':'aggregate' if spec['aggregates'] else 'raw','note':'报告使用已保存的数据，按自己的发送计划运行；不触发或修改采集任务。'}

def approve(job,user):
    from .models import ConnectorDraft
    from .connector_storage import retain_chain
    with transaction.atomic():
        current=ConnectorDraft.objects.select_for_update().get(pk=job.pk,user=user)
        if current.status=='enabled':return source_feed(current)
        if current.status!='ready' or current.spec.get('kind')!='report':raise ValueError('请先生成报告预览。')
        feed=source_feed(current);access.require_feed_builder(user,feed);plan=FeedReportPlan.objects.select_for_update().get(feed=feed)
        selection=delivery_selection(current)
        config=select_destination(destination(plan),selection);selected_workflow(config);address=site_address(config)
        if not address['available'] and current.spec.get('digest',{}).get('email_format')!='html_attachment':raise ValueError(address['reason'])
        missing=summary(feed,selection,digest=current.spec.get('digest',{'enabled':False}))['missing']
        if 'html_attachment_mapping' in missing:raise ValueError(html_attachment_hint())
        if missing:raise ValueError('请先补充发送设置：'+'、'.join({'workflow_url':'工作流链接','kind':'邮件或Teams','public_base':'本站公开地址','recipient':'收件邮箱'}[key] for key in missing))
        plan.spec=normalize(current.spec,feed)
        plan.trigger_state={}
        if selection:plan.spec['delivery']=selection
        schedule=plan.spec['schedule']
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

def payload(config,spec,headers,rows,url,audit=None,digest=None,*,html_url='',html_content=None):
    title=spec['title'];count=len(rows)
    alert='Condition matched / 触发条件已满足' if spec.get('trigger') else ''
    quality='；'.join(name+'：'+str(state['converted'])+'条有效，'+str(state['missing'])+'条缺失/未解析，原始数据保留' for name,state in (audit or {}).items())
    digest_lines=[]
    if digest:
        from .report_digest import fact_lines
        digest_lines=fact_lines(digest.get('facts',{}),spec.get('digest',{}).get('language','en'))
    if config['kind']=='email':
        from .report_email import email_signature
        signature=email_signature(site_address(config)['public_base'])
        if digest and spec.get('digest',{}).get('email_format')=='html_attachment':
            require_html_attachment_mapping(config,spec['digest'])
            from .report_presentation import render_html_report,email_html_attachment
            html=html_content or render_html_report(spec,headers,rows,digest,report_url=None,source_links=digest.get('source_links'),audit=audit)
            short_body='<h2>'+escape(title)+'</h2><p>EnGenie Digest report is attached as HTML. / EnGenie 摘要报告已作为 HTML 附件提供。</p><p>Open the attachment in a browser for tables and charts. / 请使用浏览器打开附件查看表格和图表。</p>'
            return {'t':config['recipient'],'s':title,'b':short_body+signature,
                    'attachments':[email_html_attachment('EnGenie-report.html',html)]}
        body='<h2>'+escape(title)+'</h2><p>EnGenie report · '+str(count)+' records</p>'+('<p>'+alert+'</p>' if alert else '')+'<table border="1" cellpadding="6"><tr>'+''.join('<th>'+escape(h)+'</th>' for h in headers)+'</tr>'
        for row in rows[:10]:body+='<tr>'+''.join('<td>'+escape(str(row.get(h,''))[:1000])+'</td>' for h in headers)+'</tr>'
        body+='</table>'
        if quality:body+='<p>'+escape(quality)+'</p>'
        if digest:
            body+='<h3>EnGenie Digest</h3><p>'+escape(digest.get('text','')).replace('\n','<br>')+'</p>'
            body+='<p>'+'<br>'.join(escape(line) for line in digest_lines)+'</p>'
            if spec.get('digest',{}).get('include_source_links'):
                from .report_presentation import extract_source_links,source_links_html
                body+=source_links_html(digest.get('source_links') or extract_source_links(headers,rows),language=spec['digest'].get('language','en'))
            if html_url:body+='<p><a href="'+escape(html_url,quote=True)+'">Open interactive HTML report / 打开交互式 HTML 报告</a></p>'
        body+='<p><a href="'+escape(url,quote=True)+'">Download complete Excel report / 下载 Excel 报告</a></p><p>Link expires in 7 days. / 链接7天有效。</p>'
        result={'t':config['recipient'],'s':title,'b':body+signature}
        return result
    card={'type':'AdaptiveCard','$schema':'http://adaptivecards.io/schemas/adaptive-card.json','version':'1.4',
        'body':[{'type':'TextBlock','text':title,'weight':'Bolder','size':'Large','wrap':True}],
        'actions':[{'type':'Action.OpenUrl','title':'Download Excel / 下载报告','url':url}]}
    if html_url:card['actions'].insert(0,{'type':'Action.OpenUrl','title':'Open HTML report / 打开 HTML 报告','url':html_url})
    if any(state.get('missing') for state in (audit or {}).values()):
        card['body'].append({'type':'TextBlock','wrap':True,'text':
            'Some values are unavailable. The complete processing audit is in the Excel report. / 部分数值缺失，完整处理记录请查看 Excel 报告。'})
    if alert:card['body'].append({'type':'TextBlock','text':alert,'wrap':True})
    if digest and spec.get('digest',{}).get('include_source_links'):
        from .report_presentation import extract_source_links,teams_source_actions
        selection=digest.get('source_links') or extract_source_links(headers,rows)
        source_actions=teams_source_actions(selection,limit=3 if html_url else 4)
        card['actions'].extend(source_actions)
        if selection.get('total_links',0)>len(source_actions):
            card['body'].append({'type':'TextBlock','text':f"Source links shown: {len(source_actions)} / {selection['total_links']}. Additional URLs remain in the complete report. / 其余来源链接保留在完整报告中。",'wrap':True})
    from .report_messages import teams_card_summary
    compact=teams_card_summary(spec,headers,rows)
    if compact['facts']:card['body'].append({'type':'FactSet','facts':compact['facts']})
    if compact['omitted_rows']:
        card['body'].append({'type':'TextBlock','wrap':True,'text':
            f"Items shown: {compact['shown_rows']} / {compact['row_count']}. Card space is limited; the complete list is in the Excel report. / 卡片空间有限，完整清单请查看 Excel 报告。"})
    if digest:
        card['body'].append({'type':'TextBlock','text':'EnGenie Digest','weight':'Bolder','wrap':True})
        card['body'].append({'type':'TextBlock','text':digest.get('text',''),'wrap':True})
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

def send(plan,*,run=None,spec=None,request_key=None,delivery=None,as_of=None,selection=None):
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
    delivery.status='preparing';trigger=None
    try:
        spec=normalize(spec or plan.spec,plan.feed,legacy=plan.schedule_mode=='after_collection')
        if as_of is None and run is None and not scheduled:as_of=preview_time(spec)
        trigger=spec.get('trigger')
        audit={};headers,rows=table(plan.feed,spec,run=run,as_of=as_of,allow_empty=bool(trigger),diagnostics=audit)
        if trigger:
            from .report_conditions import evaluate_trigger,claim_trigger
            evaluation=evaluate_trigger(trigger,rows)
            reason=claim_trigger(plan,delivery,trigger,evaluation,as_of or timezone.now()) if scheduled else '' if evaluation['matched'] else 'condition_not_met'
            if reason:
                delivery.status='skipped';delivery.error=reason
                delivery.save(update_fields=['status','error']);return delivery
        config=select_destination(destination(plan),selection or spec.get('delivery'));config['url']=selected_workflow(config);address=site_address(config)
        html_mode=bool(spec.get('digest',{}).get('enabled') and spec['digest'].get('email_format')=='html_attachment' and config.get('kind')=='email')
        if not address['available'] and not html_mode:raise ValueError(address['reason'])
        require_html_attachment_mapping(config,spec.get('digest',{}))
        base=address['public_base']
        digest={}
        if spec.get('digest',{}).get('enabled'):
            from .report_digest import generate_digest
            digest=generate_digest(spec,headers,rows,as_of=as_of,audit=audit)
            if not isinstance(digest,dict) or not isinstance(digest.get('text'),str) or not digest['text'].strip():
                raise ValueError('EnGenie 摘要生成失败，本次未发送。')
            digest={**digest,'language':spec['digest']['language'],'settings':spec['digest']}
            digest['report_fingerprint']=report_fingerprint(headers,rows)
            if spec['digest'].get('include_source_links'):
                from .report_presentation import extract_source_links
                digest['source_links']=extract_source_links(headers,rows)
        delivery.digest=digest
        report_html=None
        if digest and spec['digest'].get('email_format') in ('html_attachment','html_link'):
            from .report_presentation import render_html_report
            report_html=render_html_report(spec,headers,rows,digest,source_links=digest.get('source_links'),audit=audit)
        delivery.html_content=report_html.encode('utf-8') if report_html else b''
        delivery.content=(workbook(spec['title'],headers,rows,audit,digest=digest) if digest
                          else workbook(spec['title'],headers,rows,audit));delivery.filename='EnGenie-report.xlsx'
        delivery.expires_at=timezone.now()+timedelta(days=7);delivery.save(update_fields=['content','html_content','digest','filename','expires_at'])
        link='' if html_mode else download_link(delivery,base)
        body=(payload(config,spec,headers,rows,link,audit,digest=digest,html_content=report_html,
                      html_url=link+'?format=html' if link and report_html else '') if digest
              else payload(config,spec,headers,rows,link,audit))
        if scheduled and not FeedReportPlan.objects.filter(pk=plan.pk,enabled=True,updated_at=plan.updated_at).exists():raise ValueError('报告配置已变更或停用，本次发送已取消。')
        if scheduled and trigger:
            from .report_conditions import start_trigger_send
            start_trigger_send(plan,delivery)
        elif not FeedReportDelivery.objects.filter(pk=delivery.pk,status='preparing').update(status='sending'):
            raise ValueError('报告发送已取消，本次未发送。')
        delivery.status='sending'
        post_workflow(config,body);delivery.status='accepted'
    except Exception as exc:
        if scheduled and trigger and delivery.status!='sending':
            from .report_conditions import release_trigger
            release_trigger(plan,delivery)
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
    if field.get('workflow_kind') in ('email','teams') and field.get('type')=='url':return True
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


@transaction.atomic
def capture_feedback(user,feed,text,*,kind_hint=None):
    """Extract capability URLs before storing chat text or sending it to a model."""
    import re
    from .feed_credentials import _cipher
    matches=re.findall(r'https://[^\s<>\]）)]+',text)
    urls=[url for url in matches if any(host in urlsplit(url).netloc for host in ('.environment.api.powerplatform.com','.logic.azure.com','.logic.azure.cn'))]
    if not urls:return text
    require_feed(user,feed);access.require_feed_builder(user,feed);plan,_=FeedReportPlan.objects.select_for_update().get_or_create(feed=feed,defaults={'created_by':user})
    config=destination(plan)
    bindings=workflow_bindings(config);url=workflow_url(urls[-1])
    hint_text=text
    for supplied_url in urls:hint_text=hint_text.replace(supplied_url,'')
    email_hint=bool(re.search(r'email|邮件|邮箱',hint_text,re.I));teams_hint=bool(re.search(r'teams|频道|群聊',hint_text,re.I))
    kind=kind_hint if kind_hint in ('email','teams') else ('email' if email_hint else 'teams') if email_hint!=teams_hint else None
    if kind:config['kind']=kind
    if kind in ('email','teams'):
        bindings[kind]=url;config.update(url=url,url_kind=kind);config.pop('pending_url',None)
    else:config.update(pending_url=url)
    config['workflows']=bindings
    email=re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',hint_text)
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
