from __future__ import annotations

"""连接器生成、预览、打包和显式启用界面；不执行模型生成的代码。"""

import io
import json
import uuid
import zipfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import RequestDataTooBig
from django.db import transaction
from django.db.models import F
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST,require_GET

from . import access, collector_api
from .feed_reports import delivery_context
from .connector_agent import MAX_CALLS, MAX_TOOLS, MAX_SECONDS, authorize, recover_stale
from .connector_tools import check_spec, redact
from .feed_credentials import api_origin, bindings, discard_if_unused, parse_auth, request_headers, secret_values, store, summary
from .feed_http import validate_headers
from .feed_protocol import validate_url
from .feed_views import _managed
from .models import ConnectorDraft, KnowledgeBase, KnowledgeFeed


def _job(request, job_id):
    _managed(request.user)
    try:uuid.UUID(str(job_id))
    except (ValueError,TypeError,AttributeError) as exc:raise ValueError('请选择有效的已保存任务。') from exc
    job = get_object_or_404(ConnectorDraft.objects.select_related('user','kb','feed'), pk=job_id, user=request.user)
    authorize(job)
    return job


def _json(job,*,after=None,before=None):
    first_run=job.feed.runs.order_by('-started_at').first() if job.feed_id else None
    browser_event=next((event for event in reversed(job.events) if event.get('kind')=='browser_view'),None)
    activity=job.activity.all()
    if after is not None:
        rows=list(activity.filter(id__gt=after)[:200])
    else:
        older=activity.filter(id__lt=before) if before is not None else activity
        rows=list(reversed(list(older.order_by('-id')[:120])))
    events=[{**row.payload,'id':row.pk,'kind':row.kind,'turn':row.turn,'at':row.created_at.isoformat()} for row in rows]
    if not events and after is None and before is None:events=job.events
    latest=activity.order_by('-id').values_list('id',flat=True).first() or 0
    last=rows[-1].pk if rows else after or 0
    return {'id':str(job.pk),'status':job.status,'prompt':job.prompt,'spec':job.spec,'preview':job.preview,
        'report_delivery':delivery_context(job),
        'events':events,'event_cursor':last,'latest_event_id':latest,'has_more_events':last<latest,
        'older_event_cursor':rows[0].pk if rows else None,
        'has_older_events':bool(rows and activity.filter(id__lt=rows[0].pk).exists()),
        'sources':job.sources,'questions':job.questions,'metrics':job.metrics,'error':job.error,
        'created_at':job.created_at.isoformat(),'updated_at':job.updated_at.isoformat(),
        'feed_id':str(job.feed_id) if job.feed_id else None,'kb':job.kb.slug if job.kb_id else '',
        'tracker_url':reverse('kb:tracker',args=[job.kb.slug]) if job.kb_id else None,
        'api_url':job.api_url,'allow_private':job.allow_private,'authentication':summary(job),
        'limits':{'model_calls':MAX_CALLS,'tool_calls':MAX_TOOLS,'seconds':MAX_SECONDS},
        'status_url':reverse('kb:connector_status',args=[job.pk]),'thread_url':reverse('kb:connector_thread',args=[job.pk]),
        'report_download_url':reverse('kb:report_preview_download',args=[job.pk]),'report_test_url':reverse('kb:report_test',args=[job.pk]),
        'download_url':reverse('kb:connector_download',args=[job.pk]),
        'enable_url':reverse('kb:connector_enable',args=[job.pk]),
        'credential_url':reverse('kb:connector_credentials',args=[job.pk]),
        'cancel_url':reverse('kb:connector_cancel',args=[job.pk]),
        'refine_url':reverse('kb:connector_refine',args=[job.pk]),'feedback':job.feedback,
        'first_run':{'status':first_run.status,'error':first_run.error,'counts':first_run.counts} if first_run else None,
        'browser_preview_url':reverse('kb:connector_browser_preview',args=[job.pk])+'?v='+str(browser_event['revision']) if browser_event else None}


def _credentials(request,data,url):
    """录入新凭据或复用用户自己草稿中的已保存引用。"""
    auth=data.get('authentication',{'type':'none'})
    if not isinstance(auth,dict):raise ValueError('认证配置格式无效。')
    if auth.get('type')=='saved':
        previous=_job(request,data.get('credential_job'))
        if previous.credential_id:
            if api_origin(url)!=previous.credential.api_origin:raise ValueError('API 主机已改变，请重新填写该主机的凭据。')
            return previous.credential,bindings(previous.credential)
        if previous.header_env:
            if api_origin(url)!=api_origin(previous.api_url):raise ValueError('API 主机已改变，请重新填写凭据。')
            return None,previous.header_env
        raise ValueError('没有可复用的凭据，请重新填写。')
    if auth.get('type','none')!='none':
        credential=store(request.user,url,auth)
        return credential,bindings(credential)
    headers=validate_headers(data.get('header_env',{}))
    if headers and not access.is_global_admin(request.user):raise ValueError('环境变量认证仅供全局管理员配置，请直接使用页面认证表单。')
    return None,headers


@login_required
def index(request):
    libraries = _managed(request.user)
    recover_stale()
    feed=get_object_or_404(KnowledgeFeed,kb__in=libraries,pk=request.GET['feed']) if request.GET.get('feed') else None
    return render(request,'kb/connector.html',{'libraries':libraries,'departments':access.all_departments(),
        'is_global_admin':access.is_global_admin(request.user),
        'editing_feed':feed,'drafts':[]})


@login_required
@require_POST
def resume(request):
    """从数据源打开保存的对话；旧任务没有历史时从现有脚本建立编辑上下文。"""
    libraries=_managed(request.user)
    try:
        data=json.loads(request.body);feed_id=uuid.UUID(str(data.get('feed')))
        with transaction.atomic():
            feed=get_object_or_404(KnowledgeFeed.objects.select_related('kb','credential'),pk=feed_id,kb__in=libraries)
            if not feed.adapter_spec:raise ValueError('该数据源没有 AI 采集脚本。')
            from .feed_reports import remember_request_site
            remember_request_site(request.user,feed,request)
            job=ConnectorDraft.objects.filter(feed=feed,user=request.user).order_by('-created_at').first()
            if job is None:
                job=_script_edit_root(request.user,feed)
            from .connector_storage import retain_chain
            retain_chain(job)
        response=_thread_response(request,job)
        return response
    except (ValueError,TypeError,AttributeError) as exc:return JsonResponse({'ok':False,'error':str(exc)},status=400)


def _script_edit_root(user,feed) -> ConnectorDraft:
    """只使用已批准脚本建立干净的编辑起点，不继承旧对话或工具观察。"""
    fields='、'.join(field['label'] for field in feed.adapter_spec.get('body_fields',[]))
    return ConnectorDraft.objects.create(user=user,kb=feed.kb,feed=feed,status='enabled',retain_context=True,
        prompt='维护已有采集任务「'+feed.name+'」。来源：'+feed.url+'。字段：'+fields+'。定时计划：'+feed.cron+'，时区：'+feed.timezone+'。请按用户后续要求修改现有脚本。',
        api_url=feed.url,spec={**feed.adapter_spec,'write_mode':feed.write_mode},credential=feed.credential,
        header_env=feed.header_env,allow_private=feed.allow_private)


@login_required
@require_POST
def clear_context(request):
    """清空指定脚本的代理上下文，从当前已批准配置继续编辑；不操作定时任务和业务记录。"""
    libraries=_managed(request.user)
    try:
        data=json.loads(request.body);feed_id=uuid.UUID(str(data.get('feed')))
        with transaction.atomic():
            feed=get_object_or_404(KnowledgeFeed.objects.select_for_update().select_related('kb','credential'),pk=feed_id,kb__in=libraries)
            if not feed.adapter_spec:raise ValueError('该数据源没有 AI 采集脚本。')
            # SQLite 的 select_for_update 不加锁；原值写回先取得写锁，避免与新建修改轮次交错。
            KnowledgeFeed.objects.filter(pk=feed.pk).update(updated_at=F('updated_at'))
            owned=ConnectorDraft.objects.filter(user=request.user)
            ids=set(owned.filter(feed=feed).values_list('pk',flat=True));pending=set(ids)
            while pending:
                related=set(owned.filter(pk__in=pending).exclude(parent=None).values_list('parent_id',flat=True))
                related.update(owned.filter(parent_id__in=pending).values_list('pk',flat=True))
                pending=related-ids;ids.update(pending)
            jobs=owned.filter(pk__in=ids)
            if jobs.filter(status__in=['running','enabling']).exists():
                return JsonResponse({'ok':False,'error':'AI 正在执行，请先停止生成后再清空上下文。'},status=409)
            jobs.update(status='context_cleared',context_snapshot='',sample=None,browser_state={},events=[],
                preview={},sources=[],questions=[],metrics={},error='',updated_at=timezone.now())
            job=_script_edit_root(request.user,feed)
            from .connector_sessions import discard
            transaction.on_commit(lambda:discard(ids,request.user.pk))
        return JsonResponse({'ok':True,'thread':[_json(job)],
            'message':'上下文已清空，当前脚本、访问凭据、定时任务和已采集数据均保留。可继续输入修改要求。'})
    except (ValueError,TypeError,AttributeError) as exc:return JsonResponse({'ok':False,'error':str(exc)},status=400)


@login_required
@require_POST
def forget(request):
    """仅清理当前页面已结束任务的临时对话，不删除已批准的数据源。"""
    _managed(request.user)
    try:
        ids=json.loads(request.body).get('ids',[])
        if not isinstance(ids,list) or len(ids)>100:raise ValueError('临时任务列表无效。')
        ids=[uuid.UUID(str(value)) for value in ids]
        with transaction.atomic():
            jobs=ConnectorDraft.objects.filter(pk__in=ids,user=request.user)
            if jobs.filter(status__in=['running','enabling']).exists() or ConnectorDraft.objects.filter(parent_id__in=ids,status__in=['running','enabling']).exists():raise ValueError('任务仍在执行，请先停止。')
            jobs.filter(retain_context=False,feed__isnull=True).delete()
        from .connector_sessions import discard
        discard(ids,request.user.pk)
        return JsonResponse({'ok':True})
    except (ValueError,TypeError,AttributeError) as exc:return JsonResponse({'ok':False,'error':str(exc)},status=400)


@login_required
@require_POST
def create(request):
    """HTTP 外壳：解析页面入参（含页面的「复用已存凭据」）后交给服务层。"""
    libraries = _managed(request.user)
    try:
        data = json.loads(request.body)
        url = (data.get('api_url') or '').strip()
        if url: validate_url(url,allow_fragment=True)
        kb = get_object_or_404(libraries,slug=data['kb']) if data.get('kb') else None
        credential,headers=_credentials(request,data,url)
        job = collector_api.create_draft(user=request.user,requirement=data.get('prompt',''),api_url=url,
            credential=credential,header_env=headers,allow_private=data.get('allow_private',True),
            sample=data.get('sample'),kb=kb,created_via='web',conversation=True)
        return JsonResponse({'ok':True,'job':_json(job)},status=202)
    except RequestDataTooBig: return JsonResponse({'ok':False,'error':'请求内容过大。'},status=413)
    except (ValueError,TypeError,AttributeError) as exc: return JsonResponse({'ok':False,'error':str(exc)},status=400)


@login_required
def status(request,job_id):
    recover_stale()
    try:
        cursors={}
        for key in ('after','before'):
            if key in request.GET:
                value=int(request.GET[key])
                if value<0:raise ValueError('活动记录位置无效。')
                cursors[key]=value
        job=_job(request,job_id)
        from .connector_sessions import touch
        touch(job.pk,request.user.pk)
        response = JsonResponse({'ok':True,'job':_json(job,**cursors)})
    except ValueError as exc: response = JsonResponse({'ok':False,'error':str(exc)},status=403)
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@require_GET
def thread(request,job_id):
    """对话页面回放：返回一条草稿从根到最新的完整链路。"""
    recover_stale()
    return _thread_response(request,_job(request,job_id))


def _thread_response(request,job):
    chain=[]
    for _ in range(100):
        chain.append(job)
        if job.parent_id is None:break
        job=ConnectorDraft.objects.select_related('user','kb','feed').filter(pk=job.parent_id,user=request.user).first()
        if job is None:break
    chain.reverse()
    seen={item.pk for item in chain}
    while chain and len(chain)<100:
        child=ConnectorDraft.objects.select_related('user','kb','feed').filter(parent=chain[-1],user=request.user).order_by('-created_at').first()
        if not child or child.pk in seen:break
        authorize(child);seen.add(child.pk);chain.append(child)
    response=JsonResponse({'ok':True,'thread':[_json(item) for item in chain]})
    response['Cache-Control']='no-store'
    return response


@login_required
@require_GET
def browser_preview(request,job_id):
    """网页操作截图仅对该任务有权用户开放，不放在公开媒体目录。"""
    from .connector_browser import screenshot_path
    _job(request,job_id)
    path=screenshot_path(job_id)
    if not path.is_file():return JsonResponse({'ok':False,'error':'暂无网页画面。'},status=404)
    response=FileResponse(path.open('rb'),content_type='image/png');response['Cache-Control']='no-store'
    return response


@login_required
@require_POST
def enable(request,job_id):
    """确认入库：建/更新定时数据源并立即首跑；规则在 `collector_api.approve`。"""
    try:
        job=_job(request,job_id)
        data=json.loads(request.body or b'{}')
        if job.spec.get('kind')=='report':
            from .feed_reports import approve
            feed=approve(job,request.user)
            return JsonResponse({'ok':True,'feed_id':str(feed.pk)})
        feed=collector_api.approve(user=request.user,job=job,write_mode=data.get('write_mode'),
            department=data.get('department'),selected_fields=data.get('selected_fields'))
        return JsonResponse({'ok':True,'feed_id':str(feed.pk),'url':reverse('kb:feeds')+'?kb='+feed.kb.slug})
    except (ValueError,TypeError,AttributeError) as exc: return JsonResponse({'ok':False,'error':str(exc)},status=400)


@login_required
@require_POST
def credentials(request,job_id):
    """页面直接轮换凭据，同步已启用的数据源，下次请求立即使用新值。"""
    try:
        with transaction.atomic():
            job=_job(request,job_id)
            if job.status=='running':raise ValueError('生成中暂不能修改凭据，请先停止生成。')
            data=json.loads(request.body)
            url=data.get('api_url') or job.api_url
            validate_url(url)
            if job.spec and api_origin(url)!=api_origin(job.spec['url']):raise ValueError('API 主机已改变，请重新生成连接器。')
            credential,headers=_credentials(request,data,url)
            old_id=job.credential_id
            job.api_url=url;job.credential=credential;job.header_env=headers
            if job.spec:job.spec={**job.spec,'header_env':headers}
            job.save(update_fields=['api_url','credential','header_env','spec','updated_at'])
            if job.feed_id:
                if not access.can_manage_kb(request.user,job.feed.kb):raise ValueError('已失去数据源管理权限。')
                job.feed.credential=credential;job.feed.header_env=headers
                if job.feed.adapter_spec:job.feed.adapter_spec={**job.feed.adapter_spec,'header_env':headers}
                job.feed.save(update_fields=['credential','header_env','adapter_spec','updated_at'])
            discard_if_unused(old_id)
        return JsonResponse({'ok':True,'job':_json(job),'message':'认证已保存；验证工具和后续定时更新会立即使用新凭据。'})
    except (ValueError,TypeError,AttributeError) as exc:return JsonResponse({'ok':False,'error':str(exc)},status=400)


@login_required
@require_POST
def test_auth(request):
    """不调用模型，直接对用户指定接口进行有限 GET，响应不包含凭据或正文。"""
    _managed(request.user)
    try:
        from .feed_http import read_url
        data=json.loads(request.body);url=data.get('api_url','');validate_url(url)
        private=data.get('allow_private',True)
        if not isinstance(private,bool):raise ValueError('内网开关格式无效。')
        auth=data.get('authentication',{'type':'none'})
        if isinstance(auth,dict) and auth.get('type')=='saved':headers=request_headers(_job(request,data.get('credential_job')),url)
        else:_,headers,_=parse_auth(auth)
        status_code,headers_out,_=read_url(url,allow_private=private,headers=headers)
        content_type=headers_out.get('content-type','').lower()
        good=status_code==200 and ('json' in content_type or 'html' in content_type)
        message=('网页可以访问，将由浏览器继续检查动态内容和实际数据。' if good and 'html' in content_type else '连接成功，可以读取接口数据。' if good else '认证失败或权限不足，请检查凭据。' if status_code in (401,403) else '来源返回 HTTP '+str(status_code)+'，未能读取数据；请检查地址与参数。')
        response=JsonResponse({'ok':True,'valid':good,'http_status':status_code,'message':message});response['Cache-Control']='no-store'
        return response
    except (ValueError,TypeError,AttributeError) as exc:return JsonResponse({'ok':False,'error':str(exc)},status=400)


@login_required
@require_POST
def cancel(request,job_id):
    job=collector_api.cancel(user=request.user,job=_job(request,job_id))
    return JsonResponse({'ok':True,'job':_json(job)})


@login_required
@require_POST
def refine(request,job_id):
    """HTTP 外壳：页面可以先解析已存凭据（「saved」），其余规则在 `collector_api.refine_draft`。"""
    try:
        previous=_job(request,job_id)
        data=json.loads(request.body)
        extra={}
        if 'api_url' in data or 'authentication' in data:
            url=data.get('api_url') or collector_api.source_url(previous) or previous.api_url
            if url: validate_url(url,allow_fragment=True)
            extra['api_url']=url
            if 'authentication' in data:
                extra['credential'],extra['header_env']=_credentials(request,{**data,'credential_job':str(previous.pk)},url)
        from .feed_reports import source_feed,remember_request_site
        feed=source_feed(previous,required=False)
        if feed:remember_request_site(request.user,feed,request)
        feedback=data.get('feedback','')
        job=collector_api.refine_draft(user=request.user,job=previous,requirement=feedback,
            allow_private=data.get('allow_private',True),question_answers=data.get('question_answers'),**extra)
        return JsonResponse({'ok':True,'job':_json(job)},status=202)
    except (ValueError,TypeError,AttributeError) as exc:return JsonResponse({'ok':False,'error':str(exc)},status=400)


def package(spec: dict) -> bytes:
    """只打包可信运行时和校验后的配置；无用户文件、密钥或任意生成代码。"""
    data = io.BytesIO()
    source = 'from __future__ import annotations\nimport json\nfrom connector_runtime import main\nSPEC = json.loads('+repr(json.dumps(spec,ensure_ascii=False))+')\nif __name__ == "__main__":\n    main(SPEC)\n'
    environment = '\n'.join(name+'=' for name in sorted(set(spec.get('header_env',{}).values())))
    browser=spec.get('source_type')=='browser'
    with zipfile.ZipFile(data,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('connector.py',source)
        archive.writestr('connector.json',json.dumps(spec,ensure_ascii=False,indent=2))
        archive.write(Path(settings.BASE_DIR)/'kb/connector_runtime.py','connector_runtime.py')
        archive.write(Path(settings.BASE_DIR)/'kb/connector_calculations.py','connector_calculations.py')
        archive.write(Path(settings.BASE_DIR)/'scripts/connectors/feed_client.py','feed_client.py')
        if browser:
            archive.write(Path(settings.BASE_DIR)/'kb/browser_runtime.py','browser_runtime.py')
            archive.write(Path(settings.BASE_DIR)/'kb/browser_dom.py','browser_dom.py')
        archive.writestr('requirements.txt','playwright==1.58.0\nhttpx==0.28.1\n' if browser else '# Python 3.10+ standard library only. No pip packages required.\n')
        archive.writestr('.env.example',environment+'\nFOS_FEED_INGEST_URL=\nFOS_FEED_PUSH_TOKEN=\n'+('FOS_FEED_LOGIN_USERNAME=\nFOS_FEED_LOGIN_PASSWORD=\n' if browser else ''))
        archive.writestr('README.md',f'''# {spec['name']}

Python 3.10+。{'网页脚本需安装 requirements.txt 和 Chromium：python -m pip install -r requirements.txt；python -m playwright install chromium。Windows 默认复用现有 Edge。' if browser else 'API脚本无需额外依赖。'}环境变量由运行环境设置；脚本不自动读取 .env。
预览：`python connector.py --preview`

独立 cron 接入：先在自动更新知识库中新建「外部脚本推送」数据源并生成令牌，
配置 FOS_FEED_INGEST_URL、FOS_FEED_PUSH_TOKEN 及 .env.example 中的 API 认证变量，运行：
`python connector.py --push --state-file /absolute/path/checkpoint.json`

Cron：`{spec.get('cron','0 * * * *')}`，时区：`{spec.get('timezone','Asia/Shanghai')}`。
将以下命令放入该时区的系统 crontab，替换绝对路径：
`{spec.get('cron','0 * * * *')} /usr/bin/python3 /path/connector.py --push --state-file /path/checkpoint.json`
Windows 可用任务计划运行同一命令。请避免并行运行同一脚本，推送失败退出非零且不提交游标。

也可在网页审核后启用内置定时拉取，无需部署脚本。两种运行方式请选一种，避免重复数据源。
每次仅请求一页，最多100条/2MB，发布成功后提交游标。写入模式：{spec.get('write_mode','append')}。
append每次采集追加保留历史，upsert按稳定ID覆盖当前行；在推送数据源页面选择对应模式。缺席项与历史快照保留。
{'来源为动态网页，程序会重放已验证的浏览器动作，采集DOM/网络响应及详情页。' if browser else '来源为GET JSON接口。'}采集步骤和字段映射见connector.json。
''')
    return data.getvalue()


@login_required
def download(request,job_id):
    job = _job(request,job_id)
    if job.status not in ('ready','enabled'): return JsonResponse({'ok':False,'error':'请先完成连接器校验。'},status=400)
    spec = check_spec(job,job.spec)
    return FileResponse(io.BytesIO(package(spec)),as_attachment=True,filename='fos-api-connector.zip',content_type='application/zip')
