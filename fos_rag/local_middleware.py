"""Browser-side companion to the optional local-process network boundary."""
from django.conf import settings


class LocalResourcePolicy:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if settings.LOCAL_ONLY:
            response['Content-Security-Policy'] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; "
                "frame-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'"
            )
        return response
