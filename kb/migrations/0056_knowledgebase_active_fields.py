from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('kb', '0055_knowledgebasepin')]

    operations = [
        migrations.AddField(
            model_name='knowledgebase', name='active_fields',
            field=models.JSONField('展示字段', null=True, blank=True, default=None),
        ),
    ]
