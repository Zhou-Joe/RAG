from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('kb', '0054_unified_feed_names'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='KnowledgeBasePin',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('kb', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                         related_name='user_pins', to='kb.knowledgebase')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name='knowledge_base_pins',
                                           to=settings.AUTH_USER_MODEL)),
            ],
            options={'constraints': [models.UniqueConstraint(
                fields=('user', 'kb'), name='unique_user_kb_pin')]},
        ),
    ]
