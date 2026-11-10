from __future__ import annotations

"""业务数据分析接口；权限与追踪表详情页一致（可读即可分析）。

分析不跑模型：`tracker_analysis.analyze()` 只把表内已完成的采集行做 DB 聚合与
Decimal 运算（上限 120 个周期 / 200 个对象），所以「通用」库对其它部门
开放阅读时，这个面板一并开放；仅写操作（字段配置/启停/补录等）仍限管理员。
"""

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from . import access
from .models import KbTracker
from .tracker_analysis import analysis_options, analyze, completed_rows


@user_passes_test(access.can_use_kbs)
@login_required
@require_GET
def tracker_analysis(request, slug: str) -> JsonResponse:
    """返回去重对象/参数选择器，或一个对象在指定日期时间范围内的分析。"""
    tracker = get_object_or_404(KbTracker.objects.select_related('kb'), kb__slug=slug)
    if not access.kb_accessible(request.user, tracker.kb):
        raise Http404('知识库不存在')
    if not access.can_manage_kb(request.user, tracker.kb) and not tracker.enabled:
        # 只读账号只能分析已启用的表（与追踪表详情页同口径：未启用的表不对外）。
        raise Http404('知识库不存在')
    row_id = request.GET.get('row')
    if row_id is None:
        return JsonResponse({'ok': True, **analysis_options(tracker, request.GET.get('q', '').strip()[:100])})
    try:
        row_id = int(row_id)
        if row_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'message': '请选择一个采集对象。'}, status=400)
    row = get_object_or_404(completed_rows(tracker).select_related('tracker', 'feed'), pk=row_id)
    try:
        data = analyze(row, request.GET.get('parameter', ''), request.GET.get('start', ''),
                       request.GET.get('end', ''), request.GET.get('grain', 'day'))
    except ValueError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)
    response = JsonResponse({'ok': True, **data})
    response['Cache-Control'] = 'no-store'
    return response
