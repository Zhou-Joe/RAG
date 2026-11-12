"""Creator-only web controls for report schedules; collection settings are untouched."""
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from . import access,feed_reports as reports
from .feed_cron import CronSchedule
from .models import ConnectorDraft,FeedReportPlan


def selection(plan):
    saved=plan.spec.get('delivery')
    if isinstance(saved,dict) and saved.get('kind') in ('email','teams'):return saved
    # Legacy approvals did not persist their channel in the plan. Recover only
    # from an exactly matching approved definition, never from a report title.
    keys=('kind','title','columns','range','filters','group_by','aggregates','schedule','trigger')
    for job in ConnectorDraft.objects.filter(feed=plan.feed,user_id=plan.created_by_id,status='enabled',spec__kind='report').order_by('-created_at')[:20]:
        if all(job.spec.get(key)==plan.spec.get(key) for key in keys):
            chosen=reports.delivery_selection(job)
            if chosen:return chosen
    return {}


def state(feed,user):
    plan=FeedReportPlan.objects.filter(feed=feed).first()
    result=reports.summary(feed,selection(plan) if plan and plan.spec.get('kind')=='report' else None)
    result.update(has_report=bool(plan and plan.spec.get('kind')=='report' and plan.spec.get('columns')),
        can_edit=access.can_edit_feed(user,feed),control_url=reverse('kb:report_schedule_control',args=[feed.pk]))
    if plan:result['schedule']={'mode':plan.schedule_mode,'cron':plan.cron,'timezone':plan.timezone}
    return result


def require_ready(plan,chosen):
    config=reports.select_destination(reports.destination(plan),chosen)
    reports.selected_workflow(config)
    address=reports.site_address(config)
    if not address['available']:raise ValueError(address['reason'])
    if config.get('kind')=='email':
        try:validate_email(config.get('recipient',''))
        except (ValidationError,TypeError):raise ValueError('请先在对话中补充报告的收件邮箱。') from None


@transaction.atomic
def change(user,feed,data):
    reports.require_feed(user,feed);access.require_feed_builder(user,feed)
    if not isinstance(data,dict):raise ValueError('报告计划格式无效。')
    action=data.get('action')
    if action not in ('pause','resume','update_schedule'):raise ValueError('不支持的报告计划操作。')
    plan=FeedReportPlan.objects.select_for_update().filter(feed=feed).first()
    if not plan:raise ValueError('请先通过 EnGenie 创建并确认报告。')
    before=(plan.spec,plan.schedule_mode,plan.cron,plan.timezone,plan.enabled,plan.next_run_at)
    if action=='pause':
        plan.enabled=False;plan.next_run_at=None
    else:
        if plan.spec.get('kind')!='report' or not plan.spec.get('columns'):raise ValueError('请先通过 EnGenie 创建并确认报告。')
        chosen=selection(plan)
        old=(plan.schedule_mode,plan.cron,plan.timezone)
        schedule=reports.report_schedule(data.get('schedule') if action=='update_schedule' else
            {'mode':plan.schedule_mode,'cron':plan.cron,'timezone':plan.timezone},feed)
        if schedule['mode']=='after_collection' and plan.schedule_mode!='after_collection':raise ValueError('请选择独立的报告发送频率。')
        if plan.spec.get('trigger') and schedule['mode']=='after_collection':raise ValueError('条件触发报告需使用独立的检查频率或按需检查。')
        if action=='resume' and schedule['mode']=='manual':raise ValueError('当前仅按需发送，请先设置定时频率。')
        enabled=(True if action=='resume' else plan.enabled) and schedule['mode']!='manual'
        if enabled:require_ready(plan,chosen)
        plan.spec={**plan.spec,'schedule':schedule,**({'delivery':chosen} if chosen else {})}
        plan.schedule_mode=schedule['mode'];plan.cron=schedule.get('cron','');plan.timezone=schedule['timezone']
        if not enabled or plan.schedule_mode!='cron':plan.next_run_at=None
        elif not plan.enabled or plan.next_run_at is None or old!=(plan.schedule_mode,plan.cron,plan.timezone):
            plan.next_run_at=CronSchedule(plan.cron,plan.timezone).next_after(timezone.now())
        plan.enabled=enabled
    after=(plan.spec,plan.schedule_mode,plan.cron,plan.timezone,plan.enabled,plan.next_run_at)
    if before!=after:plan.save(update_fields=['spec','schedule_mode','cron','timezone','enabled','next_run_at','updated_at'])
    messages={'pause':'报告发送已暂停；已提交的发送可能仍会完成。','resume':'报告发送已启用。','update_schedule':'报告发送频率已保存。'}
    return {'ok':True,'report':state(feed,user),'message':messages[action]}
