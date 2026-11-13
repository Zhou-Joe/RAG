"""Explicit table-query libraries: preserve worksheet rows without model calls."""
from __future__ import annotations

import csv
import html
import io
import json
import math
import zipfile
from datetime import date, datetime, time
from pathlib import Path

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .spreadsheet_documents import MAX_COLUMNS, MAX_EXPANDED_BYTES, MAX_FILE_BYTES, MAX_ROWS

SPREADSHEET_TYPES = frozenset({'csv', 'tsv', 'xlsx', 'xlsm'})


def is_table_query_kb(kb):
    return bool(kb.table_query or (kb.parent_id and kb.parent.table_query))


def _value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('表格包含无法保存的非有限数值，请修正后重试。')
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _label(value):
    return '' if value is None else str(value)


def _parse_sheet(title, matrix):
    from openpyxl.utils import get_column_letter

    populated = []
    width = 0
    for number, source in enumerate(matrix, 1):
        if number > MAX_ROWS:
            raise ValueError('表格超过 100000 行，请拆分后上传；未保存部分数据。')
        values = list(source)
        while values and values[-1] is None:
            values.pop()
        if len(values) > MAX_COLUMNS:
            raise ValueError('表格超过 1000 列，请拆分后上传；未保存部分数据。')
        if not any(value is not None and value != '' for value in values):
            continue
        width = max(width, len(values))
        populated.append((number, [_value(value) for value in values]))
    if not populated:
        return {'sheet': title, 'header_row': None, 'columns': [], 'rows': []}

    header_number, header = populated[0]
    columns = []
    labels = []
    used = set()
    # Include cells beyond the header's width instead of dropping their values.
    for index in range(width):
        original = _label(header[index]) if index < len(header) else ''
        base = original if original.strip() else 'Column ' + get_column_letter(index + 1)
        label = base
        suffix = 2
        while label in used:
            label = f'{base} ({suffix})'
            suffix += 1
        used.add(label)
        labels.append(label)
        columns.append({'column': get_column_letter(index + 1),
                        'original_header': original, 'field': label})
    rows = [(number, {label: values[index] if index < len(values) else None
                      for index, label in enumerate(labels)})
            for number, values in populated[1:]]
    return {'sheet': title, 'header_row': header_number, 'columns': columns, 'rows': rows}


def read_spreadsheet_rows(path, file_type):
    """First nonempty row is the header; data rows retain original source positions."""
    if file_type not in SPREADSHEET_TYPES:
        raise ValueError('表格查询库仅支持 XLSX、XLSM、CSV 和 TSV 文件。')
    with Path(path).open('rb') as source:
        payload = source.read(MAX_FILE_BYTES + 1)
    if not payload:
        raise ValueError('上传的表格文件为空。')
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError('表格文件超过 25 MB，请拆分后上传。')
    if file_type in {'csv', 'tsv'}:
        encodings = (['utf-16'] if payload.startswith((b'\xff\xfe', b'\xfe\xff'))
                     else ['utf-8-sig', 'gb18030', 'latin-1'])
        for encoding in encodings:
            try:
                text = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t|')
        except csv.Error:
            dialect = csv.excel_tab if file_type == 'tsv' else csv.excel
        # The file-size bound already limits memory. Python's smaller process
        # default otherwise rejects an intact long text cell at 131072 chars.
        csv.field_size_limit(max(csv.field_size_limit(), MAX_FILE_BYTES))
        try:
            return [_parse_sheet('CSV' if file_type == 'csv' else 'TSV',
                                 csv.reader(io.StringIO(text), dialect))]
        except csv.Error as exc:
            raise ValueError(f'无法读取 CSV/TSV 表格：{exc}') from exc
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise ValueError('Excel 解压内容过大，请拆分后上传。')
        from openpyxl import load_workbook
        # Preserve formula source; do not execute formulas, macros or links.
        book = load_workbook(io.BytesIO(payload), read_only=True, data_only=False, keep_links=False)
        try:
            result = []
            for sheet in book.worksheets:
                sheet.reset_dimensions()
                result.append(_parse_sheet(sheet.title, sheet.iter_rows(values_only=True)))
            return result
        finally:
            book.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('无法读取 Excel 文件，请确认文件未损坏或加密。') from exc


def import_spreadsheet_document(document):
    """Replace one source's rows and publish its terminal state atomically."""
    from .models import Document, KbTracker, KnowledgeBase, TrackerRow

    sheets = read_spreadsheet_rows(document.file.path, document.file_type)
    total = sum(len(sheet['rows']) for sheet in sheets)
    if not total:
        raise ValueError('表格中没有可查询的数据行；首个非空行应为表头，后续行为数据。')
    root_id = document.kb.parent_id or document.kb_id
    manifest = {'kind': 'spreadsheet_rows', 'source': document.original_name,
                'row_count': total,
                'sheets': [{key: value for key, value in sheet.items() if key != 'rows'}
                           | {'row_count': len(sheet['rows'])} for sheet in sheets]}
    metadata = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False)
    with transaction.atomic():
        root = KnowledgeBase.objects.select_for_update().get(pk=root_id)
        current = Document.objects.select_for_update().select_related('kb__parent').get(pk=document.pk)
        if not is_table_query_kb(current.kb) or current.kb.category != 'manual':
            raise ValueError('该知识库未启用手动表格查询模式。')
        tracker, _ = KbTracker.objects.get_or_create(kb=root, defaults={'enabled': True})
        tracker = KbTracker.objects.select_for_update().get(pk=tracker.pk)
        fields = list(tracker.fields)
        labels = {field['label'] for field in fields}
        for sheet in sheets:
            for column in sheet['columns']:
                if column['field'] not in labels:
                    fields.append({'label': column['field']})
                    labels.add(column['field'])
        if tracker.fields != fields:
            tracker.fields = fields
            tracker.schema_version += 1
        tracker.enabled = True
        tracker.save(update_fields=['fields', 'enabled', 'schema_version', 'updated_at'])
        TrackerRow.objects.filter(document=current).delete()
        pending = []
        for sheet_index, sheet in enumerate(sheets, 1):
            for number, values in sheet['rows']:
                pending.append(TrackerRow(
                    tracker=tracker, document=current, source_sheet=sheet['sheet'],
                    source_row=number, external_id=f'sheet-{sheet_index}:row-{number}',
                    values=values, schema_version=tracker.schema_version,
                    status=TrackerRow.Status.DONE,
                ))
                if len(pending) >= 500:
                    TrackerRow.objects.bulk_create(pending, batch_size=500)
                    pending = []
        if pending:
            TrackerRow.objects.bulk_create(pending, batch_size=500)
        current.md_content = metadata
        current.html_content = ('<p>已按原始行列保存，可进行表格查询、筛选和统计。</p><pre>'
                                + html.escape(metadata) + '</pre>')
        current.html_built_at = timezone.now()
        current.chunk_count = 0
        current.status = Document.Status.COMPLETED
        current.stage_detail = f'已导入 {total} 行表格数据'
        current.error_msg = ''
        current.save(update_fields=['md_content', 'html_content', 'html_built_at', 'chunk_count',
                                    'status', 'stage_detail', 'error_msg', 'updated_at'])
        kb = current.kb
        complete = kb.documents.filter(status=Document.Status.COMPLETED)
        kb.doc_count = complete.count()
        kb.chunk_count = complete.aggregate(total=Sum('chunk_count'))['total'] or 0
        kb.save(update_fields=['doc_count', 'chunk_count', 'updated_at'])
    return total
