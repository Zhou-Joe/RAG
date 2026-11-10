"""Read-only browsing and aggregation for an automatically collected knowledge base."""

from __future__ import annotations

from datetime import date, datetime, timedelta, time
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import UUID

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Case, Count, F, FloatField, Max, Min, Q, Sum, TextField, Value, When
from django.db.models.functions import Cast, TruncDay, TruncHour, TruncMinute, TruncMonth, TruncWeek
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


def _record_data(request, kb, tracker, row_scope, record_scope, common, *, trend_timezone=None,
                 trend_scope=None):
    """Shared read-only paging and aggregation for feed and manual tracksheets."""
    slug = kb.slug
    first = request.GET.get("from", "")
    last = request.GET.get("to", "")
    try:
        if first:
            first = date.fromisoformat(first)
            row_scope &= Q(updated_at__date__gte=first)
        if last:
            last = date.fromisoformat(last)
            row_scope &= Q(updated_at__date__lte=last)
        if first and last and first > last:
            raise ValueError
    except ValueError:
        return JsonResponse({"ok": False, "message": "日期范围无效。"}, status=400)

    fields = [field["label"] for field in (tracker.fields if tracker else [])]
    kind = request.GET.get("kind", "rows")
    if kind not in ("rows", "summary", "record", "trend"):
        return JsonResponse({"ok": False, "message": "视图无效。"}, status=400)
    if kind == "trend":
        return _trend_data(request, tracker, trend_scope if trend_scope is not None else record_scope,
                           fields, first, last,
                           trend_timezone or settings.TIME_ZONE)
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
    }
    if not fields:
        common.update({"kind": kind, "total_matched": 0, "rows": [], "groups": [], "has_more": False})
        response = JsonResponse(common)
        response["Cache-Control"] = "private, no-store"
        return response

    query = TrackerQuery(lambda library: library.pk == kb.pk, row_scope=row_scope)
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


_TREND_PERIODS = {
    "10min": (TruncMinute, timedelta(days=1)),
    "hour": (TruncHour, timedelta(days=14)),
    "day": (TruncDay, timedelta(days=180)),
    "week": (TruncWeek, timedelta(days=730)),
    "month": (TruncMonth, timedelta(days=365 * 5)),
}
_TREND_LIMIT = 500


def _trend_floor(value, period):
    value = value.replace(second=0, microsecond=0)
    if period == "10min":
        return value.replace(minute=value.minute // 10 * 10)
    if period == "hour":
        return value.replace(minute=0)
    value = value.replace(hour=0, minute=0)
    if period == "week":
        return value - timedelta(days=value.weekday())
    if period == "month":
        return value.replace(day=1)
    return value


def _trend_next(value, period):
    if period == "month":
        return value.replace(year=value.year + 1, month=1) if value.month == 12 else value.replace(month=value.month + 1)
    return value + {
        "10min": timedelta(minutes=10), "hour": timedelta(hours=1),
        "day": timedelta(days=1), "week": timedelta(days=7),
    }[period]


def _trend_label(value, period):
    if period == "month":
        return value.strftime("%Y-%m")
    if period == "week":
        return value.strftime("%Y-%m-%d") + " 当周"
    if period == "day":
        return value.strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d %H:%M")


def _finite(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(value, 8) if math.isfinite(value) else None


def _trend_data(request, tracker, record_scope, fields, first, last, timezone_name):
    """Compare one exact entity across collection-time buckets over all saved snapshots."""
    period = request.GET.get("period", "day")
    operation = request.GET.get("op", "count")
    object_field = request.GET.get("object_field", "")
    object_value = request.GET.get("object_value", "").strip()
    measure = request.GET.get("measure", "")
    if period not in _TREND_PERIODS or operation not in ("count", "sum", "avg", "min", "max"):
        return JsonResponse({"ok": False, "message": "对比周期或统计方式无效。"}, status=400)
    if object_field not in fields and object_field not in ("$source_id", "$document_name"):
        return JsonResponse({"ok": False, "message": "请选择有效的对象字段。"}, status=400)
    if not object_value or len(object_value) > 200:
        return JsonResponse({"ok": False, "message": "请输入要对比的对象，最多 200 字。"}, status=400)
    if operation != "count" and measure not in fields:
        return JsonResponse({"ok": False, "message": "请选择数值字段。"}, status=400)
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone = ZoneInfo(settings.TIME_ZONE)
    try:
        end = (datetime.combine(last + timedelta(days=1), time.min, tzinfo=zone)
               if last else timezone.now().astimezone(zone))
        start = (datetime.combine(first, time.min, tzinfo=zone)
                 if first else end - _TREND_PERIODS[period][1])
        if start >= end:
            raise ValueError
        cursor = _trend_floor(start, period)
        bucket_count = 0
        while cursor < end:
            bucket_count += 1
            if bucket_count > _TREND_LIMIT:
                return JsonResponse({"ok": False, "message": "时间范围超过 500 个周期，请缩小日期范围或选择更长的周期。"}, status=400)
            cursor = _trend_next(cursor, period)
    except (ValueError, OverflowError):
        return JsonResponse({"ok": False, "message": "日期范围无效。"}, status=400)

    rows = TrackerRow.objects.filter(
        record_scope, tracker=tracker, status=TrackerRow.Status.DONE,
        updated_at__gte=start, updated_at__lt=end,
    )
    if object_field == "$source_id":
        rows = rows.filter(external_id=object_value)
    elif object_field == "$document_name":
        rows = rows.filter(document__original_name=object_value)
    else:
        rows = rows.annotate(trend_object=Cast(TrackerText(object_field, "values"), TextField())).filter(trend_object=object_value)

    truncate = _TREND_PERIODS[period][0]
    rows = rows.order_by().annotate(trend_bucket=truncate("updated_at", tzinfo=zone))
    if operation == "count":
        grouped = rows.values("trend_bucket").annotate(records=Count("pk")).order_by("trend_bucket")
    else:
        rows = rows.annotate(trend_raw=Cast(TrackerText(measure, "values"), TextField()))
        rows = rows.annotate(trend_number=Case(
            When(trend_raw__regex=NUMERIC, then=Cast(F("trend_raw"), FloatField())),
            default=Value(None), output_field=FloatField(),
        ))
        grouped = rows.values("trend_bucket").annotate(
            records=Count("pk"), valid=Count("trend_number"),
            total=Sum("trend_number"), minimum=Min("trend_number"), maximum=Max("trend_number"),
        ).order_by("trend_bucket")

    buckets = {}
    for group in grouped:
        key = _trend_floor(group["trend_bucket"].astimezone(zone), period)
        current = buckets.setdefault(key, {"records": 0, "valid": 0, "total": 0.0,
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

    points = []
    if buckets or first or last:
        cursor = _trend_floor(start, period) if first or last else min(buckets)
        final = _trend_floor(end - timedelta(microseconds=1), period) if first or last else max(buckets)
        previous = None
        while cursor <= final:
            data = buckets.get(cursor)
            if data is None:
                value = None
            elif operation == "count":
                value = data["records"]
            elif not data["valid"]:
                value = None
            elif operation == "sum":
                value = _finite(data["total"])
            elif operation == "avg":
                value = _finite(data["total"] / data["valid"])
            else:
                value = _finite(data["minimum" if operation == "min" else "maximum"])
            change = _finite(value - previous) if value is not None and previous is not None else None
            if change is not None and previous != 0:
                change_pct = _finite(change / abs(previous) * 100)
                comparison = "available"
            elif value is None:
                change_pct = None
                comparison = "no_value"
            elif previous is None:
                change_pct = None
                comparison = "no_previous_value"
            else:
                change_pct = None
                comparison = "zero_baseline"
            points.append({
                "bucket": cursor.isoformat(), "label": _trend_label(cursor, period),
                "value": value, "records": data["records"] if data else 0,
                "valid_numeric_values": data["valid"] if data and operation != "count" else None,
                "change": change, "change_pct": change_pct, "comparison": comparison,
            })
            previous = value
            cursor = _trend_next(cursor, period)

    response = JsonResponse({
        "ok": True, "kind": "trend", "period": period, "operation": operation,
        "object_field": object_field, "object_value": object_value, "measure": measure,
        "timezone": str(zone), "range_from": start.isoformat(), "range_to": end.isoformat(),
        "points": points, "bucket_count": len(points),
        "notice": "按记录更新时间比较已完成记录；缺失周期不补零。覆盖更新的数据源只保留当前快照。",
    })
    response["Cache-Control"] = "private, no-store"
    return response
