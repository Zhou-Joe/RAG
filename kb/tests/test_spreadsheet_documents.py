from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase
from openpyxl import Workbook

from kb.spreadsheet_documents import spreadsheet_to_markdown


class SpreadsheetDocumentTests(SimpleTestCase):
    def test_workbook_preserves_notes_sheets_dates_formulas_and_rows(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'report.xlsx'
            book = Workbook()
            sheet = book.active
            sheet.title = '运行记录'
            sheet.append(['月度原始说明'])
            sheet.append([])
            sheet.append(['设备', '数值', '日期', '公式'])
            sheet.append(['上海|设备\n<script>', 3.25, datetime(2026, 11, 11, 9, 30), '=B4*2'])
            book.create_sheet('另一工作表').append(['补充信息'])
            book.save(path)
            result = spreadsheet_to_markdown(path, 'xlsx')
        for expected in ('## 运行记录', '月度原始说明', '<tr><td>4</td>', '3.25',
                         '2026-11-11T09:30:00', '=B4*2', '## 另一工作表',
                         '补充信息', '上海|设备<br>&lt;script&gt;'):
            self.assertIn(expected, result)
        self.assertNotIn('<script>', result)
        from kb.md2html import md_to_html
        rendered = md_to_html(result)
        self.assertIn('<table>', rendered)
        self.assertIn('<td>3.25</td>', rendered)
        self.assertIn('上海|设备<br>', rendered)
        self.assertNotIn('<script>', rendered)

    def test_csv_keeps_first_row_long_values_unicode_and_quoted_newlines(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source.csv'
            path.write_text('说明,数据\n"含有\n换行",' + '原文' * 2000 + '\n', encoding='utf-8-sig')
            result = spreadsheet_to_markdown(path, 'csv')
        self.assertIn('说明', result)
        self.assertIn('含有<br>换行', result)
        self.assertIn('原文' * 2000, result)

    def test_oversized_extraction_fails_instead_of_returning_partial_table(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'large.tsv'
            path.write_text('内容\n' + 'x' * 101, encoding='utf-8')
            with patch('kb.spreadsheet_documents.MAX_TEXT_CHARS', 100):
                with self.assertRaisesMessage(ValueError, '未截断原文'):
                    spreadsheet_to_markdown(path, 'tsv')

    def test_corrupt_excel_reports_actionable_failure(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'broken.xlsx'
            path.write_bytes(b'not an excel file')
            with self.assertRaisesMessage(ValueError, '未损坏或加密'):
                spreadsheet_to_markdown(path, 'xlsx')
