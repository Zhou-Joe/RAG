"""Read uploaded spreadsheets as documents, preserving sheets and source rows."""
from __future__ import annotations

import csv
import html
import io
import re
import zipfile
from pathlib import Path

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 5_000_000
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 1_000


def _text(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _cell(value):
    # Source markup is text; only our own line-break tag is rendered as HTML.
    return html.escape(_text(value), quote=False).replace("\r\n", "\n").replace(
        "\r", "\n").replace("\n", "<br>")


def _sheet_markdown(title, matrix, budget):
    from openpyxl.utils import get_column_letter

    rows = []
    width = 0
    for number, values in enumerate(matrix, 1):
        if number > MAX_ROWS:
            raise ValueError("表格超过 100000 行，请拆分后上传；未截断原文。")
        values = list(values)
        while values and values[-1] is None:
            values.pop()
        if not any(value is not None and _text(value) != "" for value in values):
            continue
        if len(values) > MAX_COLUMNS:
            raise ValueError("表格超过 1000 列，请拆分后上传；未截断原文。")
        rendered = [_cell(value) for value in values]
        budget[0] += sum(len(value) for value in rendered) + 3 * len(values) + 20
        if budget[0] > MAX_TEXT_CHARS:
            raise ValueError("表格正文超过 5000000 字符，请拆分后上传；未截断原文。")
        rows.append((number, rendered))
        width = max(width, len(values))
    heading = "## " + re.sub(r'([\\`*_{}\[\]()#!|~])', r'\\\1', str(title))
    if not rows:
        return heading + "\n\n（空工作表）"
    lines = ['<table><thead><tr><th>行号</th>' + ''.join(
        '<th>' + get_column_letter(index) + '</th>' for index in range(1, width + 1))
        + '</tr></thead><tbody>']
    budget[1] += len(heading) + len(lines[0])
    for number, values in rows:
        line = '<tr><td>' + str(number) + '</td>' + ''.join(
            '<td>' + value + '</td>' for value in values + [''] * (width - len(values))) + '</tr>'
        budget[1] += len(line) + 1
        if budget[1] > MAX_TEXT_CHARS:
            raise ValueError("表格正文超过 5000000 字符，请拆分后上传；未截断原文。")
        lines.append(line)
    lines.append('</tbody></table>')
    return heading + "\n\n" + ''.join(lines)


def spreadsheet_to_markdown(file_path: Path, file_type: str) -> str:
    with Path(file_path).open("rb") as source:
        payload = source.read(MAX_FILE_BYTES + 1)
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("表格文件超过 25 MB，请拆分后上传。")
    if not payload:
        raise ValueError("上传的表格文件为空。")
    budget = [0, 0]
    if file_type in ("csv", "tsv"):
        encodings = (["utf-16"] if payload.startswith((b"\xff\xfe", b"\xfe\xff"))
                     else ["utf-8-sig", "gb18030", "latin-1"])
        for encoding in encodings:
            try:
                text = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("无法识别表格编码，请另存为 UTF-8 后上传。")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel_tab if file_type == "tsv" else csv.excel
        try:
            return _sheet_markdown("CSV" if file_type == "csv" else "TSV",
                                   csv.reader(io.StringIO(text), dialect), budget)
        except csv.Error as exc:
            raise ValueError("无法读取 CSV/TSV 表格：" + str(exc)) from exc
    if file_type not in ("xlsx", "xlsm"):
        raise ValueError("不支持此表格格式，请另存为 XLSX 或 CSV。")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise ValueError("Excel 解压内容过大，请拆分后上传。")
        from openpyxl import load_workbook
        # Retain formulas as source text; never execute macros or external links.
        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=False,
                                 keep_links=False)
        try:
            sections = []
            for sheet in workbook.worksheets:
                sheet.reset_dimensions()
                sections.append(_sheet_markdown(sheet.title, sheet.iter_rows(values_only=True), budget))
            return "\n\n".join(sections)
        finally:
            workbook.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法读取 Excel 文件，请确认文件未损坏或加密。") from exc
