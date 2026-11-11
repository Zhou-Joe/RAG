"""Read-only browsing and aggregation for an automatically collected knowledge base."""

from __future__ import annotations

from datetime import date, datetime, timedelta, time
import bisect
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import UUID

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Case, Count, F, FloatField, Max, Min, Q, Sum, TextField, Value, When
from django.db.models.functions import (
    Cast, Length, TruncDay, TruncHour, TruncMinute, TruncMonth, TruncWeek, TruncYear)
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET

from . import access
from .models import Document, FeedRun, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow
from .tracker_query import NUMERIC, TrackerQuery, TrackerText, cell


def _run_info(run, *, show_error=False):
    if run is None:
        return None
    return {
        "id": str(run.pk),
        "feed_id": str(run.feed_id),
        "feed_name": run.feed.kb.name,
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "counts": run.counts if isinstance(run.counts, dict) else {},
        "error": run.error if show_error else "",
    }


@user_passes_test(access.can_use_kbs)
@login_required
@require_GET
def feed_data(request, slug):
    kb = get_object_or_404(KnowledgeBase, slug=slug, category="business")
    if not access.kb_accessible(request.user, kb):
        raise Http404("知识库不存在")
    feeds = KnowledgeFeed.objects.filter(kb=kb)
    if not feeds.exists():
        raise Http404("采集任务不存在")

    feed_id = request.GET.get("feed", "")
    if feed_id:
        try:
            feed_id = UUID(feed_id)
        except (TypeError, ValueError, AttributeError):
            return JsonResponse({"ok": False, "message": "数据源无效。"}, status=400)
        feeds = feeds.filter(pk=feed_id)
        if not feeds.exists():
            raise Http404("数据源不存在")

    runs = FeedRun.objects.filter(feed__in=feeds).select_related("feed__kb")
    latest_run = runs.order_by("-started_at", "-pk").first()
    latest_success = runs.filter(status="completed").order_by("-finished_at", "-pk").first()
    latest_mode = None
    if latest_success:
        latest_mode = latest_success.counts.get("write_mode") if isinstance(latest_success.counts, dict) else None
        if latest_mode not in ("append", "upsert"):
            latest_mode = ("append" if TrackerRow.objects.filter(feed_run=latest_success).exists()
                           else latest_success.feed.write_mode)
    scope = request.GET.get("scope", "latest")
    if scope not in ("latest", "history"):
        return JsonResponse({"ok": False, "message": "查看范围无效。"}, status=400)
    if scope == "history":
        row_scope = Q(feed__in=feeds)
    elif latest_success is None:
        row_scope = Q(pk__in=[])
    elif latest_mode == "upsert":
        # Upsert replaces the current row instead of creating a batch snapshot.
        row_scope = Q(feed_id=latest_success.feed_id, feed_run__isnull=True)
    else:
        row_scope = Q(feed_run_id=latest_success.pk)

    tracker = KbTracker.objects.filter(kb=kb).first()
    common = {
        "scope": scope,
        "latest_run": _run_info(latest_run, show_error=access.can_manage_kb(request.user, kb)),
        "latest_success": _run_info(latest_success),
        "latest_is_current": latest_mode == "upsert",
    }
    trend_timezone = feeds.first().timezone if feed_id else settings.TIME_ZONE
    return _record_data(request, kb, tracker, row_scope, Q(feed__in=feeds), common,
                        trend_timezone=trend_timezone)


@user_passes_test(access.can_use_kbs)
@login_required
@require_GET
def tracker_data(request, slug):
    """Browse approved manual tracksheet rows visible in this knowledge base."""
    from .views import _scope_docs

    kb = get_object_or_404(KnowledgeBase, slug=slug)
    if not access.kb_accessible(request.user, kb):
        raise Http404("知识库不存在")
    tracker = KbTracker.objects.filter(kb=kb).first()
    if tracker is None or not tracker.fields or not (tracker.enabled or access.can_manage_kb(request.user, kb)):
        raise Http404("追踪表不存在")
    scope = Q(tracker=tracker, feed__isnull=True, document__in=_scope_docs(kb, request.user))
    # Append-mode document feeds retain completed old versions as superseded
    # documents. Only the trend needs their past snapshots; the record list
    # and ordinary aggregation continue to show current documents.
    historical = (
        Q(tracker=tracker, feed__isnull=True, document__status=Document.Status.SUPERSEDED,
          document__kb__parent=kb, document__kb__feed_record__feed__kb=kb,
          document__kb__feed_record__deleted=False,
          document__kb__feed_record__feed__write_mode="append",
          document__kb__feed_record__feed__adapter_spec__ingest_mode="documents")
        & access.visible_q(request.user, "document__kb__")
    )
    return _record_data(request, kb, tracker, scope, scope, {"scope": "history"},
                        trend_scope=scope | historical)


def _analysis_zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)


def _time_range(request, zone):
    """Local datetimes are [from, to); legacy calendar end dates include the day."""
    def boundary(name, end=False):
        raw = request.GET.get(name, "").strip()
        if not raw:
            return None
        if len(raw) > 64:
            raise ValueError
        if len(raw) == 10:
            value = date.fromisoformat(raw)
            if end:
                value += timedelta(days=1)
            return datetime.combine(value, time.min, tzinfo=zone)
        value = datetime.fromisoformat(raw)
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return value.astimezone(zone)

    first, last = boundary("from"), boundary("to", end=True)
    if first is not None and last is not None and first >= last:
        raise ValueError
    return first, last


def _time_scope(first, last):
    scope = Q()
    if first is not None:
        scope &= Q(updated_at__gte=first)
    if last is not None:
        scope &= Q(updated_at__lt=last)
    return scope


def _numeric_fields(rows, fields):
    """Classify complete scoped data with the same scalar parsing used by metrics."""
    annotations = {}
    metrics = {}
    for index, field in enumerate(fields):
        raw, number = f"option_raw_{index}", f"option_number_{index}"
        annotations[raw] = Cast(TrackerText(field, "values"), TextField())
        annotations[number] = Case(
            When(**{raw + "__regex": NUMERIC}, then=Cast(F(raw), FloatField())),
            default=Value(None), output_field=FloatField())
        # Numeric strings such as 1e999 match the grammar but are not usable
        # finite values. Do not advertise a field containing only those values.
        metrics[f"field_{index}"] = Count(number, filter=Q(**{
            number + "__gte": -float.fromhex('0x1.fffffffffffffp+1023'),
            number + "__lte": float.fromhex('0x1.fffffffffffffp+1023'),
        }))
    if not fields:
        return []
    counts = rows.order_by().annotate(**annotations).aggregate(**metrics)
    return [field for index, field in enumerate(fields) if counts[f"field_{index}"]]


def _analysis_options(request, tracker, scope, fields, first, last, zone, data_scope):
    object_field = request.GET.get("object_field", "")
    query = request.GET.get("object_query", "").strip()
    try:
        offset = int(request.GET.get("object_offset", "0"))
        if offset < 0 or offset > 100_000 or len(query) > 200:
            raise ValueError
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "message": "对象搜索或分页参数无效。"}, status=400)
    if object_field and object_field not in fields and object_field not in ("$source_id", "$document_name"):
        return JsonResponse({"ok": False, "message": "请选择有效的对象字段。"}, status=400)
    rows = TrackerRow.objects.filter(scope, tracker=tracker, status=TrackerRow.Status.DONE)
    bounds = TrackerRow.objects.filter(data_scope, tracker=tracker, status=TrackerRow.Status.DONE).aggregate(
        data_from=Min("updated_at"), data_to=Max("updated_at"))
    numeric_fields = _numeric_fields(rows, fields)
    values = []
    if object_field:
        expression = (Cast(F("external_id"), TextField()) if object_field == "$source_id" else
                      Cast(F("document__original_name"), TextField()) if object_field == "$document_name" else
                      Cast(TrackerText(object_field, "values"), TextField()))
        choices = rows.order_by().annotate(option_value=expression).annotate(
            option_length=Length("option_value"),
        ).filter(option_length__lte=200, option_value__regex=r'\S')
        if query:
            choices = choices.filter(option_value__icontains=query)
        values = list(choices.values_list("option_value", flat=True).distinct().order_by("option_value")
                      [offset:offset + 51])
    has_more = len(values) > 50
    response = JsonResponse({
        "ok": True, "kind": "options", "fields": fields, "numeric_fields": numeric_fields,
        "object_field": object_field, "object_values": values[:50],
        "object_has_more": has_more, "object_next_offset": offset + 50 if has_more else None,
        "series_max": _TREND_SERIES_MAX, "timezone": str(zone),
        "range_from": first.isoformat() if first else None,
        "range_to": last.isoformat() if last else None,
        "data_from": bounds["data_from"].astimezone(zone).isoformat() if bounds["data_from"] else None,
        "data_to": bounds["data_to"].astimezone(zone).isoformat() if bounds["data_to"] else None,
    })
    response["Cache-Control"] = "private, no-store"
    return response


def _record_data(request, kb, tracker, row_scope, record_scope, common, *, trend_timezone=None,
                 trend_scope=None):
    """Shared read-only paging and aggregation for feed and manual tracksheets."""
    slug = kb.slug
    zone = _analysis_zone(trend_timezone or settings.TIME_ZONE)
    try:
        first, last = _time_range(request, zone)
    except (TypeError, ValueError, OverflowError):
        return JsonResponse({"ok": False, "message": "日期范围无效。"}, status=400)
    time_scope = _time_scope(first, last)
    row_scope &= time_scope
    analysis_base_scope = trend_scope if trend_scope is not None else record_scope
    analysis_scope = analysis_base_scope & time_scope

    fields = [field["label"] for field in (tracker.fields if tracker else [])]
    kind = request.GET.get("kind", "rows")
    if kind not in ("rows", "summary", "record", "trend", "options"):
        return JsonResponse({"ok": False, "message": "视图无效。"}, status=400)
    if kind == "options":
        return _analysis_options(request, tracker, analysis_scope, fields, first, last, zone,
                                 analysis_base_scope)
    if kind == "trend":
        return _trend_data(request, tracker, trend_scope if trend_scope is not None else record_scope,
                           fields, first, last,
                           str(zone))
    if kind == "record":
        try:
            row_id = int(request.GET.get("row", ""))
            if row_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "message": "记录编号无效。"}, status=400)
        row = get_object_or_404(TrackerRow.objects.filter(record_scope), pk=row_id,
                                tracker=tracker, status=TrackerRow.Status.DONE)
        response = JsonResponse({"ok": True, "row_id": row.pk, "source_id": row.external_id,
                                 "values": {key: cell(value, 2000) for key, value in row.values.items()}})
        response["Cache-Control"] = "private, no-store"
        return response
    try:
        offset = int(request.GET.get("offset", "0"))
        column_offset = int(request.GET.get("column_offset", "0"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "message": "分页参数无效。"}, status=400)
    if column_offset < 0 or column_offset >= max(1, len(fields)) or column_offset % 12:
        return JsonResponse({"ok": False, "message": "字段页码无效。"}, status=400)
    common = {**common,
        "ok": True,
        "fields": fields,
        "column_offset": column_offset,
        "timezone": str(zone),
        "range_from": first.isoformat() if first else None,
        "range_to": last.isoformat() if last else None,
    }
    if not fields:
        common.update({"kind": kind, "total_matched": 0, "rows": [], "groups": [], "has_more": False})
        response = JsonResponse(common)
        response["Cache-Control"] = "private, no-store"
        return response

    query = TrackerQuery(lambda library: library.pk == kb.pk,
                         row_scope=analysis_scope if kind == "summary" and request.GET.get("scope") == "history"
                         else row_scope)
    search = request.GET.get("q", "").strip()
    field = request.GET.get("field", "")
    value = request.GET.get("value", "").strip()
    filters = [{"field": field, "op": "contains", "value": value}] if field and value else []
    try:
        if field and field not in fields:
            raise ValueError("筛选字段无效。")
        if kind == "rows":
            result = query.lookup(kb_slug=slug, query=search, filters=filters,
                                  columns=fields[column_offset:column_offset + 12], limit=25, offset=offset,
                                  cell_chars=2000, cap=250000)
        else:
            group = request.GET.get("group", "")
            operation = request.GET.get("op", "count")
            measure = request.GET.get("measure", "")
            if group and group not in fields:
                raise ValueError("分组字段无效。")
            if operation not in ("count", "sum", "avg", "min", "max"):
                raise ValueError("统计方式无效。")
            if operation != "count" and measure not in fields:
                raise ValueError("请选择数值字段。")
            metrics = [{"op": operation, "as": "value"} if operation == "count"
                       else {"op": operation, "field": measure, "as": "value"}]
            result = query.lookup(kb_slug=slug, query=search, filters=filters,
                                  metrics=metrics, group_by=[{"field": group}] if group else [],
                                  limit=50, offset=offset, cap=250000)
    except (TypeError, ValueError, OverflowError) as exc:
        return JsonResponse({"ok": False, "message": str(exc)[:400]}, status=400)

    response = JsonResponse({**common, **result})
    response["Cache-Control"] = "private, no-store"
    return response


# 周期：单位 + 步长（每 N 个单位），单位决定 SQL 先截断到哪一层，步长决定一个周期多长。
_TREND_UNITS = {
    "minute": (TruncMinute, timedelta(days=1)),
    "hour": (TruncHour, timedelta(days=14)),
    "day": (TruncDay, timedelta(days=180)),
    "week": (TruncWeek, timedelta(days=730)),
    "month": (TruncMonth, timedelta(days=365 * 5)),
    "year": (TruncYear, timedelta(days=365 * 40)),
}
_TREND_UNIT_LABELS = {"minute": "分钟", "hour": "小时", "day": "天",
                      "week": "周", "month": "月", "year": "年"}
# 步长上限按单位收着给：避免“每分钟 × 99999”这种把 500 桶上限绕过去的输入
_TREND_SPAN_MAX = {"minute": 180, "hour": 72, "day": 92, "week": 26, "month": 60, "year": 20}
_TREND_LIMIT = 500
_TREND_SERIES_MAX = 8          # 一个对象一条曲线，超过就画不清了
# 旧的固定周期名（前端旧链接、既有测试还在用）
_LEGACY_PERIODS = {"10min": ("minute", 10), "hour": ("hour", 1), "day": ("day", 1),
                   "week": ("week", 1), "month": ("month", 1)}


def _trend_period(request):
    """解析 `period` + `span` → (单位, 步长)；无效返回 None。

    新调用传 period=minute|hour|day|week|month|year 与 span=N（每 N 个单位）；
    旧的 period=10min/hour/day/week/month 仍然接受（span 缺省按旧含义）。
    """
    raw = (request.GET.get("period") or "day").strip()
    span_raw = (request.GET.get("span") or "").strip()
    if raw in _LEGACY_PERIODS and not span_raw:
        return _LEGACY_PERIODS[raw]
    unit = _LEGACY_PERIODS[raw][0] if raw in _LEGACY_PERIODS else raw
    if unit not in _TREND_UNITS:
        return None
    try:
        span = int(span_raw or 1)
    except ValueError:
        return None
    return (unit, span) if 1 <= span <= _TREND_SPAN_MAX[unit] else None


def _trend_floor(value, unit):
    """截到该单位的起点对齐处（步长再单独按窗口锚点算）。"""
    value = value.replace(second=0, microsecond=0)
    if unit == "minute":
        return value
    if unit == "hour":
        return value.replace(minute=0)
    value = value.replace(hour=0, minute=0)
    if unit == "week":
        return value - timedelta(days=value.weekday())
    if unit == "month":
        return value.replace(day=1)
    if unit == "year":
        return value.replace(month=1, day=1)
    return value


def _trend_next(value, unit, span):
    if unit == "month":
        months = value.month - 1 + span
        return value.replace(year=value.year + months // 12, month=months % 12 + 1, day=1)
    if unit == "year":
        return value.replace(year=value.year + span, month=1, day=1)
    return value + {
        "minute": timedelta(minutes=span), "hour": timedelta(hours=span),
        "day": timedelta(days=span), "week": timedelta(weeks=span),
    }[unit]


def _trend_steps(start, end, unit, span):
    """窗口内的周期起点列表；超过 _TREND_LIMIT 个周期返回 None（调用方报 400）。

    锚点是窗口起点，所以“每 30 分钟”从当天 00:00 起就是 :00/:30；用户改了日期范围
    就等于换了锚点，这是有意的（不然同一个周期会随查询窗口移动）。
    """
    cursor, steps = _trend_floor(start, unit), []
    while cursor < end:
        steps.append(cursor)
        if len(steps) > _TREND_LIMIT:
            return None
        cursor = _trend_next(cursor, unit, span)
    return steps


def _trend_step_of(value, steps):
    """把 SQL 截出来的时间归到它所属的那个自定义周期。"""
    index = bisect.bisect_right(steps, value) - 1
    return steps[index] if index >= 0 else None


def _trend_label(value, unit):
    if unit == "year":
        return value.strftime("%Y")
    if unit == "month":
        return value.strftime("%Y-%m")
    if unit == "week":
        return value.strftime("%Y-%m-%d") + " 当周"
    if unit == "day":
        return value.strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d %H:%M")


_TREND_EVERY_ONE = {"minute": "每分钟", "hour": "每小时", "day": "每天",
                    "week": "每周", "month": "每月", "year": "每年"}


def _trend_period_label(unit, span):
    if span == 1:
        return _TREND_EVERY_ONE[unit]
    return f"每 {span} {_TREND_UNIT_LABELS[unit]}"


def _finite(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(value, 8) if math.isfinite(value) else None


def _trend_data(request, tracker, record_scope, fields, first, last, timezone_name):
    """把若干个对象按自定义周期（每 N 个单位）对比，每个对象返回一条曲线。"""
    parsed = _trend_period(request)
    if parsed is None:
        return JsonResponse({"ok": False,
                             "message": "对比周期无效：单位限分钟/小时/天/周/月/年，"
                                        "周期长度要在允许范围内。"}, status=400)
    unit, span = parsed
    operation = request.GET.get("op", "count")
    object_field = request.GET.get("object_field", "")
    measure = request.GET.get("measure", "")
    # 对象可多选：前端重复发 object_value=A&object_value=B（值里可能带逗号，所以不用分隔符）
    objects = [value for value in dict.fromkeys(request.GET.getlist("object_value"))
               if value.strip()]
    if operation not in ("count", "sum", "avg", "min", "max"):
        return JsonResponse({"ok": False, "message": "统计方式无效。"}, status=400)
    if object_field not in fields and object_field not in ("$source_id", "$document_name"):
        return JsonResponse({"ok": False, "message": "请选择有效的对象字段。"}, status=400)
    if not objects:
        return JsonResponse({"ok": False, "message": "请输入要对比的对象，最多 200 字。"}, status=400)
    if any(len(value) > 200 for value in objects):
        return JsonResponse({"ok": False, "message": "对象内容最多 200 字。"}, status=400)
    if len(objects) > _TREND_SERIES_MAX:
        return JsonResponse({"ok": False,
                             "message": f"最多同时对比 {_TREND_SERIES_MAX} 个对象，"
                                        f"当前选了 {len(objects)} 个。"}, status=400)
    if operation != "count" and measure not in fields:
        return JsonResponse({"ok": False, "message": "请选择数值字段。"}, status=400)
    zone = _analysis_zone(timezone_name)
    try:
        end = last if last else timezone.now().astimezone(zone)
        start = first if first else end - _TREND_UNITS[unit][1]
        if start >= end:
            raise ValueError
        steps = _trend_steps(start, end, unit, span)
    except (ValueError, OverflowError):
        return JsonResponse({"ok": False, "message": "日期范围无效。"}, status=400)
    if steps is None:
        return JsonResponse({"ok": False,
                             "message": f"时间范围超过 {_TREND_LIMIT} 个周期，请缩小日期范围或拉长周期。"},
                            status=400)

    rows = TrackerRow.objects.filter(
        record_scope, tracker=tracker, status=TrackerRow.Status.DONE,
        updated_at__gte=start, updated_at__lt=end,
    )
    if object_field == "$source_id":
        rows = rows.filter(external_id__in=objects).annotate(
            trend_object=Cast(F("external_id"), TextField()))
    elif object_field == "$document_name":
        rows = rows.filter(document__original_name__in=objects).annotate(
            trend_object=Cast(F("document__original_name"), TextField()))
    else:
        rows = rows.annotate(trend_object=Cast(TrackerText(object_field, "values"), TextField()))
        rows = rows.filter(trend_object__in=objects)

    truncate = _TREND_UNITS[unit][0]
    rows = rows.order_by().annotate(trend_bucket=truncate("updated_at", tzinfo=zone))
    if operation == "count":
        grouped = rows.values("trend_object", "trend_bucket").annotate(records=Count("pk"))
    else:
        rows = rows.annotate(trend_raw=Cast(TrackerText(measure, "values"), TextField()))
        rows = rows.annotate(trend_number=Case(
            When(trend_raw__regex=NUMERIC, then=Cast(F("trend_raw"), FloatField())),
            default=Value(None), output_field=FloatField(),
        ))
        grouped = rows.values("trend_object", "trend_bucket").annotate(
            records=Count("pk"), valid=Count("trend_number"),
            total=Sum("trend_number"), minimum=Min("trend_number"), maximum=Max("trend_number"),
        )

    # SQL 只截到基本单位（例如每分钟）；自定义步长在这里按窗口锚点再归一次
    buckets = {}
    for group in grouped:
        step = _trend_step_of(group["trend_bucket"].astimezone(zone), steps)
        if step is None:
            continue
        current = buckets.setdefault((group["trend_object"], step),
                                     {"records": 0, "valid": 0, "total": 0.0,
                                      "minimum": None, "maximum": None})
        current["records"] += group["records"]
        if operation != "count":
            current["valid"] += group["valid"]
            current["total"] += group["total"] or 0
            if group["minimum"] is not None:
                current["minimum"] = (group["minimum"] if current["minimum"] is None
                                      else min(current["minimum"], group["minimum"]))
            if group["maximum"] is not None:
                current["maximum"] = (group["maximum"] if current["maximum"] is None
                                      else max(current["maximum"], group["maximum"]))

    def value_of(data):
        if data is None:
            return None
        if operation == "count":
            return data["records"]
        if not data["valid"]:
            return None
        if operation == "sum":
            return _finite(data["total"])
        if operation == "avg":
            return _finite(data["total"] / data["valid"])
        return _finite(data["minimum" if operation == "min" else "maximum"])

    series = []
    for name in objects:
        points, previous = [], None
        for cursor in steps:
            data = buckets.get((name, cursor))
            value = value_of(data)
            change = _finite(value - previous) if value is not None and previous is not None else None
            if change is not None and previous != 0:
                change_pct, comparison = _finite(change / abs(previous) * 100), "available"
            elif value is None:
                change_pct, comparison = None, "no_value"
            elif previous is None:
                change_pct, comparison = None, "no_previous_value"
            else:
                change_pct, comparison = None, "zero_baseline"
            points.append({
                "bucket": cursor.isoformat(), "label": _trend_label(cursor, unit),
                "value": value, "records": data["records"] if data else 0,
                "valid_numeric_values": data["valid"] if data and operation != "count" else None,
                "change": change, "change_pct": change_pct, "comparison": comparison,
            })
            previous = value
        series.append({"object_value": name, "points": points,
                       "records": sum(point["records"] for point in points)})

    response = JsonResponse({
        "ok": True, "kind": "trend", "period": unit, "period_unit": unit, "span": span,
        "period_label": _trend_period_label(unit, span), "operation": operation,
        "object_field": object_field, "object_value": objects[0], "object_values": objects,
        "series": series, "points": series[0]["points"], "bucket_count": len(series[0]["points"]),
        "measure": measure, "timezone": str(zone),
        "range_from": start.isoformat(), "range_to": end.isoformat(),
        "span_max": _TREND_SPAN_MAX[unit], "series_max": _TREND_SERIES_MAX,
        "notice": "按记录更新时间比较已完成记录；缺失周期不补零。覆盖更新的数据源只保留当前快照。",
    })
    response["Cache-Control"] = "private, no-store"
    return response
