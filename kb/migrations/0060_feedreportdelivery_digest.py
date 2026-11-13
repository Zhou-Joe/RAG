from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('kb', '0059_chat_attachments')]

    operations = [
        migrations.AddField(
            model_name='feedreportdelivery', name='digest',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
