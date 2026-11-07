from __future__ import annotations

"""供系统 cron 每分钟调用；到期任务使用数据库租约互斥。"""
import json
from django.core.management.base import BaseCommand,CommandError
from django.utils import timezone
from kb.models import FeedSchedulerState,KnowledgeFeed
from kb.feeds import claim,execute,run_due
from kb.feed_reports import run_due as run_reports_due

class Command(BaseCommand):
    help='执行到期的 API 数据源，或手动执行指定数据源。'
    def add_arguments(self,parser):
        group=parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--due',action='store_true')
        group.add_argument('--source')
    def handle(self,*args,**options):
        FeedSchedulerState.objects.update_or_create(key='default',defaults={'heartbeat_at':timezone.now()})
        reports=[]
        if options['due']:
            reports=run_reports_due();runs=run_due()
        else:
            try:
                feed=KnowledgeFeed.objects.get(pk=options['source'],mode='pull')
                run,fresh=claim(feed.pk,force=True);runs=[execute(run.pk) if fresh else run]
            except (ValueError,KnowledgeFeed.DoesNotExist) as exc:raise CommandError(str(exc)) from exc
        for run in runs:self.stdout.write(json.dumps({'run_id':str(run.pk),'status':run.status,'counts':run.counts,'error':run.error},ensure_ascii=False))
        for delivery in reports:self.stdout.write(json.dumps({'report_id':str(delivery.plan_id),'delivery_id':str(delivery.pk),'scheduled_for':delivery.scheduled_for.isoformat() if delivery.scheduled_for else None,'status':delivery.status,'error':delivery.error},ensure_ascii=False))
        if any(run.status=='failed' for run in runs) or any(delivery.status in ('failed','unknown') for delivery in reports):raise CommandError('部分数据源更新失败，请查看运行日志。')
