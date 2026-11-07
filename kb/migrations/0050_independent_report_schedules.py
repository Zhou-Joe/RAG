from django.db import migrations,models


def retain_legacy_reports(apps,schema_editor):
    Plan=apps.get_model('kb','FeedReportPlan')
    for plan in Plan.objects.select_related('feed').all():
        plan.schedule_mode='after_collection';plan.timezone=plan.feed.timezone
        if plan.spec:
            plan.spec={**plan.spec,'range':plan.spec.get('range',{'mode':'latest'}),'schedule':{'mode':'after_collection','timezone':plan.feed.timezone}}
        plan.save(update_fields=['schedule_mode','timezone','spec'])


class Migration(migrations.Migration):
    dependencies=[('kb','0049_feed_report_workflows')]
    operations=[
        migrations.AddField(model_name='feedreportplan',name='schedule_mode',field=models.CharField(choices=[('cron','独立定时'),('manual','按需发送'),('after_collection','采集后发送（兼容）')],default='manual',max_length=20)),
        migrations.AddField(model_name='feedreportplan',name='cron',field=models.CharField(blank=True,max_length=100)),
        migrations.AddField(model_name='feedreportplan',name='timezone',field=models.CharField(default='Asia/Shanghai',max_length=64)),
        migrations.AddField(model_name='feedreportplan',name='next_run_at',field=models.DateTimeField(blank=True,db_index=True,null=True)),
        migrations.AddField(model_name='feedreportdelivery',name='scheduled_for',field=models.DateTimeField(blank=True,db_index=True,null=True)),
        migrations.RunPython(retain_legacy_reports,migrations.RunPython.noop),
    ]
