"""Bounded, deterministic conditions over a report's transformed output."""
from __future__ import annotations

import math
from datetime import datetime, timedelta


def normalize_trigger(value: dict, headers: list[str]) -> dict:
    """Validate a declarative trigger; no generated Python or model calls run here."""
    if not isinstance(value, dict) or set(value) - {'condition', 'match', 'notify', 'cooldown_minutes'}:
        raise ValueError('触发规则格式无效。')
    match = value.get('match', 'any')
    notify = value.get('notify', 'on_enter')
    cooldown = value.get('cooldown_minutes', 0)
    if match not in ('any', 'all') or notify not in ('on_enter', 'every_match'):
        raise ValueError('请选择有效的条件匹配方式和重复通知方式。')
    if type(cooldown) is not int or not 0 <= cooldown <= 525600:
        raise ValueError('通知冷却时间需为0至525600分钟。')
    count = 0

    def clean(node: dict, depth: int = 0) -> dict:
        nonlocal count
        count += 1
        if count > 40 or depth > 4 or not isinstance(node, dict):
            raise ValueError('触发条件过多或嵌套过深。')
        groups = set(node) & {'all', 'any'}
        if groups:
            if len(groups) != 1 or len(node) != 1:
                raise ValueError('触发条件组合格式无效。')
            key = next(iter(groups))
            children = node[key]
            if not isinstance(children, list) or not 1 <= len(children) <= 20:
                raise ValueError('每组触发条件需包含1至20项。')
            return {key: [clean(child, depth + 1) for child in children]}
        if set(node) - {'field', 'metric', 'op', 'value'} or ('field' in node) == ('metric' in node):
            raise ValueError('触发条件需引用一个报告输出字段或记录数。')
        if 'field' in node:
            if not isinstance(node['field'], str) or node['field'] not in headers:
                raise ValueError('触发条件引用了未知报告输出字段。')
            target = {'field': node['field']}
        else:
            if node['metric'] != 'row_count':
                raise ValueError('不支持的触发统计指标。')
            target = {'metric': 'row_count'}
        op = node.get('op')
        if op not in ('eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'is_missing', 'is_present'):
            raise ValueError('触发条件的比较方式无效。')
        if op in ('is_missing', 'is_present'):
            if 'value' in node or 'metric' in node:
                raise ValueError('缺失值判断只能用于报告字段，且不需要比较值。')
            return {**target, 'op': op}
        expected = node.get('value')
        if not isinstance(expected, (str, int, float, bool)) or isinstance(expected, str) and len(expected) > 500:
            raise ValueError('触发条件的比较值无效。')
        if isinstance(expected, float) and not math.isfinite(expected):
            raise ValueError('触发条件的比较值必须是有限数值。')
        if 'metric' in node and (type(expected) not in (int, float) or expected < 0 or op == 'contains'):
            raise ValueError('记录数条件需使用非负数值。')
        if op in ('gt', 'gte', 'lt', 'lte') and type(expected) not in (int, float):
            raise ValueError('大小比较需使用数值；请先用报告计算规则转换日期或文本。')
        if op == 'contains' and not isinstance(expected, str):
            raise ValueError('包含条件需使用文本。')
        return {**target, 'op': op, 'value': expected}

    return {'condition': clean(value.get('condition')), 'match': match,
            'notify': notify, 'cooldown_minutes': cooldown}


def _missing(value) -> bool:
    return value is None or value == '' or isinstance(value, float) and not math.isfinite(value)


def _compare(actual, condition: dict) -> bool:
    op = condition['op']
    if op == 'is_missing':
        return _missing(actual)
    if op == 'is_present':
        return not _missing(actual)
    if _missing(actual):
        return False
    expected = condition['value']
    if op == 'contains':
        return isinstance(actual, str) and expected.casefold() in actual.casefold()
    if type(expected) in (int, float):
        if isinstance(actual, bool):
            return False
        try:
            actual = float(actual)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(actual):
            return False
    elif type(expected) is bool:
        if type(actual) is not bool:
            return False
    elif not isinstance(actual, str):
        return False
    return {'eq': lambda: actual == expected, 'ne': lambda: actual != expected,
            'gt': lambda: actual > expected, 'gte': lambda: actual >= expected,
            'lt': lambda: actual < expected, 'lte': lambda: actual <= expected}[op]()


def _trigger_matches(trigger: dict, rows: list[dict]):
    """Use one predicate for both notification decisions and item presentation."""
    def evaluate(node: dict, row: dict | None) -> bool:
        if 'all' in node:
            return all(evaluate(child, row) for child in node['all'])
        if 'any' in node:
            return any(evaluate(child, row) for child in node['any'])
        if 'metric' in node:
            return _compare(len(rows), node)
        # An empty dataset never satisfies field/all-row conditions vacuously.
        return row is not None and _compare(row.get(node['field']), node)

    matches = [evaluate(trigger['condition'], row) for row in rows]
    matched = (all(matches) if trigger['match'] == 'all' else any(matches)) if matches else evaluate(trigger['condition'], None)
    return matches, matched


def trigger_matching_rows(trigger: dict, rows: list[dict]) -> list[dict]:
    matches, matched = _trigger_matches(trigger, rows)
    return [row for row, included in zip(rows, matches) if included] if matched else []


def evaluate_trigger(trigger: dict, rows: list[dict]) -> dict:
    """Evaluate validated conditions after date filters, calculations and aggregation."""
    matches, matched = _trigger_matches(trigger, rows)
    return {'matched': matched, 'row_count': len(rows), 'matched_rows': sum(matches),
            'reason': 'condition_met' if matched else 'condition_not_met'}


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo else None
    except (TypeError, ValueError):
        return None


def claim_trigger(plan, delivery, trigger: dict, evaluation: dict, at) -> str:
    """Serialize a check and reserve its notification without holding a lock over HTTP."""
    from django.db import transaction
    from django.db.models import Q
    from django.utils import timezone
    from .models import FeedReportPlan

    with transaction.atomic():
        current = FeedReportPlan.objects.select_for_update().get(pk=plan.pk)
        if not current.enabled or current.updated_at != plan.updated_at:
            return 'configuration_changed'
        if not current.deliveries.filter(pk=delivery.pk, status='preparing').exists():
            return 'check_cancelled'
        if current.deliveries.filter(scheduled_for__isnull=False, status__in=['preparing', 'sending']).exclude(pk=delivery.pk).filter(Q(scheduled_for__lt=at) | Q(status='sending')).exists():
            return 'check_in_progress'
        state = dict(current.trigger_state or {})
        previous = _stamp(state.get('last_evaluated_for'))
        if previous is not None and at <= previous:
            return 'stale_check'
        notified = bool(state.get('notified_for_match')) if state.get('matched') else False
        matched = evaluation['matched']
        state.update(last_evaluated_for=at.isoformat(), matched=matched,
                     notified_for_match=notified if matched else False,
                     row_count=evaluation['row_count'], matched_rows=evaluation['matched_rows'])
        state.pop('reserved_delivery', None)
        reason = ''
        if not matched:
            reason = 'condition_not_met'
        elif trigger['notify'] == 'on_enter' and notified:
            reason = 'already_notified'
        else:
            last = _stamp(state.get('last_notified_at'))
            if last and timezone.now() < last + timedelta(minutes=trigger['cooldown_minutes']):
                reason = 'cooldown'
        if not reason:
            state['reserved_delivery'] = str(delivery.pk)
        state['last_reason'] = reason or 'condition_met'
        if not FeedReportPlan.objects.filter(pk=current.pk, trigger_state=current.trigger_state).update(trigger_state=state):
            return 'check_in_progress'
        return reason


def start_trigger_send(plan, delivery) -> None:
    """Latch before the external request: an ambiguous HTTP outcome must not retry."""
    from django.db import transaction
    from django.utils import timezone
    from .models import FeedReportPlan

    with transaction.atomic():
        current = FeedReportPlan.objects.select_for_update().get(pk=plan.pk)
        state = dict(current.trigger_state or {})
        if not current.enabled or current.updated_at != plan.updated_at or state.get('reserved_delivery') != str(delivery.pk):
            raise ValueError('触发报告配置已变更或停用，本次发送已取消。')
        state.pop('reserved_delivery', None)
        state.update(notified_for_match=True, last_notified_at=timezone.now().isoformat(), last_reason='sending')
        if not current.deliveries.filter(pk=delivery.pk, status='preparing').update(status='sending'):
            raise ValueError('触发检查已取消，本次未发送。')
        if not FeedReportPlan.objects.filter(pk=current.pk, trigger_state=current.trigger_state).update(trigger_state=state):
            raise ValueError('触发检查状态已变更，本次未发送。')


def release_trigger(plan, delivery) -> None:
    """A preparation failure may be checked again; never release an HTTP send latch."""
    from django.db import transaction
    from .models import FeedReportPlan

    with transaction.atomic():
        current = FeedReportPlan.objects.select_for_update().filter(pk=plan.pk).first()
        if current is None:
            return
        state = dict(current.trigger_state or {})
        if state.get('reserved_delivery') == str(delivery.pk):
            state.pop('reserved_delivery')
            FeedReportPlan.objects.filter(pk=current.pk, trigger_state=current.trigger_state).update(trigger_state=state)
