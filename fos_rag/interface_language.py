"""Persistent interface language and the locally maintained UI catalogue."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotModified
from django.middleware.locale import LocaleMiddleware
from django.utils import translation
from django.utils.cache import patch_vary_headers


class InterfaceLocaleMiddleware(LocaleMiddleware):
    def process_request(self, request):
        language = request.COOKIES.get("fos_language", "zh-hans")
        language = "en" if language == "en" else "zh-hans"
        translation.activate(language)
        request.LANGUAGE_CODE = language

    def process_response(self, request, response):
        response = super().process_response(request, response)
        patch_vary_headers(response, ("Cookie",))
        return response


@lru_cache(maxsize=2)
def _catalogue(signature):
    result = {"messages": {}, "patterns": [], "englishSource": {}}
    for filename, _mtime, _size in signature:
        data = json.loads(Path(filename).read_text(encoding="utf-8"))
        result["messages"].update(data.get("messages", {}))
        result["patterns"].extend(data.get("patterns", []))
        result["englishSource"].update(data.get("englishSource", {}))
    payload = "window.FOS_I18N_CATALOG=" + json.dumps(result, ensure_ascii=False).replace("<", "\\u003c") + ";\n"
    return payload, '"' + hashlib.sha256(payload.encode()).hexdigest() + '"'


def catalogue(request):
    directory = settings.BASE_DIR / "static" / "i18n"
    files = sorted(directory.glob("*.json"), key=lambda p: (p.name == "common.json", p.name))
    signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    payload, etag = _catalogue(signature)
    response = HttpResponseNotModified() if request.headers.get("If-None-Match") == etag else HttpResponse(payload, content_type="application/javascript; charset=utf-8")
    response["ETag"] = etag
    response["Cache-Control"] = "public, max-age=0, must-revalidate"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def response_language_instruction(language=None):
    language = language or translation.get_language() or "zh-hans"
    name = "English" if language.startswith("en") else "Simplified Chinese"
    return (f"The interface language is {name}. Write user-facing explanations, questions and answers in {name}, "
            "unless the user explicitly requests another language. Preserve source quotations, proper names, "
            "identifiers, URLs, field names, tool names, JSON keys and collected data in their original form. "
            "This language preference does not change any task, permission or confirmation requirement.")
