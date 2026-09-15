from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('kb', '0021_document_page_embed')]
    operations = [migrations.AddField(
        model_name='message', name='verified',
        field=models.BooleanField(default=False, verbose_name='已核对发布'),
    )]
