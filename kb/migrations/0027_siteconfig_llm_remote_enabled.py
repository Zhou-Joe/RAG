from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("kb", "0026_conversation_read_cursor")]
    operations = [migrations.AddField(model_name="siteconfig", name="llm_remote_enabled", field=models.BooleanField(default=False, verbose_name="允许远程回答 API"))]
