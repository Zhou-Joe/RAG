import hashlib
import json
import secrets
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import Http404,HttpResponse,JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET,require_POST
from .models import ConnectorDraft,FeedReportDelivery,KnowledgeFeed
from . import access
from . import feed_reports as reports

@login_required
@require_POST
def configure(request,feed_id):
    try:
        feed=get_object_or_404(KnowledgeFeed,pk=feed_id);reports.require_feed(request.user,feed)
        reports.remember_request_site(request.user,feed,request)
        reports.configure(request.user,feed,json.loads(request.body))
        return JsonResponse({'ok':True,'report':reports.summary(feed)})
    except (ValueError,TypeError,AttributeError) as exc:return JsonResponse({'ok':False,'error':str(exc)},status=400)

@login_required
@require_GET
def configuration(request,feed_id):
    try:
        feed=get_object_or_404(KnowledgeFeed,pk=feed_id);reports.require_feed(request.user,feed)
        return JsonResponse({'ok':True,'report':reports.summary(feed)})
    except ValueError as exc:return JsonResponse({'ok':False,'error':str(exc)},status=403)

@login_required
@require_POST
def test_send(request,job_id):
    try:
        job=get_object_or_404(ConnectorDraft,pk=job_id,user=request.user)
        if job.status not in ('ready','enabled','revised') or job.spec.get('kind')!='report' or not job.preview.get('valid'):raise ValueError('请先生成并查看报告预览。')
        feed=reports.source_feed(job);access.require_feed_builder(request.user,feed);reports.remember_request_site(request.user,feed,request);data=json.loads(request.body or '{}')
        key=data.get('request_key')
        if not isinstance(key,str) or not 8<=len(key)<=100:raise ValueError('请使用有效的发送请求编号。')
        delivery=reports.send(feed.report_plan,spec=job.spec,selection=reports.delivery_selection(job),request_key='test:'+str(job.pk)+':'+key)
        return JsonResponse({'ok':delivery.status=='accepted','status':delivery.status,'error':delivery.error,
            'message':'工作流已接收请求，请检查邮件/Teams及工作流运行记录。'},status=200 if delivery.status=='accepted' else 400)
    except (ValueError,TypeError,AttributeError,KnowledgeFeed.report_plan.RelatedObjectDoesNotExist) as exc:
        return JsonResponse({'ok':False,'error':str(exc)},status=400)

@login_required
@require_GET
def preview_download(request,job_id):
    job=get_object_or_404(ConnectorDraft,pk=job_id,user=request.user)
    try:
        feed=reports.source_feed(job)
        if job.spec.get('kind')!='report':raise ValueError('此任务没有报告。')
        spec=reports.normalize(job.spec,feed,legacy=True);audit={};headers,rows=reports.table(feed,spec,as_of=reports.preview_time(spec),allow_empty=True,diagnostics=audit)
        response=HttpResponse(reports.workbook(spec['title'],headers,rows,audit),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition']='attachment; filename="EnGenie-preview.xlsx"';response['Cache-Control']='no-store';return response
    except ValueError as exc:return JsonResponse({'ok':False,'error':str(exc)},status=400)

@require_GET
def public_download(request,token):
    try:
        data=signing.loads(token,salt='feed-report',max_age=7*24*3600)
        delivery=FeedReportDelivery.objects.select_related('plan__feed__kb','plan__created_by').get(pk=data['id'])
        if not secrets.compare_digest(delivery.token_digest,hashlib.sha256(token.encode()).hexdigest()) or not delivery.expires_at or delivery.expires_at<=timezone.now():raise ValueError()
        reports.require_feed(delivery.plan.created_by,delivery.plan.feed)
        response=HttpResponse(bytes(delivery.content),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition']='attachment; filename="EnGenie-report.xlsx"';response['Cache-Control']='no-store'
        response['Referrer-Policy']='no-referrer';response['X-Content-Type-Options']='nosniff';return response
    except (signing.BadSignature,ValueError,KeyError,TypeError,FeedReportDelivery.DoesNotExist):raise Http404('报告链接已失效。') from None
