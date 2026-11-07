from threading import Event
from django.core.management.base import BaseCommand,CommandError
from kb.feed_reports import run_due,watch_reports


class Command(BaseCommand):
    help='独立执行报告发送计划，不触发数据采集；也可由 run_feed_scheduler 一同运行。'

    def add_arguments(self,parser):
        parser.add_argument('--interval',type=int,default=30)
        parser.add_argument('--once',action='store_true')

    def handle(self,*args,**options):
        if not 5<=options['interval']<=60:raise CommandError('扫描间隔必须为5至60秒。')
        if options['once']:
            for delivery in run_due():self.stdout.write(str(delivery.pk)+' '+delivery.status)
            return
        stop=Event()
        self.stdout.write('Independent report scheduler started.')
        try:watch_reports(stop,options['interval'],self.stderr.write)
        except KeyboardInterrupt:stop.set();self.stdout.write('Report scheduler stopped.')
