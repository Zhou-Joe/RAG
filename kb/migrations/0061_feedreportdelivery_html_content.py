from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('kb', '0060_feedreportdelivery_digest')]
    operations = [migrations.AddField(
        model_name='feedreportdelivery', name='html_content',
        field=models.BinaryField(default=bytes),
    )]
