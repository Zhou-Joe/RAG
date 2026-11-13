"""Offline report charts from reviewed fields and deterministic full-result data.

One small SVG runtime renders every standard or EnGenie-configured chart. Chart
definitions contain field references and typed options, never executable code.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from html import escape
import json
import math
from pathlib import Path
import re

TYPES = {'metric', 'line', 'bar', 'pie', 'combo', 'area', 'scatter', 'doughnut'}
AGGREGATES = {'count', 'sum', 'avg', 'min', 'max'}
MAX_CHARTS = 8
MAX_SERIES = 6
MAX_POINTS = 100
_NUMBER = re.compile(r'^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$')


def normalize_charts(value, headers):
    if not isinstance(value, list) or len(value) > MAX_CHARTS:
        raise ValueError('HTML 报告图表必须为最多 8 个配置的列表。')
    if (not isinstance(headers, (list, tuple)) or len(headers) > 30
            or any(not isinstance(field, str) or not field or len(field) > 80 for field in headers)
            or len(set(headers)) != len(headers)):
        raise ValueError('HTML 图表需要有效的报告输出字段。')
    output = []
    for chart in value:
        if not isinstance(chart, dict) or not chart.keys() <= {'title', 'type', 'x', 'series', 'limit', 'sort', 'options'}:
            raise ValueError('HTML 图表仅接受字段与样式配置，不能包含脚本或直接数据。')
        kind = chart.get('type')
        title = chart.get('title', '')
        if not isinstance(kind, str) or kind not in TYPES or not isinstance(title, str) or len(title) > 120:
            raise ValueError('HTML 图表类型或标题无效。')
        horizontal = chart.get('x')
        if horizontal is not None and horizontal not in headers:
            raise ValueError('HTML 图表横轴必须引用实际报告输出字段。')
        series = chart.get('series')
        if not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
            raise ValueError('每个 HTML 图表需包含 1 至 6 个数据系列。')
        if kind in {'pie', 'doughnut'} and len(series) != 1 or kind == 'combo' and len(series) < 2:
            raise ValueError('饼图需一个系列，组合图至少需两个系列。')
        normalized = []
        if kind == 'metric' and (horizontal is not None or len(series) != 1):
            raise ValueError('A value card requires one aggregate series and no grouping field.')
        for index, item in enumerate(series):
            if not isinstance(item, dict) or not item.keys() <= {'field', 'aggregate', 'label', 'type', 'axis'}:
                raise ValueError('HTML 图表系列配置无效。')
            aggregate = item.get('aggregate', 'sum')
            field = item.get('field')
            if not isinstance(aggregate, str) or aggregate not in AGGREGATES or field is None and aggregate != 'count' or field is not None and field not in headers:
                raise ValueError('HTML 图表系列需使用有效运算和报告输出字段。')
            if kind in {'pie', 'doughnut'} and aggregate not in {'count', 'sum'}:
                raise ValueError('饼图占比仅支持数量或合计，请将平均值与极值改用柱状或折线图。')
            label = item.get('label')
            if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > 80):
                raise ValueError('HTML 图表系列名称需为最多 80 字符的文本。')
            part_type = item.get('type', 'bar' if kind == 'combo' and index == 0 else 'line')
            axis = item.get('axis', 'left')
            if not isinstance(part_type, str) or not isinstance(axis, str) or part_type not in {'line', 'bar'} or axis not in {'left', 'right'}:
                raise ValueError('HTML 组合图系列仅支持 line/bar 和 left/right 坐标轴。')
            if kind != 'combo' and ('type' in item or axis != 'left'):
                raise ValueError('系列类型与右侧坐标轴仅用于组合图。')
            normalized.append({'aggregate': aggregate, **({'field': field} if field is not None else {}),
                               **({'label': label.strip()} if label is not None else {}),
                               **({'type': part_type, 'axis': axis} if kind == 'combo' else {})})
        if kind == 'combo' and {item['type'] for item in normalized} != {'bar', 'line'}:
            raise ValueError('组合图需同时包含柱状和折线系列。')
        limit = chart.get('limit', 30)
        order = chart.get('sort', 'source')
        if type(limit) is not int or not 1 <= limit <= MAX_POINTS or not isinstance(order, str) or order not in {'source', 'label', 'value_desc'}:
            raise ValueError('HTML 图表需使用有效排序及 1 至 100 个显示分组。')
        options = chart.get('options', {})
        if not isinstance(options, dict) or not options.keys() <= {'legend', 'stacked', 'horizontal', 'smooth', 'y_label', 'right_y_label'}:
            raise ValueError('HTML 图表样式选项无效，不能包含自定义代码。')
        for key in ('legend', 'stacked', 'horizontal', 'smooth'):
            if key in options and type(options[key]) is not bool:
                raise ValueError('HTML 图表样式开关必须为布尔值。')
        for key in ('y_label', 'right_y_label'):
            if key in options and (not isinstance(options[key], str) or len(options[key]) > 80):
                raise ValueError('HTML 图表坐标轴名称不能超过 80 字符。')
        if options.get('horizontal') and kind != 'bar' or options.get('stacked') and kind not in {'bar', 'combo'}:
            raise ValueError('水平布局仅支持柱状图，堆叠仅支持柱状或组合图。')
        output.append({'type': kind, 'title': title.strip(), 'x': horizontal, 'series': normalized,
                       'limit': limit, 'sort': order, 'options': {'legend': True, **options}})
    return output


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    text = str(value).strip()
    if len(text) > 100 or not _NUMBER.fullmatch(text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() and abs(number.adjusted()) <= 100 else None


def _missing(value):
    return value is None or isinstance(value, str) and not value.strip()


def _stat(rows, series):
    field, operation = series.get('field'), series['aggregate']
    values = [row.get(field) for row in rows] if field is not None else [1] * len(rows)
    present = [value for value in values if not _missing(value)]
    if operation == 'count':
        return len(present), {'records': len(rows), 'valid': len(present), 'missing': len(rows) - len(present)}
    numbers = [_number(value) for value in present]
    if any(value is None for value in numbers):
        raise ValueError(f'HTML 图表数值字段「{field}」含无法转换的值，请先配置报告转换。')
    if not numbers:
        return None, {'records': len(rows), 'valid': 0, 'missing': len(rows)}
    with localcontext() as context:
        context.prec = 512
        value = (sum(numbers, Decimal(0)) if operation in {'sum', 'avg'} else
                 min(numbers) if operation == 'min' else max(numbers))
        if operation == 'avg':
            value = (value / len(numbers)).quantize(Decimal('0.000000000001'))
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError('HTML 图表数值超出可绘制范围。')
    return numeric, {'records': len(rows), 'valid': len(numbers), 'missing': len(rows) - len(numbers)}


def _default_charts(headers, rows, language):
    zh = language == 'zh-hans'
    if not headers or not rows:
        return []
    numeric = [field for field in headers if any(not _missing(row.get(field)) for row in rows)
               and all(_missing(row.get(field)) or _number(row.get(field)) is not None for row in rows)]
    categories = [field for field in headers if field not in numeric]
    if len(rows) == 1 and len(numeric) == 1:
        return normalize_charts([{'type': 'metric', 'title': numeric[0], 'x': None,
                                  'series': [{'field': numeric[0], 'aggregate': 'sum'}]}], headers)
    def dates(field):
        sample = next((row.get(field) for row in rows if not _missing(row.get(field))), None)
        try:
            return isinstance(sample, str) and date.fromisoformat(sample[:10]) is not None
        except ValueError:
            return False
    x = next((field for field in categories if dates(field)), categories[0] if categories else None)
    measure = next((field for field in numeric if field != x), None)
    count = {'aggregate': 'count', 'label': '记录数' if zh else 'Records'}
    value = {'field': measure, 'aggregate': 'avg', 'label': measure} if measure else count
    return normalize_charts([
        {'type': 'line', 'title': '趋势概览' if zh else 'Trend Overview', 'x': x, 'series': [value], 'sort': 'label' if x and dates(x) else 'source'},
        {'type': 'bar', 'title': '分组对比' if zh else 'Category Comparison', 'x': x, 'series': [{**value, 'aggregate': 'sum'} if measure else count]},
        {'type': 'pie', 'title': '记录分布' if zh else 'Record Distribution', 'x': x, 'series': [count], 'sort': 'value_desc', 'limit': 8},
        {'type': 'combo', 'title': '数量与指标' if zh else 'Volume and Metric', 'x': x, 'series': [
            {**count, 'type': 'bar'}, {**value, 'type': 'line', 'axis': 'right', 'label': measure or ('记录数（折线）' if zh else 'Records (Line)')}],
         'options': {'y_label': '记录数' if zh else 'Records', 'right_y_label': measure or ('记录数' if zh else 'Records')}},
    ], headers)


def build_charts(headers, rows, chart_specs=None, *, language='en'):
    rows = list(rows)
    if len(rows) > 5000 or any(not isinstance(row, dict) for row in rows):
        raise ValueError('HTML 图表仅接受最多 5000 行有效报告结果。')
    specs = _default_charts(headers, rows, language) if chart_specs is None else normalize_charts(chart_specs, headers)
    output = []
    names = {'count': 'Count', 'sum': 'Total', 'avg': 'Average', 'min': 'Minimum', 'max': 'Maximum'}
    if language == 'zh-hans':
        names = {'count': '数量', 'sum': '合计', 'avg': '平均', 'min': '最小', 'max': '最大'}
    for chart in specs:
        if chart['type'] == 'metric':
            definition = chart['series'][0]
            value, quality = _stat(rows, definition)
            label = definition.get('label') or (f"{definition.get('field', '')} · {names[definition['aggregate']]}".strip(' ·'))
            output.append({'type': 'metric', 'title': chart['title'] or label, 'value': value,
                           'quality': quality, 'language': language,
                           'meta': {'row_count': len(rows), 'group_count': 1, 'displayed_groups': 1,
                                    'omitted_groups': 0, 'omitted_row_count': 0, 'other_included': False}})
            continue
        grouped = OrderedDict()
        for index, row in enumerate(rows):
            value = row.get(chart['x']) if chart['x'] is not None else index + 1
            label = '' if value is None else str(value)
            if len(label) > 500:
                raise ValueError('HTML 图表分组值过长，请选择更短的分类字段。')
            identity = json.dumps([type(value).__name__, value], ensure_ascii=False, sort_keys=True, default=str)
            group = grouped.setdefault(identity, {'label': label, 'x': value, 'rows': []})
            group['rows'].append(row)
        groups = list(grouped.values())
        for group in groups:
            group['stats'] = [_stat(group['rows'], series) for series in chart['series']]
        if chart['sort'] == 'label':
            groups.sort(key=lambda group: group['label'])
        elif chart['sort'] == 'value_desc':
            groups.sort(key=lambda group: (group['stats'][0][0] is not None, group['stats'][0][0] or 0), reverse=True)
        if chart['type'] in {'pie', 'doughnut'} and any(group['stats'][0][0] is not None and group['stats'][0][0] < 0 for group in groups):
            raise ValueError('饼图不能包含负值，请改用柱状图或折线图。')
        shown = groups[:chart['limit']]
        series = []
        for index, item in enumerate(chart['series']):
            values = [group['stats'][index][0] for group in shown]
            series.append({'label': item.get('label') or (f"{item.get('field', '')} · {names[item['aggregate']]}".strip(' ·')),
                           'values': values, 'quality': [group['stats'][index][1] for group in shown],
                           'type': item.get('type', chart['type']), 'axis': item.get('axis', 'left')})
        omitted = groups[chart['limit']:]
        labels = [group['label'] for group in shown]
        x_values = None
        if chart['type'] == 'scatter':
            x_values = [_number(group['x']) for group in shown]
            if any(value is None for value in x_values):
                raise ValueError('散点图横轴需为有效数值，请选择数值字段。')
            x_values = [float(value) for value in x_values]
        include_other = bool(omitted and chart['type'] in {'pie', 'doughnut'})
        if include_other:
            labels.append(('其他' if language == 'zh-hans' else 'Other') + f" ({len(omitted)})")
            series[0]['values'].append(sum(group['stats'][0][0] or 0 for group in omitted))
            series[0]['quality'].append({key: sum(group['stats'][0][1][key] for group in omitted)
                                         for key in ('records', 'valid', 'missing')})
        output.append({'type': chart['type'], 'title': chart['title'] or chart['type'].title(),
                       'labels': labels, 'series': series, 'options': chart['options'],
                       **({'x_values': x_values} if x_values is not None else {}),
                       'meta': {'row_count': len(rows), 'group_count': len(groups), 'displayed_groups': len(shown),
                                'omitted_groups': len(omitted), 'omitted_row_count': sum(len(group['rows']) for group in omitted),
                                'other_included': include_other},
                       'language': language})
    return output


def render_charts_html(charts, *, language='en'):
    if not charts:
        return ''
    zh = language == 'zh-hans'
    cards = []
    for index, chart in enumerate(charts):
        meta = chart['meta']
        note = (f"完整结果 {meta['row_count']} 行 · 显示 {meta['displayed_groups']} / {meta['group_count']} 组" if zh else
                f"Full result: {meta['row_count']} rows · Showing {meta['displayed_groups']} / {meta['group_count']} groups")
        if chart['type'] == 'metric':
            note = (f"完整结果：{meta['row_count']} 行" if zh else f"Full result: {meta['row_count']} rows")
        if meta['omitted_groups']:
            note += (f" · 另有 {meta['omitted_groups']} 组（{meta['omitted_row_count']} 行）"
                     + ('合并为其他' if meta.get('other_included') else '未绘制') if zh else
                     f" · {meta['omitted_groups']} groups ({meta['omitted_row_count']} rows) "
                     + ('combined into Other' if meta.get('other_included') else 'not plotted'))
        card_class = 'report-chart report-chart--metric' if chart['type'] == 'metric' else 'report-chart'
        cards.append('<article class="' + card_class + '"><h3>' + escape(chart['title']) + '</h3>'
                     '<div class="report-chart-canvas" data-engenie-chart="' + str(index) + '"></div>'
                     '<p class="report-chart-note">' + escape(note) + '</p></article>')
    return '<section class="report-charts" id="charts"><h2>' + ('图表视图' if zh else 'Chart Views') + '</h2><div class="report-chart-grid">' + ''.join(cards) + '</div></section>'


def chart_assets():
    return (Path(__file__).resolve().parents[1] / 'static' / 'js' / 'report_chart_runtime.js').read_text(encoding='utf-8')


def chart_styles():
    return '''.report-chart--metric .report-chart-canvas{min-height:0}.erc-metric{padding:14px 2px 8px;min-height:92px;display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 10px}.erc-metric-main{display:flex;align-items:baseline;flex-wrap:wrap;gap:8px}.erc-metric-value{font:750 clamp(32px,5vw,56px)/1.1 system-ui;letter-spacing:-.04em;color:#2868c7;overflow-wrap:anywhere}.erc-metric-unit{font:600 15px system-ui;color:#63758a}.erc-metric-subtitle,.erc-metric-quality{flex-basis:100%;font:12px/1.5 system-ui;color:#63758a;overflow-wrap:anywhere}''' + '''.report-chart-canvas{min-height:270px;position:relative}.report-chart-canvas svg{display:block;width:100%;height:auto;overflow:visible}.erc-legend{display:flex;flex-wrap:wrap;gap:8px 14px;margin:12px 0 0;max-height:120px;overflow:auto;font:12px system-ui;color:#526174}.erc-legend-item{display:inline-flex;align-items:center;gap:6px;border:0;background:none;padding:2px 0;font:inherit;color:inherit;text-align:left}.erc-legend-item[aria-pressed]{cursor:pointer}.erc-swatch{width:9px;height:9px;border-radius:3px;flex:none}.erc-tooltip{position:fixed;z-index:99999;max-width:min(320px,calc(100vw - 16px));padding:9px 12px;border-radius:9px;color:#fff;background:#142840;box-shadow:0 8px 24px #10243b30;font:12px/1.5 system-ui;white-space:pre-wrap;pointer-events:none;overflow-wrap:anywhere}.erc-tooltip[hidden]{display:none!important}.erc-empty{padding:70px 15px;text-align:center;color:#63758a;font:13px system-ui}.erc-hit{cursor:crosshair;outline:none}.erc-axis{font:10px system-ui;fill:#63758a}.erc-grid{stroke:#e7edf3;stroke-width:1}.erc-point-active{stroke:#142840;stroke-width:2}@media print{.erc-tooltip{display:none!important}.erc-legend{max-height:none;overflow:visible}.report-chart{break-inside:avoid}}'''
