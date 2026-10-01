from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("kb", "0027_siteconfig_llm_remote_enabled")]
    operations = [migrations.AddField(model_name="siteconfig", name="all_remote_apis_enabled", field=models.BooleanField(default=False, verbose_name="允许全部远程 API"))]
