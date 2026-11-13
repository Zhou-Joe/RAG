"""Deterministic report links and safe, standalone HTML email attachments.

URLs come only from selected output cells. The model never supplies anchors,
and rendering does not fetch links, images, scripts, or external stylesheets.
"""
from __future__ import annotations

import base64
from html import escape
import json
import math
from pathlib import PurePosixPath
import re
import secrets
from urllib.parse import parse_qsl, unquote, urlsplit


MAX_SOURCE_LINKS = 20
MAX_URL_CHARS = 2048
MAX_HTML_BYTES = 1_000_000
MAX_REPORT_ROWS = 5000
MAX_REPORT_COLUMNS = 30
_CONTROLS = re.compile(r'[\x00-\x20\x7f-\x9f]')
_FILE_CONTROLS = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_SECRET_KEYS = {
    'token', 'accesstoken', 'refreshtoken', 'idtoken', 'auth', 'authorization',
    'apikey', 'secret', 'clientsecret', 'password', 'passwd', 'sig', 'signature',
    'credential', 'xamzcredential', 'xamzsignature',
}


def safe_source_url(value):
    """Return an intact HTTP(S) source URL, or None; never repair unsafe input."""
    if not isinstance(value, str) or _FILE_CONTROLS.search(value):
        return None
    value = value.strip()
    if (not value or len(value) > MAX_URL_CHARS or _CONTROLS.search(value)
            or any(character in value for character in '<>"`\\')):
        return None
    decoded = value
    for _ in range(2):
        decoded = unquote(decoded)
        if _FILE_CONTROLS.search(decoded):
            return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in {'http', 'https'} or not parsed.netloc
                or not parsed.hostname or parsed.username is not None or parsed.password is not None):
            return None
        # urlsplit validates a malformed/out-of-range port only when accessed.
        parsed.port
        if '%' in parsed.netloc:
            return None
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True) + parse_qsl(parsed.fragment, keep_blank_values=True):
            canonical = re.sub(r'[-_\s]', '', key).casefold()
            if canonical in _SECRET_KEYS:
                return None
    except (ValueError, UnicodeError):
        return None
    return value


def _display(value):
    if value is None:
        return ''
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('HTML 报告包含无效数值，请先处理报告字段。')
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError('HTML 报告包含无法显示的数据。') from exc
    return str(value)


def _row_label(headers, row):
    preferred = {'name', 'title', '名称', '标题', '设备', 'attraction', 'item', '_record'}
    candidates = [header for header in headers if header.casefold() in preferred] + list(headers)
    for header in candidates:
        value = row.get(header)
        if isinstance(value, str) and value.strip() and not re.match(r'^[a-z][a-z0-9+.-]*:', value.strip(), re.I):
            return value.strip()[:160]
    return ''


def extract_source_links(headers, rows, *, limit=MAX_SOURCE_LINKS):
    """Select unique links from actual chosen columns, preserving source order.

    Whole-cell URLs are supported. URL-looking invalid cells are counted as
    rejected; arbitrary text/HTML is not searched for executable hyperlinks.
    """
    if type(limit) is not int or not 0 <= limit <= MAX_SOURCE_LINKS:
        raise ValueError('来源链接显示数量无效。')
    links = []
    seen = set()
    rejected = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('报告行格式无效。')
        label = _row_label(headers, row)
        for header in headers:
            value = row.get(header)
            if not isinstance(value, str) or not re.match(r'^\s*(?:https?|javascript|data|file|ftp|vbscript|blob|mailto|tel):', value, re.I):
                continue
            url = safe_source_url(value)
            if url is None:
                rejected += 1
                continue
            parsed = urlsplit(url)
            identity = (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, parsed.fragment)
            if identity in seen:
                continue
            seen.add(identity)
            if len(links) < limit:
                links.append({'label': (label + ' · ' if label else '') + str(header), 'url': url})
    return {'links': links, 'total_links': len(seen), 'omitted_links': max(0, len(seen) - len(links)),
            'rejected_links': rejected}


def _validated_links(selection):
    links = selection.get('links', []) if isinstance(selection, dict) else selection or []
    if not isinstance(links, list) or len(links) > MAX_SOURCE_LINKS:
        raise ValueError('来源链接格式无效。')
    for link in links:
        if not isinstance(link, dict) or safe_source_url(link.get('url')) is None:
            raise ValueError('来源链接无效，无法生成 HTML 报告。')
        yield str(link.get('label') or link['url'])[:240], link['url']


def source_links_html(selection, *, language='en'):
    """Render already extracted links with a second validation at the boundary."""
    items = list(_validated_links(selection))
    if not items:
        return ''
    heading = '来源链接' if language == 'zh-hans' else 'Source Links'
    content = '<section><h2>' + heading + '</h2><ul>'
    for label, url in items:
        content += '<li><a href="' + escape(url, quote=True) + '" target="_blank" rel="noopener noreferrer">' + escape(label) + '</a></li>'
    content += '</ul>'
    omitted = selection.get('omitted_links', 0) if isinstance(selection, dict) else 0
    rejected = selection.get('rejected_links', 0) if isinstance(selection, dict) else 0
    if omitted:
        content += '<p>' + escape(f'另有 {omitted} 个链接未在消息中列出。' if language == 'zh-hans' else f'{omitted} additional links are not listed in the message.') + '</p>'
    if rejected:
        content += '<p>' + escape(f'已排除 {rejected} 个无效或含凭据的链接。' if language == 'zh-hans' else f'{rejected} invalid or credential-bearing links were excluded.') + '</p>'
    return content + '</section>'


def teams_source_actions(selection, *, limit=4):
    """Return safe source actions; caller keeps its report action and omission note."""
    if type(limit) is not int or not 0 <= limit <= MAX_SOURCE_LINKS:
        raise ValueError('来源链接显示数量无效。')
    return [{'type': 'Action.OpenUrl', 'title': label[:100], 'url': url}
            for label, url in list(_validated_links(selection))[:limit]]


_REPORT_CSS = '''
:root{color-scheme:light;--ink:#17233b;--muted:#64748b;--line:#e2e8f0;--brand:#4f46e5}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f3f6fb;color:var(--ink);font:14px/1.6 "Segoe UI",system-ui,sans-serif}
button,input{font:inherit}button,a{touch-action:manipulation}a{color:#4338ca;text-decoration:none}a:hover{text-decoration:underline}
.report-shell{max-width:1320px;margin:auto;padding:30px 28px 50px}.report-hero{padding:30px 34px;border-radius:20px;background:linear-gradient(125deg,#18264b,#343b82 58%,#5546ad);color:white;box-shadow:0 15px 38px #25315c1a}
.report-eyebrow{font-size:11px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;opacity:.76}.report-hero h1{font-size:30px;line-height:1.25;letter-spacing:-.025em;margin:12px 0;overflow-wrap:anywhere}.report-meta{display:flex;gap:12px 24px;flex-wrap:wrap;opacity:.82;font-size:12px}
.report-nav{display:flex;align-items:center;gap:8px;margin:18px 0 22px;flex-wrap:wrap}.report-nav a,.report-nav button{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:9px;padding:8px 13px;background:white;color:#34425c;font-weight:600;font-size:12px}.report-nav button{margin-left:auto;cursor:pointer}
.report-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:24px}.report-metric{background:white;padding:18px 20px;border:1px solid var(--line);border-radius:14px;min-width:0;box-shadow:0 5px 15px #26345603}.report-metric span{display:block;color:var(--muted);font-size:11px;font-weight:650;letter-spacing:.015em;overflow-wrap:anywhere}.report-metric strong{display:block;margin:7px 0 4px;font-size:27px;font-weight:650;line-height:1.2;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.report-metric small{display:block;font-size:10px;color:var(--muted)}
.report-section{background:white;border:1px solid var(--line);border-radius:16px;padding:23px 26px;margin:20px 0;box-shadow:0 5px 16px #26345603}h2{font-size:18px;line-height:1.35;letter-spacing:-.02em;margin:0 0 14px}h3{font-size:14px;margin:0 0 14px}.report-summary{white-space:pre-wrap;font-size:14px;line-height:1.85;margin:0}.report-section details{margin-top:18px;border-top:1px solid var(--line);padding-top:15px}.report-section summary{cursor:pointer;font-weight:650;font-size:12px;color:#465879}
.report-section ul{padding-left:20px}.erc-legend-item{border:0;background:none;font:inherit;cursor:pointer}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:11px 13px;border-bottom:1px solid #e9edf4;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{position:sticky;top:0;background:#f7f9fd;font-weight:650;color:#53627a;white-space:nowrap}tbody tr:nth-child(even){background:#fafbfe}tbody tr:hover{background:#f1f4fd}td{max-width:450px;white-space:pre-wrap}td a{white-space:normal;word-break:break-word}.report-table-wrap{overflow:auto;max-height:580px;border:1px solid var(--line);border-radius:10px;margin-top:14px}table caption{text-align:left;color:var(--muted);padding:8px 0}
.report-table-toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}.report-search{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12px}.report-search input{min-width:240px;max-width:100%;border:1px solid var(--line);border-radius:8px;padding:8px 11px;color:var(--ink);background:#fafbfe}.report-table-status,.report-footnote{font-size:11px;color:var(--muted)}.report-pagination{display:flex;justify-content:flex-end;align-items:center;gap:10px;margin-top:14px}.report-pagination button{border:1px solid var(--line);border-radius:7px;background:white;padding:6px 11px;color:#40506b;cursor:pointer}.report-pagination button:disabled{opacity:.4;cursor:default}.report-sort{padding:0;border:0;background:none;color:inherit;cursor:pointer;font-weight:inherit;text-align:left}.report-sort:focus-visible,button:focus-visible,a:focus-visible,input:focus-visible{outline:2px solid #818cf8;outline-offset:3px}.report-footer{display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap;margin-top:28px;color:#7c879b;font-size:11px}.report-sources li{margin:7px 0}.js-only{display:none}.report-ready .js-only{display:flex}
@media(max-width:700px){.report-shell{padding:16px 12px}.report-hero{padding:24px}.report-hero h1{font-size:23px}.report-section{padding:19px 16px}.report-metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.report-metric{padding:15px}.report-metric strong{font-size:23px}.report-search{width:100%}.report-search input{min-width:0;flex:1}.report-nav button{margin-left:0}}
@media print{body{background:white}.report-shell{padding:0}.report-hero{background:#25315c!important;box-shadow:none;padding:20px;border-radius:0;print-color-adjust:exact}.report-nav,.report-table-toolbar,.report-pagination{display:none!important}.report-section,.report-metric{box-shadow:none;break-inside:avoid}.report-table-wrap{max-height:none;overflow:visible}#report-table tr[hidden]{display:table-row!important}#aggregate-details{display:block}a{color:inherit}.report-footer{border-top:1px solid #ddd;padding-top:12px}}
'''

_TABLE_SCRIPT = '''
(function(){'use strict';
document.documentElement.classList.add('report-ready');
const table=document.getElementById('report-table'),input=document.getElementById('report-search');
if(!table||!input)return;
const original=Array.from(table.tBodies[0].rows),status=document.getElementById('report-table-status'),pageLabel=document.getElementById('report-page-label');
const previous=document.getElementById('report-prev'),next=document.getElementById('report-next');
let ordered=original.slice(),page=0,column=-1,ascending=true,filtered=[];
function render(){
 const query=input.value.toLocaleLowerCase();filtered=ordered.filter(row=>row.textContent.toLocaleLowerCase().includes(query));
 const pages=Math.max(1,Math.ceil(filtered.length/25));page=Math.max(0,Math.min(page,pages-1));const visible=new Set(filtered.slice(page*25,page*25+25));
 original.forEach(row=>{row.hidden=!visible.has(row)});
 status.textContent=status.dataset.template.replace('{matched}',filtered.length).replace('{total}',original.length);
 pageLabel.textContent=(page+1)+' / '+pages;previous.disabled=page===0;next.disabled=page>=pages-1;
}
input.addEventListener('input',()=>{page=0;render()});previous.addEventListener('click',()=>{page--;render()});next.addEventListener('click',()=>{page++;render()});
for(const button of table.tHead.querySelectorAll('[data-column]'))button.addEventListener('click',()=>{
 const index=Number(button.dataset.column);ascending=column===index?!ascending:true;column=index;
 ordered=original.slice().sort((left,right)=>{const a=left.cells[index].textContent,b=right.cells[index].textContent;return a.localeCompare(b,undefined,{numeric:true,sensitivity:'base'})*(ascending?1:-1)});
 for(const row of ordered)table.tBodies[0].appendChild(row);
 for(const header of table.tHead.querySelectorAll('th'))header.removeAttribute('aria-sort');button.parentNode.setAttribute('aria-sort',ascending?'ascending':'descending');page=0;render();
});
document.getElementById('report-print').addEventListener('click',()=>window.print());render();
})();
'''


def _metric_cards(facts, row_count, *, language):
    zh = language == 'zh-hans'
    cards = [('报告输出行数' if zh else 'Report Output Rows', facts.get('row_count', row_count),
              '完整所选报告范围' if zh else 'Complete selected report')]
    for metric in facts.get('metrics', [])[:5]:
        cards.append((metric['label'], metric.get('value'), metric.get('op', '')))
    if len(cards) == 1:
        for field, stats in list(facts.get('numeric_fields', {}).items())[:5]:
            cards.append((field, stats.get('avg'), '平均值' if zh else 'Average'))
    html = '<div class="report-metrics">'
    for label, value, note in cards:
        html += '<article class="report-metric"><span>' + escape(_display(label)) + '</span><strong>' + escape(_display(value) if value is not None else '—') + '</strong><small>' + escape(_display(note)) + '</small></article>'
    return html + '</div>'


def _aggregate_details(facts, *, language):
    if not facts:
        return ''
    zh = language == 'zh-hans'
    lines = []
    def add(scope, values):
        lines.append((scope, '行数' if zh else 'Rows', values.get('row_count', ''), '', ''))
        for metric in values.get('metrics', []):
            quality = metric.get('quality', {})
            lines.append((scope, metric['label'], metric.get('value'), quality.get('missing', 0), quality.get('non_numeric', 0)))
        for field, stats in values.get('numeric_fields', {}).items():
            for key in ('count', 'sum', 'avg', 'min', 'max'):
                lines.append((scope, str(field) + ' · ' + key, stats.get(key), stats.get('missing', 0), stats.get('non_numeric', 0)))
    add('全部报告' if zh else 'All Report Rows', facts)
    for group in facts.get('groups', []):
        label = '; '.join(str(key) + '=' + _display(value) for key, value in group.get('keys', {}).items())
        add(label, group)
    headings = ('范围', '指标', '数值', '缺失', '非数值') if zh else ('Scope', 'Metric', 'Value', 'Missing', 'Non-Numeric')
    result = '<details id="aggregate-details"><summary>' + ('查看全部汇总数据' if zh else 'View All Aggregate Figures') + '</summary><div class="report-table-wrap"><table><thead><tr>'
    result += ''.join('<th>' + heading + '</th>' for heading in headings) + '</tr></thead><tbody>'
    for line in lines:
        result += '<tr>' + ''.join('<td>' + escape(_display(value)) + '</td>' for value in line) + '</tr>'
    result += '</tbody></table></div>'
    if facts.get('omitted_groups'):
        result += '<p class="report-footnote">' + escape((f"分组明细另省略 {facts['omitted_groups']} 组；整体汇总覆盖全部数据。" if zh else f"{facts['omitted_groups']} groups are omitted from group detail; overall figures cover the full result.")) + '</p>'
    return result + '</details>'


def _script_json(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False)
            .replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
            .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def render_html_report(spec, headers, rows, digest=None, *, report_url=None, source_links=None, audit=None):
    """Build an offline dashboard from escaped data and trusted bundled scripts."""
    rows = list(rows)
    headers = list(headers)
    if (not 1 <= len(headers) <= MAX_REPORT_COLUMNS or len(rows) > MAX_REPORT_ROWS
            or any(not isinstance(header, str) for header in headers)
            or any(not isinstance(row, dict) for row in rows)):
        raise ValueError('HTML 报告超出行列范围，请缩小报告或改用链接发送。')
    digest = digest or {}
    settings = spec.get('digest', {})
    language = settings.get('language', digest.get('language', 'en'))
    zh = language == 'zh-hans'
    title = _display(spec.get('title') or 'EnGenie Report')
    from .report_chart_specs import build_charts, chart_assets, chart_styles, render_charts_html
    charts = build_charts(headers, rows, settings.get('charts'), language=language)
    charts_markup = render_charts_html(charts, language=language)
    nonce = secrets.token_urlsafe(20)
    facts = digest.get('facts') or {}
    parts = ['<!doctype html><html lang="' + ('zh-Hans' if zh else 'en') + '"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta name="referrer" content="no-referrer">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'nonce-' + nonce + '\'; style-src \'unsafe-inline\'; connect-src \'none\'; img-src data:; object-src \'none\'; base-uri \'none\'; form-action \'none\'">',
             '<title>' + escape(title) + '</title>',
             '<style>' + _REPORT_CSS + chart_styles() + '</style></head><body><main class="report-shell">',
             '<header class="report-hero"><div class="report-eyebrow">EnGenie · Intelligence Digest</div><h1>' + escape(title) + '</h1><div class="report-meta"><span>' + str(len(rows)) + (' 条报告输出' if zh else ' report output rows') + '</span>' + ('<span>' + escape(_display(digest['generated_at'])) + '</span>' if digest.get('generated_at') else '') + '</div></header>',
             '<nav class="report-nav" aria-label="' + ('报告导航' if zh else 'Report Navigation') + '"><a href="#overview">' + ('摘要' if zh else 'Overview') + '</a>' + ('<a href="#charts">' + ('图表' if zh else 'Charts') + '</a>' if charts_markup else '') + '<a href="#report-data">' + ('数据' if zh else 'Report Data') + '</a><button type="button" id="report-print" class="js-only">' + ('打印 / 保存 PDF' if zh else 'Print / Save PDF') + '</button></nav>',
             _metric_cards(facts, len(rows), language=language),
             '<section class="report-section" id="overview"><h2>EnGenie Digest</h2>']
    if digest.get('text'):
        parts.append('<p class="report-summary">' + escape(_display(digest['text'])) + '</p>')
    else:
        parts.append('<p class="report-summary">' + ('以下内容基于本次所选报告数据。' if zh else 'This dashboard presents the selected report data.') + '</p>')
    parts.append(_aggregate_details(facts, language=language) + '</section>')
    parts.append(charts_markup)
    if settings.get('include_source_links'):
        selected_links = source_links if source_links is not None else extract_source_links(headers, rows)
        parts.append('<section class="report-section report-sources">' + source_links_html(selected_links, language=language) + '</section>')
    if report_url is not None:
        valid = safe_source_url(report_url)
        if valid is None:
            raise ValueError('报告下载链接无效，无法生成 HTML 报告。')
        parts.append('<p><a href="' + escape(valid, quote=True) + '" rel="noopener noreferrer">' + ('下载完整报告' if zh else 'Download Report') + '</a></p>')
    template = '匹配 {matched} / {total} 条' if zh else '{matched} of {total} rows'
    parts.append('<section class="report-section" id="report-data"><h2>' + ('报告数据' if zh else 'Report Data') + '</h2><div class="report-table-toolbar js-only"><label class="report-search" for="report-search">' + ('搜索数据' if zh else 'Search Data') + '<input id="report-search" type="search" placeholder="' + ('搜索任意字段…' if zh else 'Search any field…') + '"></label><span class="report-table-status" id="report-table-status" aria-live="polite" data-template="' + escape(template, quote=True) + '"></span></div><div class="report-table-wrap"><table id="report-table"><thead><tr>')
    parts.extend('<th><button type="button" class="report-sort" data-column="' + str(index) + '">' + escape(header) + ' <span aria-hidden="true">↕</span></button></th>' for index, header in enumerate(headers))
    parts.append('</tr></thead><tbody>')
    for row in rows:
        parts.append('<tr>')
        for header in headers:
            value = _display(row.get(header))
            url = safe_source_url(value) if settings.get('include_source_links') else None
            rendered = ('<a href="' + escape(url, quote=True) + '" rel="noopener noreferrer">' + escape(value) + '</a>') if url else escape(value)
            parts.append('<td>' + rendered.replace('\n', '<br>') + '</td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div><div class="report-pagination js-only"><button id="report-prev" type="button">' + ('上一页' if zh else 'Previous') + '</button><span id="report-page-label" class="report-table-status"></span><button id="report-next" type="button">' + ('下一页' if zh else 'Next') + '</button></div></section>')
    if audit:
        parts.append('<section class="report-section"><h2>' + ('转换质量' if zh else 'Conversion Quality') + '</h2><ul>')
        for field, state in audit.items():
            parts.append('<li>' + escape(str(field)) + ': ' + escape(_display(state.get('converted', 0))) + (' 条有效；' if zh else ' valid; ') + escape(_display(state.get('missing', 0))) + (' 条缺失或未解析' if zh else ' missing or unparsed') + '</li>')
        parts.append('</ul></section>')
    parts.append('<footer class="report-footer"><span>EnGenie · SHDR RAG</span><span>' + ('此报告可离线查看，图表与数据不访问外部网络。' if zh else 'Offline report · Charts and data make no external requests.') + '</span></footer></main>')
    parts.append('<script type="application/json" id="engenie-chart-data" nonce="' + nonce + '">' + _script_json(charts) + '</script>')
    for script in (chart_assets(), _TABLE_SCRIPT):
        trusted = re.sub(r'</script', r'<\\/script', script, flags=re.I)
        parts.append('<script nonce="' + nonce + '">' + trusted + '</script>')
    parts.append('</body></html>')
    result = ''.join(parts)
    try:
        size = len(result.encode('utf-8'))
    except UnicodeError as exc:
        raise ValueError('HTML 报告包含无效文字编码。') from exc
    if size > MAX_HTML_BYTES:
        raise ValueError('HTML 报告超过 1 MB，请减少字段或日期范围，或改用链接发送；未截断报告。')
    return result


def email_html_attachment(name, html):
    """Power Automate SendEmailV2 item; map the array to email Attachments."""
    if not isinstance(name, str):
        raise ValueError('HTML 附件文件名无效。')
    name = PurePosixPath(str(name).replace('\\', '/')).name
    if not name.strip('.') or _FILE_CONTROLS.search(name) or len(name) > 160:
        raise ValueError('HTML 附件文件名无效。')
    name = re.sub(r'[<>:"|?*]', '_', name)
    if not name.lower().endswith('.html'):
        name += '.html'
    if isinstance(html, str):
        try:
            payload = html.encode('utf-8')
        except UnicodeError as exc:
            raise ValueError('HTML 报告包含无效文字编码。') from exc
    else:
        raise ValueError('HTML 附件内容无效。')
    if not payload or len(payload) > MAX_HTML_BYTES:
        raise ValueError('HTML 附件为空或超过 1 MB，请改用链接发送。')
    return {'Name': name, 'ContentBytes': base64.b64encode(payload).decode('ascii')}
