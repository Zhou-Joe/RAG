"""Per-user shortcuts for the visible top-level KB list."""

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from . import access
from .models import KnowledgeBase, KnowledgeBasePin


@login_required
@require_POST
def kb_pin(request, slug):
    if not access.can_use_kbs(request.user):
        raise Http404("知识库不存在")
    kb = get_object_or_404(
        KnowledgeBase.objects.filter(parent__isnull=True).filter(access.visible_q(request.user)),
        slug=slug,
    )
    selected = request.POST.get("pinned")
    if selected not in ("0", "1"):
        return JsonResponse({"ok": False, "message": "置顶参数无效。"}, status=400)
    pinned = selected == "1"
    if pinned:
        KnowledgeBasePin.objects.get_or_create(user=request.user, kb=kb)
    else:
        KnowledgeBasePin.objects.filter(user=request.user, kb=kb).delete()
    response = JsonResponse({"ok": True, "slug": kb.slug, "pinned": pinned})
    response["Cache-Control"] = "private, no-store"
    return response
