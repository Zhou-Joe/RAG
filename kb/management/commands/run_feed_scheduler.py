from __future__ import annotations

"""Collection and reporting have independent scheduler loops, outside web requests."""
import time
from threading import Event,Thread
from django.core.management.base import BaseCommand,CommandError
from django.db import close_old_connections
from django.utils import timezone
from kb.models import FeedSchedulerState
from kb.feeds import run_due
from kb.feed_reports import run_due as run_reports_due,watch_reports


class Command(BaseCommand):
    help='常驻执行数据采集与独立报告计划；生产也可用系统 cron 调用 sync_feeds --due。'

    def add_arguments(self,parser):
        parser.add_argument('--interval',type=int,default=30)
        parser.add_argument('--once',action='store_true')

    def handle(self,*args,**options):
        if not 5<=options['interval']<=60:raise CommandError('扫描间隔必须为5至60秒。')
        self.stdout.write('Knowledge feed and independent report schedulers started.')
        stop=Event();reports=None
        if options['once']:run_reports_due()
        else:
            reports=Thread(target=watch_reports,args=(stop,options['interval'],self.stderr.write),name='report-scheduler',daemon=True)
            reports.start()
        try:
            while True:
                close_old_connections()
                try:
                    FeedSchedulerState.objects.update_or_create(key='default',defaults={'heartbeat_at':timezone.now()})
                    run_due()
                except Exception as exc:self.stderr.write('Scheduler tick failed: '+type(exc).__name__)
                finally:close_old_connections()
                if options['once']:return
                time.sleep(options['interval'])
        except KeyboardInterrupt:self.stdout.write('Scheduler stopped.')
        finally:
            stop.set()
            if reports:reports.join(timeout=2)
