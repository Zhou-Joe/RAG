"""Deterministic, compact delivery presentation over the complete report output."""
from __future__ import annotations

import json
import re


MAX_TITLE_CHARS = 120
MAX_CARD_FIELDS = 3
# Leave room for the card heading, actions, and an explicitly requested digest.
MAX_FACT_BYTES = 16000


def report_title(value, fallback):
    title = fallback if value is None else value
    if (not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE_CHARS
            or re.search(r'[\x00-\x1f\x7f]', title)):
        raise ValueError('报告标题需为简洁的单行文字，最多 120 字符。')
    return title.strip()


def normalize_teams_card(value, headers):
    if not isinstance(value, dict) or set(value) != {'fields'}:
        raise ValueError('Teams 卡片需用 fields 选择最多三个报告输出字段。')
    fields = value['fields']
    if (not isinstance(fields, list) or not 1 <= len(fields) <= MAX_CARD_FIELDS
            or any(not isinstance(field, str) or field not in headers for field in fields)
            or len(set(fields)) != len(fields)):
        raise ValueError('Teams 卡片只能选择一至三个不重复的报告输出字段。')
    return {'fields': list(fields)}


def _cell(value):
    if value is None or value == '':
        return '—'
    text = json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (dict, list)) else str(value)
    # CSV quoting keeps commas inside an item distinguishable from item separators.
    return '"' + text.replace('"', '""') + '"' if any(char in text for char in ',"\n\r') else text


def teams_card_summary(spec, headers, rows, *, max_bytes=MAX_FACT_BYTES):
    """Join every matching item, preserving row alignment across at most 3 facts.

    A large result is shortened only at whole-row boundaries, with an explicit
    omitted count. The complete, unmodified report remains in the linked file.
    """
    fields = normalize_teams_card(spec['teams_card'], headers)['fields'] if 'teams_card' in spec else list(headers[:MAX_CARD_FIELDS])
    selected = rows
    if spec.get('trigger'):
        from .report_conditions import trigger_matching_rows
        selected = trigger_matching_rows(spec['trigger'], rows)
    values = [[] for _ in fields]
    facts = [{'title': field, 'value': ''} for field in fields]
    used = len(json.dumps(facts, ensure_ascii=False).encode('utf-8'))
    shown = 0
    for row in selected:
        cells = [_cell(row.get(field)) for field in fields]
        size = sum(len(json.dumps(cell, ensure_ascii=False).encode('utf-8')) - 2 for cell in cells)
        size += 2 * len(fields) if shown else 0
        if used + size > max_bytes:
            break
        for column, cell in zip(values, cells):
            column.append(cell)
        used += size
        shown += 1
    facts = [{'title': field, 'value': ', '.join(column)} for field, column in zip(fields, values)] if shown else []
    return {'facts': facts, 'fields': fields, 'row_count': len(selected),
            'shown_rows': shown, 'omitted_rows': len(selected) - shown}
