"""ASGI config for fos_rag project."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fos_rag.settings")

application = get_asgi_application()
