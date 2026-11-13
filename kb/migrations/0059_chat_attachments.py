import django.db.models.deletion
import kb.chat_attachments
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('kb', '0058_table_query_libraries')]

    operations = [migrations.CreateModel(
        name='ChatAttachment',
        fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('original_name', models.CharField(max_length=255)),
            ('file', models.FileField(storage=kb.chat_attachments.ChatAttachmentStorage(), upload_to=kb.chat_attachments.attachment_upload_path)),
            ('kind', models.CharField(choices=[('image', 'Image'), ('document', 'Document')], max_length=12)),
            ('content_type', models.CharField(max_length=100)),
            ('size', models.PositiveIntegerField()),
            ('digest', models.CharField(max_length=64)),
            ('extracted_text', models.TextField(blank=True, default='')),
            ('visual_pages', models.PositiveSmallIntegerField(default=0)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('message', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attachments', to='kb.message')),
        ],
        options={'ordering': ['created_at', 'pk']},
    )]
