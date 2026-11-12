from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('kb', '0056_knowledgebase_active_fields')]

    operations = [migrations.AddField(
        model_name='feedreportplan', name='trigger_state',
        field=models.JSONField(blank=True, default=dict),
    )]
