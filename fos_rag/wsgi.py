"""WSGI config for fos_rag project."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fos_rag.settings")

application = get_wsgi_application()
