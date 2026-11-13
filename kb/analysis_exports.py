"""Deterministic tracksheet analysis, Excel/plot exports and private snapshots.

The page and the agent use the same permission-checked queries. Files contain
the database result, never a table copied back from an LLM's context window.
"""
from __future__ import annotations

import io
import hashlib
import json
import math
import re
import time
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, QueryDict
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET

from . import access
from .models import KbTracker, KnowledgeBase, KnowledgeFeed
from .publication import digest
from .tracker_query import RESPONSE_CHARS, encode

EXPORT_ROWS = 50000
EXPORT_CHARS = 20_000_000
FILE_TTL = 24 * 60 * 60
MIME = {'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'svg': 'image/svg+xml'}
COLORS = ('#2f6f9f', '#c2410c', '#3d7a3f', '#7b3fa0', '#a16207', '#0f766e', '#b91c1c', '#334155')


def query_analysis(user, slug, params):
    """Internal full-result request; all row/document/feed scopes stay in the views."""
    from .feed_kb_views import feed_data, tracker_data
    kb = KnowledgeBase.objects.select_related('parent').filter(slug=slug).first()
    if kb is None or not access.kb_accessible(user, kb):
        raise ValueError('知识库不存在或无权访问。')
    query = QueryDict('', mutable=True)
    for key, values in params.items():
        query.setlist(key, values if isinstance(values, list) else [str(values)])
    query['scope'] = 'history'
    query['offset'] = '0'
    query.pop('export', None)
    request = HttpRequest()
    request.method = 'GET'
    request.user = user
    request.GET = query
    request.analysis_full_result = True
    feeds = KnowledgeFeed.objects.filter(kb=kb)
    view = (feed_data if kb.category == 'business' and feeds.exists()
            and not feeds.filter(adapter_spec__ingest_mode='documents').exists() else tracker_data)
    if view == tracker_data and query.get('feed'):
        raise ValueError('文档追踪表请留空 feed_id，按知识库分析。')
    try:
        response = view(request, slug)
    except Http404 as exc:
        raise ValueError('知识库或已启用的追踪表不存在。') from exc
    if response.status_code != 200:
        message = json.loads(response.content).get('message', '无法分析此追踪表。') if response.status_code == 400 else '无权访问。'
        raise ValueError(message)
    data = json.loads(response.content)
    criteria = {'query': query.get('analysis_search', ''),
                'filters': json.loads(query.get('analysis_filters', '[]'))}
    if criteria['query'] or criteria['filters']:
        data['criteria'] = criteria
    # Run status and UI pagination metadata are not facts in an analysis snapshot.
    keys = ('kind', 'groups', 'total_matched', 'total_groups', 'applied_query', 'series',
            'bucket_count', 'period_unit', 'span', 'period_label', 'operation', 'measure',
            'object_field', 'object_values', 'timezone', 'range_from', 'range_to', 'notice',
            'fields', 'numeric_fields', 'object_has_more', 'object_next_offset',
            'criteria', 'data_from', 'data_to')
    return kb, {key: data[key] for key in keys if key in data}


def analysis_table(data, params):
    if data['kind'] == 'trend':
        series = data['series']
        headers = ['Period']
        for item in series:
            name = item['object_value']
            headers.extend(f'{name} | {label}' for label in
                           ('Value', 'Change', 'Change (%)', 'Records', 'Valid numeric values', 'Comparison'))
        rows = []
        for index, point in enumerate(series[0]['points']):
            row = [point['bucket']]
            for item in series:
                current = item['points'][index]
                row.extend(current[key] for key in ('value', 'change', 'change_pct', 'records',
                                                    'valid_numeric_values', 'comparison'))
            rows.append(row)
        return headers, rows
    group = params.get('group', '')
    headers = [group or 'Scope', 'Value', 'Valid numeric values']
    rows = [[item.get('by', {}).get(group) if group else 'Overall',
             scalar(item['metrics'].get('value')), item.get('valid_numeric_values', {}).get('value')]
            for item in data.get('groups', [])]
    return headers, rows


def scalar(value):
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def interactive_chart(data, params, title):
    """Use full, permission-checked analysis results in the shared chat renderer.

    This payload stays outside the model's bounded textual evidence. Publication
    validation rebuilds it from the same database query before it can be sent or
    restored from chat history; the model never supplies chart coordinates.
    """
    if data.get('kind') not in ('tracker_summary', 'trend'):
        return None
    metric = params.get('chart_metric', 'value')
    if metric not in ('value', 'change', 'change_pct'):
        raise ValueError('Chart metric must be value, change or change_pct.')
    if data['kind'] != 'trend' and metric != 'value':
        raise ValueError('Change and change_pct charts require trend mode.')
    kind = params.get('chart_type', 'auto')
    if kind not in ('auto', 'line', 'bar', 'pie', 'combo', 'area', 'scatter', 'doughnut'):
        raise ValueError('Chart type must be auto, line, bar, pie, combo, area, scatter or doughnut.')

    def number(value):
        # Empty strings, booleans and warning dictionaries must not become zero.
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        try:
            return value if math.isfinite(value) else None
        except OverflowError:
            raise ValueError('An analysis chart value exceeds the drawable numeric range.') from None

    metric_label = {'value': 'Value', 'change': 'Change', 'change_pct': 'Change (%)'}[metric]
    chart = {'type': 'line' if data['kind'] == 'trend' else 'bar',
             'title': str(title)[:120], 'labels': [], 'series': [],
             'options': {'legend': True, 'y_label': metric_label}, 'language': 'en',
             'meta': {'system_metric': metric, 'system_summary': data['kind'] == 'tracker_summary'}}
    if data['kind'] == 'trend':
        items = data.get('series', [])
        if len(items) > 8 or any(len(item.get('points', [])) > 500 for item in items):
            raise ValueError('Interactive analysis charts support up to 8 series and 500 periods.')
        points = items[0].get('points', []) if items else []
        chart['labels'] = [str(point.get('label', point.get('bucket', ''))) for point in points]
        buckets = [point.get('bucket') for point in points]
        for item in items:
            current = item.get('points', [])
            if [point.get('bucket') for point in current] != buckets:
                raise ValueError('Interactive analysis chart periods must align across series.')
            part = {'label': str(item.get('object_value', '')), 'values': [number(point.get(metric)) for point in current]}
            if params.get('op', 'count') != 'count':
                part['quality'] = [{'valid': point.get('valid_numeric_values', 0),
                                    'records': point.get('records', 0)} for point in current]
            chart['series'].append(part)
    else:
        groups = data.get('groups', [])
        if len(groups) > 50:
            raise ValueError('Interactive summary charts support up to 50 groups. Narrow the grouping or export Excel.')
        group = params.get('group', '')
        chart['labels'] = [str(item.get('by', {}).get(group, 'Overall')) for item in groups]
        chart['series'] = [{'label': metric_label,
                            'values': [number(item.get('metrics', {}).get('value')) for item in groups]}]
    if kind != 'auto':
        chart['type'] = kind
    if kind in ('pie', 'doughnut'):
        if len(chart['series']) != 1:
            raise ValueError('Pie and doughnut charts require one series. Select one object or use a summary.')
        if params.get('op', 'count') not in ('count', 'sum') or metric != 'value':
            raise ValueError('Pie and doughnut charts require count or sum values. Use a bar or line chart for averages or changes.')
        if any(value is not None and value < 0 for value in chart['series'][0]['values']):
            raise ValueError('Pie and doughnut charts cannot contain negative values. Use a bar or line chart.')
    elif kind == 'combo':
        if len(chart['series']) < 2:
            raise ValueError('Combo charts require at least two series. Select two or more objects in a trend analysis.')
        for index, item in enumerate(chart['series']):
            item['type'] = 'bar' if index == 0 else 'line'
    elif kind == 'scatter':
        if data['kind'] == 'trend':
            raise ValueError('Scatter charts require a numeric grouping field. Use summary mode with numeric groups.')
        x_values = []
        for label in chart['labels']:
            if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', label.strip()):
                raise ValueError('Scatter chart group values must be numeric. Choose a numeric grouping field.')
            value = float(label)
            if not math.isfinite(value):
                raise ValueError('Scatter chart group values must be finite numbers.')
            x_values.append(value)
        chart['x_values'] = x_values
    return chart


def excel(data, params, title):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.utils import get_column_letter
    headers, rows = analysis_table(data, params)
    if len(rows) > EXPORT_ROWS:
        raise ValueError('导出最多 50,000 组，请缩小日期范围或减少分组。')
    book = Workbook()
    sheet = book.active
    sheet.title = 'Analysis'
    used = 0

    def append(target, values):
        nonlocal used
        clean = []
        for value in values:
            if isinstance(value, str):
                value = ILLEGAL_CHARACTERS_RE.sub('', value)
                if len(value) > 32767:
                    raise ValueError('单元格超过 Excel 的 32,767 字符限制，请缩短分组字段。')
                used += len(value)
            clean.append(value)
        if used > EXPORT_CHARS:
            raise ValueError('导出文本过大，请缩小日期范围。')
        target.append(clean)
        for cell in target[target.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = 's'  # Source text must never become an Excel formula.

    append(sheet, headers)
    for row in rows:
        append(sheet, row)
    sheet.freeze_panes = 'B2' if data['kind'] == 'trend' else 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    for index in range(1, len(headers) + 1):
        sheet.column_dimensions[get_column_letter(index)].width = 28 if index == 1 else 22
    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='2F6F9F')
    meta = book.create_sheet('Details')
    for row in [('Knowledge base', title), ('Generated at', timezone.now().isoformat()),
                ('Timezone', data.get('timezone')), ('From (inclusive)', data.get('range_from')),
                ('To (exclusive)', data.get('range_to')), ('Operation', params.get('op', 'count')),
                ('Measure', params.get('measure', '')), ('Period', data.get('period_label', '')),
                ('Notice', data.get('notice', '')),
                ('Comparison', 'Change (%) uses percent units, e.g. 25 means 25%. Missing periods stay blank; a zero baseline has no change rate.'),
                ('Query', encode(params))]:
        append(meta, row)
    meta.column_dimensions['A'].width = 26
    meta.column_dimensions['B'].width = 100
    book.properties.title = title
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def plot(data, params, title):
    """Standalone SVG, no model code, executable content or external resources."""
    metric = params.get('chart_metric', 'value')
    if metric not in ('value', 'change', 'change_pct'):
        raise ValueError('图表口径必须为 value/change/change_pct。')
    trend = data['kind'] == 'trend'
    if not trend and len(data.get('groups', [])) > 50:
        raise ValueError('汇总图最多 50 组，请缩小分组范围；Excel 可导出全部分组。')
    width, height = 1100, 620
    root = ET.Element('svg', xmlns='http://www.w3.org/2000/svg', viewBox=f'0 0 {width} {height}', role='img')

    def add(tag, text=None, **attrs):
        node = ET.SubElement(root, tag, {key.replace('_', '-'): str(value) for key, value in attrs.items()})
        if text is not None:
            node.text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(text))
        return node

    add('title', title)
    add('rect', width=width, height=height, fill='#ffffff')
    add('text', title[:100], x=70, y=30, font_size=20, fill='#162f48')
    subtitle = f"{params.get('op', 'count')} {params.get('measure', '')} | {data.get('timezone', '')} | {metric}"
    add('text', subtitle[:150], x=70, y=55, font_size=13, fill='#475569')
    if trend:
        series = [(item['object_value'], [point.get(metric) for point in item['points']]) for item in data['series']]
        labels = [point['label'] for point in data['series'][0]['points']]
    else:
        group = params.get('group', '')
        series = [('Value', [scalar(item['metrics'].get('value')) for item in data.get('groups', [])])]
        labels = [item.get('by', {}).get(group, 'Overall') for item in data.get('groups', [])]
    numbers = [value for _, values in series for value in values
               if isinstance(value, (int, float)) and math.isfinite(value)]
    if not numbers:
        add('text', 'No numeric data in this range.', x=70, y=140, font_size=18)
    else:
        lo, hi = min(0, min(numbers)), max(0, max(numbers))
        # Normalize first so very large finite numbers cannot overflow the axis.
        scale = max(abs(lo), abs(hi), 1)
        low, high = lo / scale, hi / scale
        if low == high:
            high = low + 1
        x = lambda index: 100 + index / max(1, len(labels) - 1) * 920
        y = lambda value: 450 - (value / scale - low) / (high - low) * 350
        for tick in range(6):
            pos = 450 - tick * 70
            value = (low + tick / 5 * (high - low)) * scale
            add('line', x1=100, y1=pos, x2=1020, y2=pos, stroke='#e2e8f0')
            add('text', f'{value:.5g}', x=90, y=pos + 4, text_anchor='end', font_size=12, fill='#475569')
        for index, (name, values) in enumerate(series):
            color = COLORS[index % len(COLORS)]
            path = []
            previous = False
            for point, value in enumerate(values):
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    previous = False
                    continue
                px, py = x(point), y(value)
                if trend:
                    path.append(f'{"L" if previous else "M"}{px:.2f},{py:.2f}')
                    add('circle', cx=px, cy=py, r=2, fill=color)
                    previous = True
                else:
                    bar = add('rect', x=px - 6, y=min(y(0), py), width=12, height=max(1, abs(y(0) - py)), fill=color)
                    ET.SubElement(bar, 'title').text = str(labels[point]) + ': ' + str(value)
            if path:
                add('path', d=' '.join(path), fill='none', stroke=color, stroke_width=2)
            add('text', str(name)[:50], x=100 + (index % 4) * 240, y=550 + (index // 4) * 22, fill=color, font_size=13)
        for index, label in enumerate(labels):
            if index % max(1, math.ceil(len(labels) / 7)) == 0 or index == len(labels) - 1:
                add('text', str(label)[:24], x=x(index), y=478 + (index % 2) * 18,
                    text_anchor='middle', font_size=11, fill='#475569')
    add('text', f"{data.get('range_from') or 'All history'} — {data.get('range_to') or 'Present'}; missing values are gaps.",
        x=70, y=605, font_size=12, fill='#475569')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def export_response(request, kb, data):
    fmt = request.GET.get('export', '')
    if not fmt:
        return None
    if fmt not in MIME or data.get('kind') not in ('tracker_summary', 'trend'):
        raise ValueError('仅汇总与周期对比支持导出，格式为 xlsx/svg。')
    if data.get('has_more'):
        raise ValueError('导出最多 50,000 组，请缩小日期范围或减少分组。')
    content = (excel if fmt == 'xlsx' else plot)(data, request.GET, kb.name)
    response = HttpResponse(content, content_type=MIME[fmt])
    response['Content-Disposition'] = content_disposition_header(True, f'{kb.name}-analysis.{fmt}')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def permission_stamp(user, kb):
    rows = KnowledgeBase.objects.filter(pk=kb.pk) | KnowledgeBase.objects.filter(parent=kb)
    return digest(encode(sorted(item.slug for item in rows.select_related('parent') if access.kb_accessible(user, item))))


def store_file(user, kb, content, fmt):
    directory = Path(settings.DATA_DIR) / 'analysis_exports'
    directory.mkdir(parents=True, exist_ok=True)
    # Only delete our own expired UUID artifacts; never a user-supplied path.
    cutoff = time.time() - FILE_TTL
    for old in directory.iterdir():
        if re.fullmatch(r'[0-9a-f]{32}\.(xlsx|svg)', old.name):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
            except OSError:
                pass
    name = uuid4().hex + '.' + fmt
    (directory / name).write_bytes(content)
    token = signing.dumps({'user': user.pk, 'kb': kb.slug, 'file': name,
                           'permissions': permission_stamp(user, kb)}, salt='tracker-analysis-file', compress=True)
    return {'filename': f'{kb.name}-analysis.{fmt}',
            'url': reverse('kb:analysis_file', kwargs={'token': token}), 'format': fmt,
            'content_sha256': hashlib.sha256(content).hexdigest(),
            'expires_in_seconds': FILE_TTL}


@login_required
@require_GET
def download_file(request, token):
    try:
        info = signing.loads(token, salt='tracker-analysis-file', max_age=FILE_TTL)
        name = info['file']
        if info['user'] != request.user.pk or not re.fullmatch(r'[0-9a-f]{32}\.(xlsx|svg)', name):
            raise ValueError
        kb = KnowledgeBase.objects.select_related('parent').get(slug=info['kb'])
        tracker = KbTracker.objects.filter(kb=kb).first()
        if (not access.kb_accessible(request.user, kb) or tracker is None
                or not (tracker.enabled or access.can_manage_kb(request.user, kb))
                or permission_stamp(request.user, kb) != info['permissions']):
            raise ValueError
        path = Path(settings.DATA_DIR) / 'analysis_exports' / name
        fmt = name.rsplit('.', 1)[1]
        response = FileResponse(path.open('rb'), as_attachment=True, filename=f'{kb.name}-analysis.{fmt}', content_type=MIME[fmt])
    except (signing.BadSignature, KeyError, TypeError, ValueError, OSError, KnowledgeBase.DoesNotExist):
        raise Http404('文件不存在、已过期或无权访问，请重新生成。')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return response


def preview(data, limit):
    result = dict(data)
    if 'groups' in result:
        result['groups'] = result['groups'][:limit]
        result['preview_only'] = len(data['groups']) > limit
    if 'series' in result:
        result['series'] = [{**item, 'points': item['points'][-limit:]} for item in data['series']]
        result['preview_only'] = data['bucket_count'] > limit
        result['preview_position'] = 'last periods'
    return result


def agent_analysis(user, allowed, service, slug, params, output):
    if output not in ('preview', 'excel', 'plot', 'both'):
        raise ValueError('output 使用 preview/excel/plot/both。')
    if user is None:
        raise ValueError('生成分析需要已登录的用户身份。')
    target = KnowledgeBase.objects.select_related('parent').filter(slug=slug).first()
    if target is None or not allowed(target):
        raise ValueError('知识库不存在或无权访问。')
    with service.lock:
        cap = min(RESPONSE_CHARS, service.remaining)
        if cap < 2500:
            return service._budget_notice()
        kb, data = query_analysis(user, slug, params)
        params = dict(params)
        if data['kind'] == 'trend':
            # Freeze implicit "now" bounds for later publication verification.
            params.update({'from': data['range_from'], 'to': data['range_to']})
        files = []
        if params['kind'] == 'options' and output != 'preview':
            raise ValueError('先选择 summary/trend 分析方式，再生成文件。')
        chart = interactive_chart(data, params, kb.name) if output in ('plot', 'both') else None
        generated = {}
        if output in ('excel', 'both'):
            generated['xlsx'] = excel(data, params, kb.name)
        if output in ('plot', 'both'):
            generated['svg'] = plot(data, params, kb.name)
        for fmt, content in generated.items():
            files.append(store_file(user, kb, content, fmt))
        limit = 20
        while True:
            result = preview(data, limit)
            text = encode({'analysis': result, 'files': files,
                           'notice': '统计使用完整匹配数据；JSON 可能仅为预览，文件包含完整结果。文件仅当前账号可下载，有效期 24 小时。'})
            if len(text) <= cap:
                break
            if limit <= 1:
                raise ValueError('分析字段过长，请缩小分组或对象范围。')
            limit = max(1, limit // 2)
        if params['kind'] != 'options':
            source_text = encode({'analysis': result, 'files': files})
            snapshot = {'kind': 'tracker', 'source': '追踪表汇总与周期分析', 'text': source_text,
                        'query': {'kb_slug': slug}, 'analysis_query': params, 'preview_limit': limit,
                        'files': files,
                        'data_digest': digest(encode(data)), 'result_digest': digest(source_text)}
            if chart is not None:
                snapshot['chart'] = chart
            if snapshot not in service.evidence:
                service.evidence.append(snapshot)
        service.remaining -= len(text)
        return text


def validate_analysis_snapshot(snapshot, user):
    try:
        text = snapshot['text']
        limit = snapshot['preview_limit']
        if (not isinstance(text, str) or len(text) > RESPONSE_CHARS or digest(text) != snapshot['result_digest']
                or type(limit) is not int or not 1 <= limit <= 20):
            return False
        kb, data = query_analysis(user, snapshot['query']['kb_slug'], snapshot['analysis_query'])
        if 'chart' in snapshot:
            if not isinstance(snapshot['chart'], dict) or snapshot['chart'] != interactive_chart(data, snapshot['analysis_query'], kb.name):
                return False
        files = snapshot.get('files', [])
        for file in files:
            token = file['url'].rstrip('/').rsplit('/', 1)[-1]
            info = signing.loads(token, salt='tracker-analysis-file', max_age=FILE_TTL)
            if (info['user'] != user.pk or info['kb'] != kb.slug
                    or file['url'] != reverse('kb:analysis_file', kwargs={'token': token})
                    or not re.fullmatch(r'[0-9a-f]{32}\.(xlsx|svg)', info['file'])
                    or permission_stamp(user, kb) != info['permissions']):
                return False
            with (Path(settings.DATA_DIR) / 'analysis_exports' / info['file']).open('rb') as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != file['content_sha256']:
                    return False
        return (digest(encode(data)) == snapshot['data_digest']
                and encode({'analysis': preview(data, limit), 'files': files}) == text)
    except (signing.BadSignature, KeyError, TypeError, ValueError, OverflowError, OSError):
        return False
