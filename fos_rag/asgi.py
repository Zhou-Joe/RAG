"""ASGI config for fos_rag project."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fos_rag.settings")

application = get_asgi_application()

# Start only in the application server, never on a management-command import.
if os.environ.get("FOS_INGESTION_WORKER", "1") == "1":
    from kb.ingestion import start
    start()
