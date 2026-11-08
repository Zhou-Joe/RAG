from __future__ import annotations

"""资料管理页的知识库关联读写接口。"""

import json

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from . import access
from .kb_links import managed_kbs, management_state
from .models import KnowledgeBaseLink


@user_passes_test(access.can_use_kbs)
@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def connections(request) -> JsonResponse:
    """维护无向关系；说明可留空，两端均须可管理，拒绝自关联和子文档关联。"""
    if request.method == 'GET':
        return JsonResponse(management_state(request.user))
    try:
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'message': '请求内容无效。'}, status=400)
    if body.get('action') == 'delete':
        try:
            link_id = int(body.get('id', ''))
        except (TypeError, ValueError):
            return JsonResponse({'ok': False, 'message': '关联编号无效。'}, status=400)
        link = get_object_or_404(KnowledgeBaseLink.objects.select_related('left', 'right'), pk=link_id)
        if not access.can_manage_kb(request.user, link.left) or not access.can_manage_kb(request.user, link.right):
            raise Http404('关联不存在')
        link.delete()
    elif body.get('action') == 'save':
        summary = body.get('summary', '')
        if not isinstance(summary, str) or len(summary.strip()) > 1000:
            return JsonResponse({'ok': False, 'message': '关系说明最多 1000 字，也可以留空。'}, status=400)
        pair = []
        for key in ('left', 'right'):
            slug = body.get(key)
            if not isinstance(slug, str) or not slug:
                return JsonResponse({'ok': False, 'message': '请选择两个不同的知识库。'}, status=400)
            pair.append(get_object_or_404(managed_kbs(request.user), slug=slug))
        if pair[0].pk == pair[1].pk:
            return JsonResponse({'ok': False, 'message': '请选择两个不同的知识库。'}, status=400)
        left, right = sorted(pair, key=lambda kb: kb.pk)
        KnowledgeBaseLink.objects.update_or_create(left=left, right=right,
            defaults={'summary': summary.strip()}, create_defaults={'summary': summary.strip(), 'created_by': request.user})
    else:
        return JsonResponse({'ok': False, 'message': '不支持的操作。'}, status=400)
    return JsonResponse(management_state(request.user))
