from __future__ import annotations

"""按采集对象和采集时间分析业务参数；流式读取单列，不装载整张追踪表。"""

import math
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import Count, DateTimeField, F, Max, Q, Value
from django.db.models.functions import Coalesce, Trim
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import KbTracker, TrackerRow
from .active_fields import visible_fields
from .tracker_query import TrackerText

PERIOD_LIMIT = 120
ITEM_LIMIT = 200
METADATA_FIELDS = {'标题', '来源ID', '来源链接', '更新时间', '采集时间', '采集日期'}
NUMBER = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$')
CLOCK = re.compile(r'^(\d{1,2}):(\d{2})(?::(\d{2}))?$')


def numeric(value: str | None) -> Decimal | None:
    """只接受有限的完整数值，不把关闭/缺失/带单位原文转换成零。"""
    text = (value or '').strip()
    if len(text) > 100 or not NUMBER.fullmatch(text):
        return None
    try:
        number = Decimal(text)
        # 防止畸形的指数令 Decimal 运算或客户端绘图溢出。
        return number if number.is_finite() and number.adjusted() <= 308 and number.adjusted() >= -308 else None
    except InvalidOperation:
        return None


def clock_minutes(value: str | None) -> Decimal | None:
    """HH:MM[:SS] 时刻统一换成分钟，时刻不作可相加的时长。"""
    match = CLOCK.fullmatch((value or '').strip())
    if not match:
        return None
    hour, minute, second = (int(part or 0) for part in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return None
    return Decimal(hour * 60 + minute) + Decimal(second) / 60


def output_number(value: Decimal | None) -> float | str | None:
    """正常数值返回 JSON 数字，超大差值保留十进制文本。"""
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else str(value)


@dataclass
class Stats:
    """在线汇总一个参数，内存开销与采集次数无关。"""
    count: int = 0
    total: Decimal = Decimal(0)
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    last: Decimal | None = None
    previous: Decimal | None = None
    last_at: datetime | None = None

    def add(self, value: Decimal | None, stamp: datetime) -> None:
        if value is None:
            return
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.previous, self.last = self.last, value
        self.last_at = stamp

    @property
    def mean(self) -> Decimal | None:
        return self.total / self.count if self.count else None

    def export(self) -> dict:
        return {'valid_count': self.count, 'avg': output_number(self.mean),
                'sum': output_number(self.total) if self.count else None,
                'min': output_number(self.minimum), 'max': output_number(self.maximum),
                'latest': output_number(self.last),
                'latest_at': self.last_at.isoformat() if self.last_at else None}


@dataclass
class Period:
    """同时累计数值/时刻，分析类型由完整筛选范围内的有效值决定。"""
    start: datetime
    count: int = 0
    present: int = 0
    changes: int = 0
    latest_text: str | None = None
    numbers: Stats = field(default_factory=Stats)
    clocks: Stats = field(default_factory=Stats)
    prior_numbers: Stats | None = None
    prior_clocks: Stats | None = None

    def add(self, value: str | None, stamp: datetime, changed: bool) -> None:
        self.count += 1
        self.changes += int(changed)
        if value is not None and value.strip():
            self.present += 1
            self.latest_text = value
        self.numbers.add(numeric(value), stamp)
        self.clocks.add(clock_minutes(value), stamp)


def parameters(tracker: KbTracker) -> list[str]:
    """仅列出业务参数，系统采集日期/来源等元信息不作为分析指标。"""
    return [label for label in visible_fields(tracker.kb, tracker) if label not in METADATA_FIELDS]


def completed_rows(tracker: KbTracker):
    """分析仅使用当前表内已完成的采集数据。"""
    return tracker.rows.filter(status=TrackerRow.Status.DONE, feed__isnull=False)


def analysis_options(tracker: KbTracker, query: str = '') -> dict:
    """按稳定来源 ID 去重并限制选择器大小，不把所有历史记录传给浏览器。"""
    rows = completed_rows(tracker)
    if query:
        rows = rows.annotate(item_title=TrackerText('标题', 'values')).filter(
            Q(item_title__icontains=query) | Q(external_id__icontains=query) | Q(feed__name__icontains=query))
    # 空稳定 ID 的旧数据仍可从行内入口分析，不合并为同一对象。
    groups = list(rows.exclude(external_id='').order_by().values('feed_id', 'external_id', 'feed__name')
                  .annotate(row_id=Max('pk'), sample_count=Count('pk'))
                  .order_by('feed__name', 'external_id')[:ITEM_LIMIT + 1])
    selected = groups[:ITEM_LIMIT]
    representatives = {row.pk: row for row in tracker.rows.filter(pk__in=[g['row_id'] for g in selected])}
    fields = parameters(tracker)
    default = fields[0] if fields else ''
    for row in representatives.values():
        candidate = next((key for key in fields if numeric(str(row.values.get(key, ''))) is not None), None)
        if candidate:
            default = candidate
            break
    return {'parameters': fields, 'default_parameter': default, 'has_more': len(groups) > ITEM_LIMIT,
            'items': [{'row_id': group['row_id'], 'title': representatives[group['row_id']].record_name,
                       'source': group['feed__name'], 'sample_count': group['sample_count']}
                      for group in selected if group['row_id'] in representatives]}


def feed_zone(name: str) -> ZoneInfo:
    """兼容旧任务中无效的时区设置。"""
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return ZoneInfo(settings.TIME_ZONE)


def time_bound(text: str, zone: ZoneInfo) -> datetime | None:
    """无偏移的日期时间按该采集任务时区解释。"""
    if not text:
        return None
    try:
        value = parse_datetime(text)
    except ValueError:
        value = None
    if value is None:
        raise ValueError('日期时间格式无效，请重新选择。')
    return timezone.make_aware(value, zone) if timezone.is_naive(value) else value


def period_start(stamp: datetime, grain: str) -> datetime:
    """使用来源时区分桶，周从周一开始。"""
    if grain == 'hour':
        return stamp.replace(minute=0, second=0, microsecond=0)
    day = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
    if grain == 'week':
        return day - timedelta(days=day.weekday())
    if grain == 'month':
        return day.replace(day=1)
    if grain == 'year':
        return day.replace(month=1, day=1)
    return day


def analyze(row: TrackerRow, parameter: str, start: str = '', end: str = '', grain: str = 'day') -> dict:
    """对同一来源/稳定 ID 的全部有效采样流式计算，响应最多保留最近 120 个周期。"""
    if parameter not in parameters(row.tracker):
        raise ValueError('请选择追踪表中的业务参数。')
    if grain not in {'hour', 'day', 'week', 'month', 'year'}:
        raise ValueError('不支持的时间粒度。')
    zone = feed_zone(row.feed.timezone)
    since, until = time_bound(start, zone), time_bound(end, zone)
    if since and until and since > until:
        raise ValueError('开始时间不能晚于结束时间。')
    rows = completed_rows(row.tracker).filter(feed_id=row.feed_id, external_id=row.external_id)
    if not row.external_id:
        rows = rows.filter(pk=row.pk)
    # 采集批次时间最可靠；旧的覆盖记录回退到保存的采集时间，再回退到更新时间。
    # 唯一约束保证同对象至多一条无批次的覆盖行。先解析这条旧记录，避免 SQLite
    # strftime 截断微秒或生成 .000，导致恰好位于起止边界的采样被错误排除。
    legacy = rows.filter(feed_run__isnull=True).order_by('pk').annotate(
        saved_time=TrackerText('采集时间', 'values')).values('saved_time', 'updated_at').first()
    fallback = F('updated_at')
    if legacy:
        try:
            stamp = time_bound(legacy['saved_time'] or '', zone) or legacy['updated_at']
        except ValueError:
            stamp = legacy['updated_at']
        fallback = Value(stamp, output_field=DateTimeField())
    rows = rows.annotate(sampled_at=Coalesce(F('feed_run__started_at'),
        fallback),
        analysis_value=Trim(TrackerText(parameter, 'values')))
    if since:
        rows = rows.filter(sampled_at__gte=since)
    if until:
        rows = rows.filter(sampled_at__lte=until)
    overall = Period(start=timezone.now())
    periods: deque[Period] = deque(maxlen=PERIOD_LIMIT + 1)
    current = None
    prior_numbers = prior_clocks = None
    period_count = 0
    first_at = last_at = previous_text = None
    for observation in rows.order_by('sampled_at', 'pk').values('sampled_at', 'analysis_value').iterator(chunk_size=400):
        stamp = observation['sampled_at'].astimezone(zone)
        value = observation['analysis_value']
        key = period_start(stamp, grain)
        if current is None or key.isoformat() != current.start.isoformat():
            if current:
                if current.numbers.count:
                    prior_numbers = current.numbers
                if current.clocks.count:
                    prior_clocks = current.clocks
            current = Period(start=key, prior_numbers=prior_numbers, prior_clocks=prior_clocks)
            periods.append(current)
            period_count += 1
        present = value is not None and bool(value.strip())
        changed = present and previous_text is not None and value != previous_text
        current.add(value, stamp, changed)
        overall.add(value, stamp, changed)
        if present:
            previous_text = value
        first_at = first_at or stamp
        last_at = stamp
    kind = 'number' if overall.numbers.count else 'clock' if overall.clocks.count else 'category'
    attr = 'numbers' if kind == 'number' else 'clocks'
    stats = getattr(overall, attr)
    result_periods = []
    for bucket in periods:
        values = getattr(bucket, attr)
        prior = bucket.prior_numbers if kind == 'number' else bucket.prior_clocks
        previous = {'avg': prior.mean, 'sum': prior.total, 'min': prior.minimum,
                    'max': prior.maximum, 'latest': prior.last} if prior else None
        metrics = values.export() if kind != 'category' else {}
        comparisons = {}
        if kind != 'category':
            for metric in ('avg', 'sum', 'min', 'max', 'latest'):
                # 均值之外的指标使用同一口径进行周期比较。
                current_value = {'avg': values.mean, 'sum': values.total if values.count else None,
                                 'min': values.minimum, 'max': values.maximum, 'latest': values.last}[metric]
                prior_value = previous.get(metric) if previous else None
                delta = current_value - prior_value if current_value is not None and prior_value is not None else None
                comparisons[metric] = {'delta': output_number(delta),
                    'percent': output_number(delta / abs(prior_value) * 100) if delta is not None and prior_value and kind == 'number' else None}
        result_periods.append({'start': bucket.start.isoformat(), 'count': bucket.count,
            'present_count': bucket.present, 'changes': bucket.changes,
            'latest_text': (bucket.latest_text or '')[:500], **metrics, 'comparison': comparisons})
    summary = {'sample_count': overall.count, 'present_count': overall.present, 'changes': overall.changes,
               'latest_text': (overall.latest_text or '')[:500], 'first_at': first_at.isoformat() if first_at else None,
               'last_at': last_at.isoformat() if last_at else None}
    distribution = []
    if kind != 'category':
        summary.update(stats.export())
        delta = stats.last - stats.previous if stats.previous is not None else None
        summary['delta'] = output_number(delta)
        summary['delta_percent'] = output_number(delta / abs(stats.previous) * 100) if delta is not None and stats.previous and kind == 'number' else None
    else:
        nonempty = rows.exclude(analysis_value__isnull=True).exclude(analysis_value='')
        summary['distinct_count'] = nonempty.aggregate(count=Count('analysis_value', distinct=True))['count']
        distribution = [{'value': group['analysis_value'][:500], 'count': group['count']}
                        for group in nonempty.order_by().values('analysis_value').annotate(count=Count('pk'))
                        .order_by('-count', 'analysis_value')[:8]]
    return {'parameter': parameter, 'kind': kind, 'timezone': str(zone), 'grain': grain,
        'item': {'row_id': row.pk, 'title': row.record_name, 'source': row.feed.name,
                 'write_mode': row.feed.write_mode},
        'summary': summary, 'period_count': period_count, 'period_limit': PERIOD_LIMIT,
        'periods': result_periods[-PERIOD_LIMIT:], 'distribution': distribution,
        'truncated_periods': period_count > PERIOD_LIMIT,
        'filters': {'start': start, 'end': end}}
