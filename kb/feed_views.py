from __future__ import annotations

"""自动更新管理页与按单个数据源授权的脚本写入接口。"""

import json
import secrets
import uuid
import io
import zipfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import ValidationError,RequestDataTooBig
from django.db import transaction
from django.db.models import Q
from django.http import Http404,JsonResponse,FileResponse
from django.shortcuts import get_object_or_404,render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST,require_http_methods

from . import access
from .feed_cron import CronSchedule
from .feed_http import validate_headers
from .feed_credentials import api_origin, bindings, discard_if_unused, request_headers, store, summary
from .feed_protocol import MAX_BYTES,load_batch,validate_url
from .feeds import FeedBusy,authorize,claim,rotate_token,start_async
from .models import ConnectorDraft,DEPARTMENT_GENERAL,FeedRun,FeedSchedulerState,KnowledgeBase,KnowledgeFeed
from .publication import digest


def _managed(user):
    if not access.can_use_kbs(user):raise Http404('页面不存在')
    return access.managed_kbs(user).filter(is_folder=True)


def _run_json(run):
    return {'id':str(run.pk),'status':run.status,'trigger':run.trigger,'counts':run.counts,'error':run.error,
            'started_at':run.started_at.isoformat(),'finished_at':run.finished_at.isoformat() if run.finished_at else None}


def _feed_json(feed,user):
    from .report_controls import state as report_state
    report=report_state(feed,user)
    runs=[_run_json(run) for run in feed.runs.order_by('-started_at')[:10]]
    ingest_mode=feed.adapter_spec.get('ingest_mode','tracker')
    items=(feed.records.filter(deleted=False).filter(Q(document__isnull=False)|Q(pending_document__isnull=False)).count()
           if ingest_mode=='documents' else feed.tracker_rows.filter(status='done').count())
    return {'id':str(feed.pk),'name':feed.kb.name,'kb':feed.kb.slug,'kb_name':feed.kb.name,'mode':feed.mode,'write_mode':feed.write_mode,
            'ingest_mode':ingest_mode,'kb_url':reverse('kb:manage_detail',args=[feed.kb.slug]),
            'can_edit':access.can_edit_feed(user,feed),'workflow_settings':{'saved_workflow_types':report['saved_workflow_types'],'html_attachments_supported':report['html_attachments_supported']},'report':report,'url':feed.url,'header_env':feed.header_env,'allow_private':feed.allow_private,
            'cron':feed.cron,'timezone':feed.timezone,'enabled':feed.enabled,'token_configured':bool(feed.token_digest),
            'next_run_at':feed.next_run_at.isoformat() if feed.next_run_at else None,
            'items':items,
            'tracker_url':reverse('kb:tracker',args=[feed.kb.slug]),
            'ai_edit_url':reverse('kb:connector_builder')+'?feed='+str(feed.pk) if feed.adapter_spec else None,
            'runs':runs,'generated_connector':bool(feed.adapter_spec),'authentication':summary(feed),'source_type':feed.adapter_spec.get('source_type','api'),
            'ingest_url':reverse('kb:feed_ingest',args=[feed.pk])}


@login_required
def index(request):
    libraries=_managed(request.user)
    return render(request,'kb/feeds.html',{'libraries':libraries,'selected_kb':request.GET.get('kb',''),
        'departments':access.all_departments(),'is_global_admin':access.is_global_admin(request.user)})


@login_required
@transaction.atomic
def state(request):
    libraries=_managed(request.user)
    feeds=KnowledgeFeed.objects.filter(kb__in=libraries).select_related('kb').order_by('-created_at')
    if request.GET.get('kb'):feeds=feeds.filter(kb__slug=request.GET['kb'])
    rows=list(feeds[:101]);heartbeat=FeedSchedulerState.objects.filter(pk='default').first()
    response=JsonResponse({'ok':True,'feeds':[_feed_json(feed,request.user) for feed in rows[:100]],'truncated':len(rows)>100,
        'scheduler_seen_at':heartbeat.heartbeat_at.isoformat() if heartbeat else None,
        'scheduler_recent':bool(heartbeat and heartbeat.heartbeat_at>timezone.now()-timedelta(minutes=3))})
    response['Cache-Control']='no-store'
    return response


@login_required
def kit(request):
    _managed(request.user)
    files=['docs/knowledge-feeds.md','docs/schemas/knowledge-feed-v1.schema.json',
           'scripts/connectors/feed_client.py','scripts/connectors/example_api_feed.py','scripts/connectors/example_feed.json']
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:archive.write(Path(settings.BASE_DIR)/name,arcname=name)
    data.seek(0)
    return FileResponse(data,as_attachment=True,filename='fos-knowledge-feed-v1.zip',content_type='application/zip')


@login_required
@require_POST
def action(request):
    libraries=_managed(request.user)
    if request.headers.get('X-Requested-With')!='XMLHttpRequest':return JsonResponse({'ok':False,'error':'请使用页面操作。'},status=400)
    try:
        data=json.loads(request.body) if request.content_type=='application/json' else request.POST
        command=data.get('action','')
        if command in ('create','update'):
            requested_name=data.get('kb_name',data.get('name'))
            # Older editor forms submitted an empty hidden kb_name beside name.
            if isinstance(requested_name,str) and not requested_name.strip() and 'name' in data:
                requested_name=data['name']
            if requested_name is not None:
                if not isinstance(requested_name,str) or not requested_name.strip() or len(requested_name.strip())>100:
                    raise ValueError('请填写不超过100字符的知识库名称。')
                requested_name=requested_name.strip()
            mode=data.get('mode','pull')
            if mode not in ('pull','push'):raise ValueError('不支持的接入方式。')
            zone=data.get('timezone','Asia/Shanghai');cron=data.get('cron','0 * * * *')
            schedule=CronSchedule(cron,zone)
            url=data.get('url','').strip()
            if mode=='pull':validate_url(url)
            auth=data.get('authentication')
            if auth is not None and not isinstance(auth,dict):raise ValueError('认证配置格式无效。')
            headers=data.get('header_env',{}) if auth is None else {}
            if isinstance(headers,str):headers=json.loads(headers or '{}')
            validate_headers(headers)
            if headers and not access.is_global_admin(request.user):raise ValueError('认证请求头必须由全局管理员配置；可改用脚本推送接入。')
            private=data.get('allow_private',False)
            if not isinstance(private,bool):raise ValueError('内网开关格式无效。')
            if private and not access.is_global_admin(request.user):raise ValueError('仅全局管理员能启用内网 API。')
            with transaction.atomic():
                if command=='create':
                    if data.get('kb'):kb=get_object_or_404(libraries,slug=data['kb'])
                    else:
                        kb_name=requested_name or ''
                        if not kb_name or len(kb_name)>100:raise ValueError('请选择资料集合或填写新知识库名称。')
                        department=data.get('department',DEPARTMENT_GENERAL) if access.is_global_admin(request.user) else access.user_department(request.user)
                        if department not in access.all_departments():raise ValueError('请选择已有部门。')
                        kb=KnowledgeBase.objects.create(name=kb_name,slug='auto-'+uuid.uuid4().hex[:20],is_folder=True,category='business',
                            description='API 自动更新知识库',created_by=request.user,**access.creation_fields(request.user,data.get('access_level'),department))
                    feed=KnowledgeFeed(kb=kb)
                else:
                    feed=get_object_or_404(KnowledgeFeed.objects.select_for_update().filter(kb__in=libraries),pk=data.get('id'))
                    if not access.can_edit_feed(request.user,feed):return JsonResponse({'ok':False,'error':'仅此自动任务的创建者可编辑配置。'},status=403)
                    if 'allow_private' not in data:private=feed.allow_private
                    if feed.lease_token and feed.lease_until and feed.lease_until>timezone.now():raise FeedBusy('任务运行中，请结束后再修改配置。')
                    if feed.url!=url or feed.mode!=mode:feed.cursor='';feed.etag=''
                kb=feed.kb
                # Older callers may still send name; selecting an existing KB on
                # create never renames it implicitly through that legacy alias.
                if requested_name is not None and (command=='update' or not data.get('kb') or 'kb_name' in data):
                    if kb.name!=requested_name:
                        kb.name=requested_name;kb.save(update_fields=['name','updated_at'])
                name=kb.name
                write_mode=data.get('write_mode',feed.write_mode)
                if write_mode not in ('append','upsert'):raise ValueError('请选择追加保留历史或覆盖更新。')
                if feed.write_mode!=write_mode:feed.etag=''
                feed.write_mode=write_mode
                old_credential=feed.credential_id
                if mode!='pull':feed.credential=None;headers={}
                elif auth is not None:
                    if auth.get('type')=='saved':
                        if not feed.header_env:raise ValueError('该数据源尚未保存认证，请填写凭据。')
                        if feed.credential_id:request_headers(feed,url)
                        elif api_origin(feed.url)!=api_origin(url):raise ValueError('API 主机已改变，请重新填写认证。')
                        headers=feed.header_env
                        if not feed.credential_id and not access.is_global_admin(request.user):raise ValueError('环境变量认证须由全局管理员配置。')
                    else:
                        feed.credential=store(request.user,url,auth)
                        headers=bindings(feed.credential) if feed.credential else {}
                elif feed.credential_id:
                    if 'header_env' in data and headers!=feed.header_env:raise ValueError('请通过认证表单修改已保存的凭据。')
                    request_headers(feed,url);headers=feed.header_env
                schedule_changed=feed._state.adding or (feed.mode,feed.cron,feed.timezone)!=(mode,cron,zone)
                feed.name=name;feed.mode=mode;feed.url=url if mode=='pull' else '';feed.header_env=headers
                feed.cron=cron;feed.timezone=zone;feed.allow_private=private
                if feed._state.adding:feed.created_by=request.user
                if feed.adapter_spec:
                    if mode!='pull':feed.adapter_spec={}
                    else:feed.adapter_spec={**feed.adapter_spec,'name':name,'url':url,'header_env':headers,'cron':cron,'timezone':zone,'write_mode':write_mode}
                if mode!='pull':feed.next_run_at=None
                elif schedule_changed or feed.next_run_at is None:feed.next_run_at=schedule.next_after(timezone.now())
                if feed._state.adding:feed.save(force_insert=True)
                else:feed.save(update_fields=['name','mode','write_mode','url','credential','header_env','adapter_spec','cron','timezone','allow_private','next_run_at','cursor','etag','updated_at'])
                from .feed_reports import save_workflow_settings
                save_workflow_settings(request.user,feed,data)
                for draft in ConnectorDraft.objects.filter(feed=feed):
                    draft.credential=feed.credential;draft.header_env=headers
                    if draft.spec:draft.spec={**draft.spec,'header_env':headers,'write_mode':write_mode}
                    draft.save(update_fields=['credential','header_env','spec','updated_at'])
                discard_if_unused(old_credential)
            return JsonResponse({'ok':True,'feed':_feed_json(feed,request.user),'message':'数据源配置已保存。'})
        feed=get_object_or_404(KnowledgeFeed.objects.filter(kb__in=libraries).select_related('kb','created_by'),pk=data.get('id'))
        if command=='run':
            if feed.mode!='pull':raise ValueError('脚本数据源请使用推送接口或 feed_import 命令。')
            run,fresh=claim(feed.pk,force=True)
            if fresh:start_async(run.pk)
            return JsonResponse({'ok':True,'run':_run_json(run),'message':'更新任务已开始。'},status=202)
        if command in ('pause','resume','rotate_token') and not access.can_edit_feed(request.user,feed):return JsonResponse({'ok':False,'error':'仅此自动任务的创建者可编辑配置。'},status=403)
        if command in ('pause','resume'):
            feed.enabled=command=='resume'
            if feed.enabled:authorize(feed)
            feed.save(update_fields=['enabled','updated_at'])
            return JsonResponse({'ok':True,'message':'已启用后续更新。' if feed.enabled else '已暂停后续更新；正在执行的任务会正常结束。'})
        if command=='rotate_token':
            if feed.mode!='push':raise ValueError('拉取型数据源不使用推送令牌。')
            token=rotate_token(feed)
            return JsonResponse({'ok':True,'token':token,'ingest_url':reverse('kb:feed_ingest',args=[feed.pk]),'message':'令牌只显示这一次；旧令牌已失效。'})
        raise ValueError('不支持的操作。')
    except FeedBusy as exc:return JsonResponse({'ok':False,'error':str(exc)},status=409)
    except (ValueError,TypeError,KeyError,AttributeError,ValidationError) as exc:return JsonResponse({'ok':False,'error':str(exc) or '配置格式无效。'},status=400)


def _token_feed(request,feed_id):
    feed=get_object_or_404(KnowledgeFeed.objects.select_related('kb','created_by'),pk=feed_id,mode='push',enabled=True)
    token=request.headers.get('Authorization','')
    if not token.startswith('Bearer ') or not feed.token_digest or not secrets.compare_digest(feed.token_digest,digest(token[7:])):
        raise PermissionError('无效的推送令牌。')
    authorize(feed)
    return feed


@csrf_exempt
@require_POST
def ingest(request,feed_id):
    try:
        feed=_token_feed(request,feed_id)
        if request.content_type!='application/json':raise ValueError('Content-Type 必须是 application/json。')
        if int(request.META.get('CONTENT_LENGTH') or 0)>MAX_BYTES:raise ValueError('单批数据不能超过500MB。')
        # Bound this authenticated feed stream explicitly. request.body applies
        # Django's small generic form limit, which would reject large valid batches.
        payload=load_batch(request.read(MAX_BYTES+1))
        key='batch:'+payload['batch_id'] if payload.get('batch_id') else ''
        run,fresh=claim(feed.pk,trigger='push',payload=payload,request_key=key)
        if fresh:start_async(run.pk)
        return JsonResponse({'ok':True,'run':_run_json(run),'replayed':not fresh,
            'status_url':reverse('kb:feed_run_status',args=[feed.pk,run.pk])},status=202 if run.status=='running' else 200)
    except PermissionError as exc:return JsonResponse({'ok':False,'error':str(exc)},status=401)
    except RequestDataTooBig:return JsonResponse({'ok':False,'error':'单批数据不能超过500MB。'},status=413)
    except FeedBusy as exc:return JsonResponse({'ok':False,'error':str(exc)},status=409)
    except (ValueError,TypeError) as exc:return JsonResponse({'ok':False,'error':str(exc)},status=400)


@require_http_methods(['GET'])
def run_status(request,feed_id,run_id):
    try:
        feed=_token_feed(request,feed_id)
        run=get_object_or_404(FeedRun,pk=run_id,feed=feed)
        response=JsonResponse({'ok':True,'run':_run_json(run)})
        response['Cache-Control']='no-store'
        return response
    except PermissionError as exc:return JsonResponse({'ok':False,'error':str(exc)},status=401)
    except ValueError as exc:return JsonResponse({'ok':False,'error':str(exc)},status=403)
