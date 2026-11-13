"""Typed report visuals derived only from the reviewed report's output rows."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, time
from decimal import Decimal
import json
import math
import re
from zoneinfo import ZoneInfo

from django.conf import settings


TYPES = {'progress', 'sparkline', 'heatmap', 'pareto', 'timeline', 'table'}
AGGREGATES = {'count', 'sum', 'avg', 'min', 'max'}
LIMITS = {'progress': 1, 'sparkline': 500, 'heatmap': 1024, 'pareto': 50, 'timeline': 100, 'table': 500}
_NUMERIC = re.compile(r'^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$')


def _field(value, headers, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or value not in headers:
        raise ValueError('Extended visual fields must reference actual report output columns.')
    return value


def normalize_extended(chart, headers):
    kind = chart['type']
    common = {'title', 'type', 'limit', 'sort', 'options'}
    specific = {'progress': {'series', 'target', 'unit'}, 'sparkline': {'x', 'series', 'unit'},
                'heatmap': {'x', 'y', 'series', 'unit'}, 'pareto': {'x', 'series', 'unit'},
                'timeline': {'label', 'start', 'end', 'group'}, 'table': {'columns'}}
    if not chart.keys() <= common | specific[kind]:
        raise ValueError('Extended visuals accept only field-based configuration, never code or literal data.')
    title = chart.get('title', '')
    if not isinstance(title, str) or len(title) > 120:
        raise ValueError('Extended visual titles must be text of up to 120 characters.')
    default_limit = 100 if kind == 'table' else LIMITS[kind]
    limit = chart.get('limit', default_limit)
    if type(limit) is not int or not 1 <= limit <= LIMITS[kind]:
        raise ValueError('Extended visual item limit is invalid.')
    options = chart.get('options', {})
    if not isinstance(options, dict) or not options.keys() <= {'legend'} or any(type(value) is not bool for value in options.values()):
        raise ValueError('Extended visual options cannot contain custom code or unsupported settings.')
    sort = chart.get('sort', 'value_desc' if kind == 'pareto' else 'source')
    if sort not in ('source', 'label', 'value_desc') or kind == 'pareto' and sort != 'value_desc':
        raise ValueError('Extended visual ordering is invalid; Pareto charts require descending values.')
    result = {'type': kind, 'title': title.strip(), 'limit': limit, 'sort': sort, 'options': options}
    if kind in {'progress', 'sparkline', 'heatmap', 'pareto'}:
        series = chart.get('series')
        if not isinstance(series, list) or len(series) != 1:
            raise ValueError('This visual requires exactly one aggregate series.')
        item = series[0]
        if not isinstance(item, dict) or not item.keys() <= {'field', 'aggregate', 'label'}:
            raise ValueError('Extended visual series use only a field, aggregate and optional label.')
        aggregate = item.get('aggregate', 'sum')
        field = item.get('field')
        if not isinstance(aggregate, str) or aggregate not in AGGREGATES or aggregate != 'count' and field is None:
            raise ValueError('Extended visual aggregate is invalid.')
        if field is not None:
            _field(field, headers)
        label = item.get('label')
        if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > 80):
            raise ValueError('Extended visual series labels must be text of up to 80 characters.')
        if kind == 'pareto' and aggregate not in {'count', 'sum'}:
            raise ValueError('Pareto charts require a count or sum series.')
        result['series'] = [{'aggregate': aggregate, **({'field': field} if field is not None else {}),
                             **({'label': label.strip()} if label is not None else {})}]
        unit = chart.get('unit', '')
        if not isinstance(unit, str) or len(unit) > 80:
            raise ValueError('Extended visual units must be text of up to 80 characters.')
        if unit:
            result['unit'] = unit
    if kind == 'progress':
        target = chart.get('target')
        try:
            valid_target = type(target) in (int, float) and math.isfinite(float(target)) and target > 0
        except OverflowError:
            valid_target = False
        if not valid_target:
            raise ValueError('A progress visual requires an explicit positive numeric target.')
        result['target'] = target
    elif kind in {'sparkline', 'heatmap', 'pareto'}:
        result['x'] = _field(chart.get('x'), headers)
        if kind == 'heatmap':
            result['y'] = _field(chart.get('y'), headers)
    elif kind == 'timeline':
        for key in ('label', 'start', 'end'):
            result[key] = _field(chart.get(key), headers)
        if chart.get('group') is not None:
            result['group'] = _field(chart['group'], headers)
    elif kind == 'table':
        columns = chart.get('columns', list(headers[:12]))
        if (not isinstance(columns, list) or not 1 <= len(columns) <= 12
                or any(not isinstance(field, str) for field in columns) or len(set(columns)) != len(columns)):
            raise ValueError('An interactive table requires 1 to 12 unique report fields.')
        result['columns'] = [_field(field, headers) for field in columns]
    return result


def _text(value):
    result = '' if value is None else str(value)
    if len(result) > 500:
        raise ValueError('Extended visual labels exceed 500 characters; choose shorter labels.')
    return result


def _timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('Timeline and date-axis fields must contain valid ISO dates or datetimes.') from None
    else:
        raise ValueError('Timeline and date-axis fields must contain valid ISO dates or datetimes.')
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(settings.TIME_ZONE))
    try:
        stamp = parsed.timestamp() * 1000
    except (OverflowError, OSError, ValueError):
        raise ValueError('A timeline date is outside the supported range.') from None
    if not math.isfinite(stamp) or abs(stamp) > 8.64e15:
        raise ValueError('A timeline date is outside the supported range.')
    return stamp


def _meta(rows, total=1, shown=None):
    shown = total if shown is None else shown
    return {'row_count': len(rows), 'group_count': total, 'displayed_groups': shown,
            'omitted_groups': total - shown, 'omitted_row_count': total - shown if shown < total else 0,
            'other_included': False}


def _groups(rows, fields, *, typed=False):
    grouped = OrderedDict()
    for row in rows:
        values = [row.get(field) for field in fields]
        labels = [_text(value) for value in values]
        identity = json.dumps([[type(value).__name__, value] for value in values] if typed else labels,
                              ensure_ascii=False, default=str)
        group = grouped.setdefault(identity, {'labels': labels, 'values': values, 'rows': []})
        group['rows'].append(row)
    return list(grouped.values())


def _axis_labels(groups, index, language):
    """Name null/blank categories without stealing literal source labels."""
    zh = language == 'zh-hans'
    identity = lambda value: json.dumps([type(value).__name__, value], ensure_ascii=False, default=str)
    entries = OrderedDict((identity(group['values'][index]), group['values'][index]) for group in groups)
    names, used = {}, set()
    # Reserve real nonblank text first. A source category literally named
    # "Missing (null)" must remain distinct from an actual missing value.
    for key, value in entries.items():
        if isinstance(value, str) and value.strip():
            names[key] = _text(value); used.add(names[key])
    for key, value in entries.items():
        if key in names:
            continue
        if value is None:
            base, kind = ('缺失（null）', 'null') if zh else ('Missing (null)', 'null')
        elif isinstance(value, str) and not value:
            base, kind = ('空白（空字符串）', 'empty') if zh else ('Blank (empty)', 'empty')
        elif isinstance(value, str) and not value.strip():
            base, kind = (f'空白（{len(value)}字符）', 'blank') if zh else (f'Blank ({len(value)} characters)', 'blank')
        else:
            base = _text(value)
            kind = ('布尔' if zh else 'boolean') if type(value) is bool else ('数值' if zh else 'number')
        candidate, suffix = base, 1
        while candidate in used:
            candidate = base + ' [' + kind + (f' {suffix}' if suffix > 1 else '') + ']'
            suffix += 1
        names[key] = candidate; used.add(candidate)
    order = list(entries)
    return [names[key] for key in order], {key: position for position, key in enumerate(order)}, identity


def build_extended(chart, rows, language, stat):
    kind = chart['type']
    titles = {'progress': ('目标进度', 'Progress to Target'), 'sparkline': ('趋势', 'Trend'),
              'heatmap': ('热力图', 'Heatmap'), 'pareto': ('主要贡献因素', 'Pareto Contributions'),
              'timeline': ('时间安排', 'Timeline'), 'table': ('数据明细', 'Data Table')}
    result = {'type': kind, 'title': chart['title'] or titles[kind][language != 'zh-hans'], 'language': language}
    if chart.get('unit'):
        result['unit'] = chart['unit']
    if kind == 'progress':
        value, _ = stat(rows, chart['series'][0])
        if value is not None and value < 0:
            raise ValueError('Progress values cannot be negative.')
        return {**result, 'value': value, 'target': chart['target'], 'meta': _meta(rows)}
    if kind == 'timeline':
        if len(rows) > chart['limit']:
            raise ValueError('The timeline exceeds its item limit; narrow the report range.')
        items = []
        for row in rows:
            start, end = _timestamp(row.get(chart['start'])), _timestamp(row.get(chart['end']))
            if end < start:
                raise ValueError('Timeline end dates cannot precede start dates.')
            item = {'label': _text(row.get(chart['label'])), 'start': start, 'end': end,
                    'start_label': _text(row.get(chart['start'])), 'end_label': _text(row.get(chart['end']))}
            if chart.get('group'):
                item['group'] = _text(row.get(chart['group']))
            items.append(item)
        items.sort(key=lambda item: (item['start'], item['end'], item['label']))
        return {**result, 'items': items, 'meta': _meta(rows, len(items))}
    if kind == 'table':
        count = 0
        def cell(value):
            nonlocal count
            if value is None or type(value) is bool or type(value) is int and abs(value) <= 9007199254740991:
                return value
            if type(value) is float:
                if not math.isfinite(value):
                    raise ValueError('Interactive table values must be finite.')
                return value
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False, default=str)
            elif not isinstance(value, str):
                value = str(value)
            count += min(len(value), 2000)
            if count > 200000:
                raise ValueError('The interactive table is too large; reduce its rows or columns.')
            return value if len(value) <= 2000 else {'text': value[:2000], 'truncated': True, 'original_chars': len(value)}
        shown = rows[:chart['limit']]
        return {**result, 'columns': [{'key': key, 'label': key} for key in chart['columns']],
                'rows': [{key: cell(row.get(key)) for key in chart['columns']} for row in shown],
                'total_rows': len(rows), 'truncated': len(shown) < len(rows), 'meta': _meta(rows, len(rows), len(shown))}
    definition = chart['series'][0]
    if kind == 'sparkline':
        grouped = {}
        numeric = None
        for row in rows:
            raw = row.get(chart['x'])
            if type(raw) in (int, float, Decimal) or isinstance(raw, str) and _NUMERIC.fullmatch(raw.strip()):
                stamp = float(raw)
                mode = True
            else:
                stamp = _timestamp(raw)
                mode = False
            if not math.isfinite(stamp) or numeric is not None and numeric != mode:
                raise ValueError('Sparkline axes must use consistent finite numeric positions or ISO dates.')
            numeric = mode
            group = grouped.setdefault(stamp, {'label': _text(raw), 'rows': []})
            group['rows'].append(row)
        positions = sorted(grouped)
        if len(positions) > chart['limit']:
            raise ValueError('The sparkline exceeds its point limit; narrow or aggregate the report dates.')
        values = [stat(grouped[position]['rows'], definition)[0] for position in positions]
        return {**result, 'labels': [grouped[position]['label'] for position in positions], 'x_values': positions,
                'series': [{'label': definition.get('label') or definition.get('field') or 'Count', 'values': values}],
                'meta': _meta(rows, len(positions))}
    groups = _groups(rows, [chart['x'], chart['y']] if kind == 'heatmap' else [chart['x']], typed=kind == 'heatmap')
    for group in groups:
        group['value'] = stat(group['rows'], definition)[0]
    if kind == 'heatmap':
        x_labels, x_positions, x_identity = _axis_labels(groups, 0, language)
        y_labels, y_positions, y_identity = _axis_labels(groups, 1, language)
        if len(x_labels) > 32 or len(y_labels) > 32 or len(groups) > chart['limit']:
            raise ValueError('The heatmap exceeds its axis or cell limit; narrow the report range.')
        cells = [{'x': x_positions[x_identity(group['values'][0])], 'y': y_positions[y_identity(group['values'][1])],
                  'value': group['value']} for group in groups]
        return {**result, 'x_labels': x_labels, 'y_labels': y_labels, 'cells': cells,
                'meta': _meta(rows, len(groups))}
    if len(groups) > chart['limit']:
        raise ValueError('The Pareto chart exceeds its category limit; narrow the report range.')
    if any(group['value'] is None or group['value'] < 0 for group in groups):
        raise ValueError('Pareto contributions must be nonnegative numeric counts or sums.')
    groups.sort(key=lambda group: (-group['value'], group['labels'][0]))
    return {**result, 'labels': [group['labels'][0] for group in groups],
            'series': [{'label': definition.get('label') or definition.get('field') or 'Count',
                        'values': [group['value'] for group in groups]}], 'meta': _meta(rows, len(groups))}
