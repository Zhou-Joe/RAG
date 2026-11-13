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


def attach_value_card(result, *, columns, metrics, group_by, order_by, limit, offset,
                      title='', unit='', cap=10000):
    """Never turn a sample, grouped summary, missing value or arbitrary first row into a KPI."""
    from .tracker_query import encode

    value = None
    label = ''
    if result.get('kind') == 'tracker_summary':
        groups = result.get('groups', [])
        if group_by or not metrics or len(metrics) != 1 or len(groups) != 1:
            return result
        rule = metrics[0]
        name = rule.get('as', rule.get('op'))
        value = _number(groups[0].get('metrics', {}).get(name))
        field, operation = rule.get('field', ''), rule.get('op', '')
        label = name if name not in ('count', 'sum', 'avg', 'min', 'max', 'value') else (
            {'count': 'Count', 'distinct_count': 'Distinct count', 'sum': 'Total',
             'avg': 'Average', 'min': 'Minimum', 'max': 'Maximum'}.get(operation, operation)
            + (f' · {field}' if field else ''))
    elif result.get('kind') == 'tracker_rows':
        rows = result.get('rows', [])
        if (not isinstance(columns, list) or len(columns) != 1 or len(rows) != 1 or offset
                or not (result.get('total_matched') == 1 or limit == 1 and order_by)):
            return result
        label = columns[0]
        value = _number(rows[0].get('values', {}).get(label))
    if value is None:
        return result
    chart = {'type': 'metric', 'title': title or label, 'value': value}
    if unit:
        chart['unit'] = unit
    candidate = {**result, 'chart': chart, 'presentation': 'value_card',
                 'presentation_notice': 'This value is already displayed as a compact card above the answer. '
                                        'For a simple exact-answer question, return only the assigned [N] citation; '
                                        'the card is the answer. Do not add prose, insights, caveats, a table, or repeat the value. '
                                        'Explain further only when the user asks for explanation or the answer needs clarification.'}
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
