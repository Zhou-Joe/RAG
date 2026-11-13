from django.apps import AppConfig


class KbConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "kb"

    def ready(self):
        from .chat_attachments import register_cleanup_signals
        register_cleanup_signals()
