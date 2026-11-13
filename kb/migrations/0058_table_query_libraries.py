from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('kb', '0057_feedreportplan_trigger_state')]

    operations = [
        migrations.AddField(
            model_name='knowledgebase', name='table_query',
            field=models.BooleanField(default=False, verbose_name='主要用于表格查询'),
        ),
        migrations.AddField(
            model_name='trackerrow', name='source_sheet',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='来源工作表'),
        ),
        migrations.AddField(
            model_name='trackerrow', name='source_row',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='来源行号'),
        ),
        migrations.AlterUniqueTogether(name='trackerrow', unique_together=set()),
        migrations.AddConstraint(
            model_name='trackerrow',
            constraint=models.UniqueConstraint(fields=('tracker', 'document'), condition=models.Q(source_row__isnull=True), name='unique_tracker_document_summary'),
        ),
        migrations.AddConstraint(
            model_name='trackerrow',
            constraint=models.UniqueConstraint(fields=('tracker', 'document', 'source_sheet', 'source_row'), condition=models.Q(source_row__isnull=False), name='unique_tracker_spreadsheet_row'),
        ),
    ]
