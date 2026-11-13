"""Compact cards derived from one explicitly queried, verified business value."""
from __future__ import annotations

import math
import re


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str) and not re.fullmatch(
            r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value.strip()):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


PRESENTATION_NOTICE = ('This value is displayed as a compact card above the answer. For a simple exact-answer '
                       'question, return only the assigned [N] citation; do not repeat it in prose or add unrequested insights.')
CONTEXT_FIELD = re.compile(r'(?:^|[ _-])(?:id|uuid|identifier|code|index|year|month|day|date|timestamp)$|'
                           r'(?:^|[ _-])(?:created|updated|collected|collection|record)[ _-](?:at|date|time)$|'
                           r'(?-i:ID|Id)$|(?:编号|编码|序号|年份|年度|月份|日期|时间戳)$|'
                           r'(?:采集|更新|创建|记录)(?:时间|日期)$', re.I)


def _chart(value, field, title, unit, *, allow_text=False):
    number = _number(value)
    if (isinstance(value, str) and re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value.strip())
            and number is None):
        raise ValueError('The selected numeric value must be finite.')
    # Identifiers and formatted answers retain their original characters when
    # explicitly selected; they are never guessed as the numeric business KPI.
    if isinstance(value, str) and CONTEXT_FIELD.search(field):
        number = None
    if number is not None:
        result = {'type': 'metric', 'title': title or field, 'value': number}
        if unit:result['unit'] = unit
        return result
    if (allow_text and isinstance(value, str) and value.strip() and len(value) <= 300
            and value.strip().casefold() not in {'nan', '+nan', '-nan', 'inf', '+inf', '-inf',
                                               'infinity', '+infinity', '-infinity'}):
        if unit:raise ValueError('For a text answer, keep any source unit in the value and leave unit empty.')
        return {'type': 'metric', 'title': title or field, 'value': value, 'value_type': 'text'}
    raise ValueError('Choose a complete numeric value or a short exact text answer from the returned result.')


def selected_value_card(result, *, field, row_id=None, group=None, title='', unit=''):
    """Select source data, never accept a model-supplied answer value."""
    if not isinstance(field, str) or not 1 <= len(field) <= 500:
        raise ValueError('Choose an actual returned field or metric name.')
    if (not isinstance(title, str) or len(title) > 160 or not isinstance(unit, str) or len(unit) > 40
            or row_id is not None and (isinstance(row_id, bool) or not isinstance(row_id, int) or row_id < 1)
            or group is not None and (not isinstance(group, dict) or len(group) > 3)):
        raise ValueError('Card title, unit or source row/group selection is invalid.')
    if result.get('kind') == 'tracker_rows':
        if group is not None or field not in result.get('columns', []):
            raise ValueError('Choose a field that was returned by this lookup.')
        rows = result.get('rows', [])
        selected = [row for row in rows if row.get('row_id') == row_id] if row_id is not None else rows
        if len(selected) != 1 or row_id is None and result.get('total_matched') != 1:
            raise ValueError('Choose the relevant returned row_id; do not use an arbitrary first row.')
        row = selected[0]
        if row.get('status') != 'done' or field not in row.get('values', {}):
            raise ValueError('Choose a returned field from an approved source row.')
        return _chart(row['values'][field], field, title, unit, allow_text=True)
    if result.get('kind') == 'tracker_summary':
        if row_id is not None:
            raise ValueError('Summary values use their metric name and optional exact group, not row_id.')
        groups = result.get('groups', [])
        if result.get('applied_query', {}).get('group_by'):
            if group is None:raise ValueError('Choose an exact returned group for this metric.')
            groups = [row for row in groups if row.get('by') == group]
        elif group:
            raise ValueError('This summary has no groups.')
        if len(groups) != 1 or field not in groups[0].get('metrics', {}):
            raise ValueError('Choose an actual returned metric and group.')
        return _chart(groups[0]['metrics'][field], field, title, unit)
    raise ValueError('First retrieve the relevant records or summary with tracker_lookup.')


def attach_value_card(result, *, columns, metrics, group_by, order_by, limit, offset,
                      title='', unit='', cap=10000, field='', row_id=None, infer=False):
    """Never turn a sample, grouped summary, missing value or arbitrary first row into a KPI."""
    from .tracker_query import encode

    value = None
    label = ''
    legacy = not infer and not field and row_id is None
    if result.get('kind') == 'tracker_summary':
        groups = result.get('groups', [])
        if group_by or not metrics or len(groups) != 1 or not field and len(metrics) != 1:
            return result
        rule = next((rule for rule in metrics if rule.get('as', rule.get('op')) == field), None) if field else metrics[0]
        if rule is None:raise ValueError('card_field must be an actual returned metric name.')
        name = rule.get('as', rule.get('op'))
        value = _number(groups[0].get('metrics', {}).get(name))
        field, operation = rule.get('field', ''), rule.get('op', '')
        label = name if name not in ('count', 'sum', 'avg', 'min', 'max', 'value') else (
            {'count': 'Count', 'distinct_count': 'Distinct count', 'sum': 'Total',
             'avg': 'Average', 'min': 'Minimum', 'max': 'Maximum'}.get(operation, operation)
            + (f' · {field}' if field else ''))
    elif result.get('kind') == 'tracker_rows':
        rows = result.get('rows', [])
        if row_id is not None:
            chart = selected_value_card(result, field=field, row_id=row_id, title=title, unit=unit)
            return _attached(result, chart, cap)
        if (len(rows) != 1 or offset or not (result.get('total_matched') == 1 or limit == 1 and order_by)):
            return result
        selected_columns = result.get('columns', [])
        if field:
            if field not in selected_columns:raise ValueError('card_field must be a returned column.')
            return _attached(result, _chart(rows[0].get('values', {}).get(field), field, title, unit, allow_text=True), cap)
        if isinstance(columns, list) and len(columns) == 1:
            label = columns[0]
        elif infer:
            candidates = [name for name in selected_columns if not CONTEXT_FIELD.search(name)
                          and _number(rows[0].get('values', {}).get(name)) is not None]
            if len(candidates) != 1:return result
            label = candidates[0]
        else:return result
        value = _number(rows[0].get('values', {}).get(label))
    if value is None:
        return result
    chart = {'type': 'metric', 'title': title or label, 'value': value}
    if unit:
        chart['unit'] = unit
    return _attached(result, chart, cap, legacy=legacy)


def _attached(result, chart, cap, *, legacy=False):
    from .tracker_query import encode
    candidate = {**result, 'chart': chart, 'presentation': 'value_card',
                 'presentation_notice': ('This value is already displayed as a compact card above the answer. '
                                        'For a simple exact-answer question, return only the assigned [N] citation; '
                                        'the card is the answer. Do not add prose, insights, caveats, a table, or repeat the value. '
                                        'Explain further only when the user asks for explanation or the answer needs clarification.') if legacy else PRESENTATION_NOTICE}
    return candidate if len(encode(candidate)) <= cap else result


def grounded_value_card(snapshot, *, title, value, unit, quote):
    """A document card repeats an exact source quantity, without unit conversion."""
    if snapshot.get('kind') != 'text' or not snapshot.get('chunk_id'):
        raise ValueError('Choose a selected text passage for this value.')
    if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 160
            or not isinstance(unit, str) or len(unit) > 40
            or not isinstance(quote, str) or not 1 <= len(quote) <= 400
            or quote not in snapshot.get('text', '')):
        raise ValueError('Use a short title, known unit and an exact quote from the selected passage.')
    number = _number(value)
    if number is None:
        raise ValueError('The card needs a finite numeric value from the source.')
    matches = re.finditer(r'(?<![A-Za-z0-9_.,])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?)(?![\d.,])', quote)
    grounded = False
    for match in matches:
        if _number(match[1].replace(',', '')) != number:
            continue
        tail = quote[match.end():].lstrip()
        if unit and (not tail.startswith(unit) or len(tail) > len(unit)
                and re.match(r'[\w°/%²³]', tail[len(unit)])):
            continue
        grounded = True
        break
    if not grounded:
        raise ValueError('The exact value and unit must appear together in the quoted source; do not convert or calculate them.')
    chart = {'type': 'metric', 'title': title.strip(), 'value': number}
    if unit:
        chart['unit'] = unit
    return {key: snapshot.get(key, '') for key in ('doc_id', 'chunk_id', 'document_digest', 'chunk_digest')} | {
        'chart': chart, 'quote': quote}


def grounded_card_current(card, snapshot):
    try:
        chart = card['chart']
        return card == grounded_value_card(snapshot, title=chart['title'], value=chart['value'],
            unit=chart.get('unit', ''), quote=card['quote'])
    except (KeyError, TypeError, ValueError):
        return False
