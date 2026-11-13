"""Bounded, permission-checked visualization payloads outside the model preview."""
from __future__ import annotations

from datetime import date, datetime, time
import json
import math
import re
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .active_fields import visible_fields
from .models import KnowledgeBase
from .publication import digest
from .tracker_query import RESPONSE_CHARS, TrackerQuery, encode, integer


KINDS = {'progress', 'sparkline', 'heatmap', 'pareto', 'timeline', 'table'}
OPERATIONS = {'count', 'sum', 'avg', 'min', 'max'}
MAX_PAYLOAD_CHARS = 250000
LOOKUP_CHARS = 500000
NAMES = {'count': 'Count', 'sum': 'Total', 'avg': 'Average', 'min': 'Minimum', 'max': 'Maximum'}


def _result_digest(kb_slug, params, chart, facts):
    """Bind citation identity to the query and complete visible result, not its summary."""
    return digest(json.dumps({'kb_slug': kb_slug, 'query': params, 'chart': chart, 'facts': facts},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))


def _string(value, name, limit=500):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'{name} must be text of up to {limit} characters.')
    return value


def _number(value, name, *, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number.')
    return value


def _parameters(params):
    if not isinstance(params, dict):
        raise ValueError('Visualization settings must be an object.')
    allowed = {'kind', 'title', 'operation', 'measure', 'x_field', 'y_field', 'label_field',
               'start_field', 'end_field', 'group_field', 'columns', 'filters', 'query',
               'target', 'unit', 'period', 'limit'}
    if not params.keys() <= allowed:
        raise ValueError('Visualization settings contain an unsupported option.')
    result = dict(params)
    kind = result.get('kind')
    if kind not in KINDS:
        raise ValueError('Use progress, sparkline, heatmap, pareto, timeline or table.')
    for name in ('title', 'measure', 'x_field', 'y_field', 'label_field', 'start_field', 'end_field', 'group_field', 'query', 'unit'):
        result[name] = _string(result.get(name, ''), name, 80 if name == 'unit' else 300 if name == 'query' else 500)
    result['operation'] = result.get('operation', 'count')
    if result['operation'] not in OPERATIONS:
        raise ValueError('Use count, sum, avg, min or max.')
    result['period'] = result.get('period', 'day')
    if result['period'] not in ('day', 'month', 'year'):
        raise ValueError('Sparkline period must be day, month or year.')
    result['limit'] = integer(result.get('limit', 100), 'limit', 1, 500)
    if result['operation'] != 'count' and not result['measure'] and kind not in ('table', 'timeline'):
        raise ValueError('A numeric measure field is required for this operation.')
    if kind == 'progress':
        target = _number(result.get('target'), 'target')
        if target <= 0:
            raise ValueError('A progress target must be greater than zero.')
        result['target'] = target
    if kind in ('sparkline', 'pareto', 'heatmap') and not result['x_field']:
        raise ValueError('An actual x_field is required for this visualization.')
    if kind == 'heatmap' and (not result['y_field'] or result['x_field'] == result['y_field']):
        raise ValueError('A heatmap needs two distinct grouping fields.')
    if kind == 'pareto' and result['operation'] not in ('count', 'sum'):
        raise ValueError('Pareto charts require an additive count or sum.')
    if kind == 'timeline' and not all(result[name] for name in ('label_field', 'start_field', 'end_field')):
        raise ValueError('A timeline requires label_field, start_field and end_field.')
    if result.get('filters') is None:
        result['filters'] = []
    return result


def _metric(params):
    return {'op': params['operation'], 'as': 'value', **({'field': params['measure']} if params['operation'] != 'count' else {})}


def _label(value):
    if value is None:
        return ''
    text = str(value)
    if len(text) > 500:
        raise ValueError('A visualization label is too long; choose a shorter label field.')
    return text


def _category_key(value):
    return encode([type(value).__name__, value])


def _category_axis(values):
    """Keep SQL category identities distinct even when display text collides.

    Reserve literal source labels first so a real '(Missing)' value keeps its
    original wording. Generated missing/blank labels get an unambiguous suffix
    when needed; this never combines the underlying aggregates.
    """
    originals = list(dict.fromkeys(_category_key(value) for value in values))
    source = {_category_key(value): value for value in values}
    reserved = {_label(value) for value in values if value is not None and value != ''}
    labels, positions, used = [], {}, set()
    for identity in originals:
        value = source[identity]
        generated = value is None or value == ''
        base = '(Missing)' if value is None else '(Blank)' if value == '' else _label(value)
        label, suffix = base, 1
        while label in used or generated and label in reserved:
            suffix += 1
            ending = f' [{suffix}]'
            label = base[:500-len(ending)] + ending
        positions[identity] = len(labels)
        labels.append(label); used.add(label)
    return labels, positions


def _lookup(service, slug, params, **extra):
    return service.lookup(kb_slug=slug, query=params['query'], filters=params['filters'],
                          status='done', cap=LOOKUP_CHARS, _export=True, **extra)


def _full_groups(service, slug, params, group_by, maximum):
    data = _lookup(service, slug, params, metrics=[_metric(params)], group_by=group_by, limit=maximum + 1)
    if data.get('has_more') or data['total_groups'] > maximum or len(data['groups']) > maximum:
        raise ValueError(f'This visualization supports at most {maximum} groups; narrow the query.')
    if not data['groups']:
        raise ValueError('No completed data matches this visualization query.')
    return data


def _epoch(value, name):
    if not isinstance(value, str):
        raise ValueError(f'{name} must contain valid ISO dates or date-times.')
    parsed = None
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            parsed = datetime.combine(date.fromisoformat(value), time.min)
        else:
            parsed = parse_datetime(value)
    except (ValueError, TypeError, OverflowError):
        pass
    if parsed is None:
        raise ValueError(f'{name} must contain valid ISO dates or date-times.')
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, ZoneInfo(settings.TIME_ZONE))
    return round(parsed.timestamp() * 1000)


def _period_epoch(value, period):
    expected = {'day': r'\d{4}-\d{2}-\d{2}', 'month': r'\d{4}-\d{2}', 'year': r'\d{4}'}[period]
    if not re.fullmatch(expected, value):
        raise ValueError('Sparkline dates must use ISO business-date values.')
    day = value + ('-01-01' if period == 'year' else '-01' if period == 'month' else '')
    return _epoch(day, 'x_field')


def build_visualization(user, allowed, service, slug, params):
    """Build the exact typed chart and a small model-visible summary from saved data."""
    if user is None or not getattr(user, 'is_active', False):
        raise ValueError('A signed-in user is required to create a visualization.')
    kb = KnowledgeBase.objects.select_related('parent').filter(slug=slug).first()
    if kb is None or not allowed(kb):
        raise ValueError('Knowledge base not found or unavailable.')
    params = _parameters(params)
    kind = params['kind']
    caption = NAMES[params['operation']]
    default_title = f"{params['measure']} · {caption}".strip(' ·')
    chart = {'type': kind, 'title': params['title'] or default_title}
    if params['unit']:
        chart['unit'] = params['unit']
    if kind == 'progress':
        data = _lookup(service, slug, params, metrics=[_metric(params)], limit=1)
        value = _number(data['groups'][0]['metrics']['value'], 'value', nullable=True)
        if value is not None and value < 0:
            raise ValueError('A progress value cannot be negative.')
        chart.update(value=value, target=params['target'])
    elif kind in ('sparkline', 'pareto', 'heatmap'):
        groups = [{'field': params['x_field'], 'period': params['period'] if kind == 'sparkline' else 'value'}]
        if kind == 'heatmap':
            groups.append({'field': params['y_field']})
        data = _full_groups(service, slug, params, groups, 1024 if kind == 'heatmap' else 500 if kind == 'sparkline' else 50)
        if kind == 'heatmap':
            xs, x_positions = _category_axis([group['by'].get(params['x_field']) for group in data['groups']])
            ys, y_positions = _category_axis([group['by'].get(params['y_field']) for group in data['groups']])
            if len(xs) > 32 or len(ys) > 32:
                raise ValueError('A heatmap supports at most 32 categories per axis; narrow the query.')
            chart.update(x_labels=xs, y_labels=ys, cells=[{
                'x': x_positions[_category_key(group['by'].get(params['x_field']))],
                'y': y_positions[_category_key(group['by'].get(params['y_field']))],
                'value': _number(group['metrics']['value'], 'value', nullable=True),
            } for group in data['groups']])
        else:
            ordered = list(data['groups'])
            if kind == 'pareto':
                if any(group['metrics']['value'] is None or group['metrics']['value'] < 0 for group in ordered):
                    raise ValueError('Pareto contributions must be nonnegative numeric values.')
                ordered.sort(key=lambda group: (-group['metrics']['value'], _label(group['by'].get(params['x_field']))))
            labels = [_label(group['by'].get(params['x_field'])) for group in ordered]
            if kind == 'pareto':
                labels, _ = _category_axis([group['by'].get(params['x_field']) for group in ordered])
            chart.update(labels=labels, series=[{'label': params['measure'] or caption,
                         'values': [_number(group['metrics']['value'], 'value', nullable=kind != 'pareto') for group in ordered]}])
            if kind == 'sparkline':
                chart['x_values'] = [_period_epoch(label, params['period']) for label in labels]
    else:
        fields = (params.get('columns') if kind == 'table' else list(dict.fromkeys(
            [params[name] for name in ('label_field', 'start_field', 'end_field', 'group_field') if params[name]])))
        if kind == 'table' and fields is None:
            fields = list(dict.fromkeys(label for tracker in service.tables(slug) for label in visible_fields(tracker.kb, tracker)))[:6]
        maximum = params['limit'] if kind == 'table' else min(params['limit'], 100)
        data = _lookup(service, slug, params, columns=fields, limit=maximum, cell_chars=8000)
        if kind == 'table':
            rows = []
            for row in data['rows']:
                values = {}
                for key, value in row['values'].items():
                    if isinstance(value, dict) and value.get('value', object()) is None:
                        value = None
                    values[key] = value
                rows.append(values)
            chart.update(title=params['title'] or 'Query Results', columns=[{'key': field, 'label': field} for field in fields],
                         rows=rows, total_rows=data['total_matched'], truncated=data['has_more'])
        else:
            if data['has_more'] or data['total_matched'] > maximum or len(data['rows']) != data['total_matched']:
                raise ValueError(f'A timeline supports at most {maximum} matching tasks; narrow the query.')
            if not data['rows']:
                raise ValueError('No completed tasks match this timeline query.')
            items = []
            for row in data['rows']:
                values = row['values']
                start_value, end_value = values.get(params['start_field']), values.get(params['end_field'])
                start, end = _epoch(start_value, 'start_field'), _epoch(end_value, 'end_field')
                if end < start:
                    raise ValueError('A timeline task cannot end before it starts.')
                item = {'label': _label(values.get(params['label_field'])), 'start': start, 'end': end,
                        'start_label': start_value, 'end_label': end_value}
                if params['group_field']:
                    item['group'] = _label(values.get(params['group_field']))
                items.append(item)
            chart.update(title=params['title'] or 'Timeline', items=items)
    chart['meta'] = {'row_count': data['total_matched']}
    if len(encode(chart)) > MAX_PAYLOAD_CHARS:
        raise ValueError('Visualization data is too large; select fewer columns or narrow the query.')
    facts = {'visualization': kind, 'title': chart['title'], 'matching_rows': data['total_matched'],
             'notice': 'The inline visualization uses the permission-checked query. Only selected fields are available.'}
    if kind == 'progress':
        facts.update(value=chart['value'], target=chart['target'], unit=params['unit'])
    elif kind == 'table':
        facts.update(displayed_rows=len(chart['rows']), truncated=chart['truncated'], columns=fields)
    elif kind == 'timeline':
        facts['displayed_tasks'] = len(chart['items'])
        first = min(chart['items'], key=lambda item: item['start'])
        last = max(chart['items'], key=lambda item: item['end'])
        longest = max(chart['items'], key=lambda item: item['end'] - item['start'])
        facts.update(range_start=first['start_label'], range_end=last['end_label'],
                     longest_task={'label': longest['label'], 'duration_ms': longest['end'] - longest['start']})
    elif kind == 'heatmap':
        facts.update(x_field=params['x_field'], y_field=params['y_field'], displayed_cells=len(chart['cells']))
        ranked = sorted((cell for cell in chart['cells'] if cell['value'] is not None),
                        key=lambda cell: (-cell['value'], cell['x'], cell['y']))[:3]
        facts['highest_cells'] = [{'x': chart['x_labels'][cell['x']], 'y': chart['y_labels'][cell['y']],
                                  'value': cell['value']} for cell in ranked]
    else:
        facts['displayed_points'] = len(chart['labels'])
        if kind == 'pareto':
            total = _number(sum(chart['series'][0]['values']), 'total')
            facts.update(total=total,
                         largest_category=chart['labels'][0], largest_value=chart['series'][0]['values'][0])
            facts['top_contributors'] = [{'category': label, 'value': value,
                                         'share_pct': value / total * 100 if total else None}
                                        for label, value in list(zip(chart['labels'], chart['series'][0]['values']))[:3]]
        else:
            values = chart['series'][0]['values']
            valid = [value for value in values if value is not None]
            change = values[-1] - values[0] if values[0] is not None and values[-1] is not None else None
            if change is not None and not math.isfinite(change):
                change = None
            change_pct = change / abs(values[0]) * 100 if change is not None and values[0] else None
            if change_pct is not None and not math.isfinite(change_pct):
                change_pct = None
            facts.update(first_period=chart['labels'][0], last_period=chart['labels'][-1],
                         first_value=values[0], latest_value=values[-1], min_value=min(valid) if valid else None,
                         max_value=max(valid) if valid else None, change=change, change_pct=change_pct)
    return chart, facts, params


def agent_visualization(user, allowed, service, kb_slug, params):
    """Publish a typed result, keeping its bounded chart data out of LLM context."""
    with service.lock:
        cap = min(RESPONSE_CHARS, service.remaining)
        if cap < 1200:
            return service._budget_notice()
        chart, facts, normalized = build_visualization(user, allowed, service, kb_slug, params)
        text = encode(facts)
        if len(text) > cap:
            raise ValueError('Visualization summary exceeds the available context budget.')
        snapshot = {'kind': 'tracker', 'source': 'Business Data Visualization', 'query': {'kb_slug': kb_slug},
                    'visualization_query': normalized, 'chart': chart, 'text': text, 'response_chars': cap,
                    'result_digest': _result_digest(kb_slug, normalized, chart, facts),
                    'text_digest': digest(text), 'data_digest': digest(encode(chart))}
        # Relationship descriptions are navigation hints, never part of the
        # reproducible chart evidence or source values.
        tables = service.tables(kb_slug)
        wrapped = service.relationship_scope.attach(facts, [table.kb for table in tables], cap=cap,
            establish=facts['matching_rows'] > 0)
        result = encode(wrapped)
        if len(result) > cap:
            raise ValueError('Visualization summary exceeds the available context budget.')
        if snapshot not in service.evidence:
            service.evidence.append(snapshot)
        service.remaining -= len(result)
        return result


def validate_visualization_snapshot(snapshot, user):
    from .access import kb_accessible
    try:
        text, chart = snapshot['text'], snapshot['chart']
        cap = snapshot['response_chars']
        if (not isinstance(text, str) or type(cap) is not int or not 1200 <= cap <= RESPONSE_CHARS
                or len(text) > cap or digest(text) != snapshot['text_digest']
                or digest(encode(chart)) != snapshot['data_digest']):
            return False
        service = TrackerQuery(lambda kb: kb_accessible(user, kb))
        slug = snapshot['query']['kb_slug']
        rebuilt, facts, normalized = build_visualization(user, service.allowed, service, slug, snapshot['visualization_query'])
        return (rebuilt == chart and encode(facts) == text
                and _result_digest(slug, normalized, rebuilt, facts) == snapshot['result_digest'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
