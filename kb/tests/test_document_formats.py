"""Offline source format extraction, using actual DOCX, MIME and CFB payloads."""
import io
import struct
import tempfile
import zipfile
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings

from kb.document_formats import office_email_to_markdown
from kb.pipeline import md_to_html, process_document, run_ocr_with_images


def docx_payload(body, extras=None):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml',
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<w:body>' + body + '</w:body></w:document>')
        for name, value in (extras or {}).items():
            archive.writestr(name, value)
    return result.getvalue()


def msg_payload(streams):
    """Small real CFB v3 fixture: 4096-byte streams avoid a mini-FAT."""
    free, end, fat = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
    entries = [{'name': 'Root Entry', 'type': 5, 'children': []}]
    storages = {'': 0}
    for path, data in streams.items():
        parent = ''
        for part in path.split('/')[:-1]:
            current = parent + '/' + part if parent else part
            if current not in storages:
                storages[current] = len(entries)
                entries[storages[parent]]['children'].append(len(entries))
                entries.append({'name': part, 'type': 1, 'children': []})
            parent = current
        entries[storages[parent]]['children'].append(len(entries))
        entries.append({'name': path.split('/')[-1], 'type': 2, 'children': [], 'data': data})
    directory_sectors = (len(entries) * 128 + 511) // 512
    sectors = [b''] * (1 + directory_sectors)
    allocation = [fat] + [index + 1 for index in range(1, directory_sectors)] + [end]
    for entry in entries:
        entry['start'] = end
        if entry['type'] == 2:
            entry['start'] = len(sectors)
            payload = entry['data'].ljust(4096, b'\0')
            for offset in range(0, len(payload), 512):
                sectors.append(payload[offset:offset + 512])
                allocation.append(len(allocation) + 1 if offset + 512 < len(payload) else end)
    directory = bytearray()
    for index, entry in enumerate(entries):
        name = (entry['name'] + '\0').encode('utf-16-le')
        siblings = next((item['children'] for item in entries if index in item['children']), [])
        sibling_index = siblings.index(index) if siblings else -1
        right = siblings[sibling_index + 1] if sibling_index + 1 < len(siblings) else free
        directory.extend(name.ljust(64, b'\0') + struct.pack('<HBBIII', len(name), entry['type'], 1,
            free, right, entry['children'][0] if entry['children'] else free)
            + b'\0' * 36 + struct.pack('<IQ', entry['start'], 4096 if entry['type'] == 2 else 0))
    directory.extend(b'\0' * (directory_sectors * 512 - len(directory)))
    for index in range(directory_sectors):
        sectors[index + 1] = directory[index * 512:(index + 1) * 512]
    sectors[0] = struct.pack('<128I', *(allocation + [free] * (128 - len(allocation))))
    header = (bytes.fromhex('D0CF11E0A1B11AE1') + b'\0' * 16
        + struct.pack('<HHHHH', 0x3E, 3, 0xFFFE, 9, 6) + b'\0' * 6
        + struct.pack('<IIIIIIIII', 0, 1, 1, 0, 4096, end, 0, end, 0)
        + struct.pack('<109I', 0, *([free] * 108)))
    return header + b''.join(sectors)


class DocumentFormatTests(SimpleTestCase):
    def parse(self, payload, kind):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ('source.' + kind)
            path.write_bytes(payload)
            return office_email_to_markdown(path, kind)

    def test_docx_preserves_heading_table_unicode_and_order(self):
        payload = docx_payload(
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>设备报告</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>对象</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>时长</w:t></w:r></w:p></w:tc></w:tr>'
            '<w:tr><w:tc><w:p><w:r><w:t>旋转木马</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>90</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '<w:p><w:r><w:t>结束 &lt;script&gt;</w:t></w:r></w:p>')
        md, images = self.parse(payload, 'docx')
        self.assertIn('# 设备报告', md)
        self.assertIn('<td><p>旋转木马</p></td><td><p>90</p></td>', md)
        self.assertLess(md.index('设备报告'), md.index('旋转木马'))
        self.assertLess(md.index('旋转木马'), md.index('结束'))
        self.assertIn('&lt;script&gt;', md_to_html(md))
        self.assertEqual(images, {})

    def test_docx_images_are_local_hashed_assets(self):
        payload = docx_payload('<w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>', {
            'word/_rels/document.xml.rels': '<Relationships><Relationship Id="rId1" Target="media/pic.png"/></Relationships>',
            'word/media/pic.png': b'png-test-content',
        })
        md, images = self.parse(payload, 'docx')
        self.assertEqual(len(images), 1)
        filename = next(iter(images))
        self.assertRegex(filename, r'^[0-9a-f]{64}\.png$')
        self.assertIn('images/' + filename, md)

    def test_docx_text_cannot_introduce_remote_markdown_images(self):
        payload = docx_payload('<w:p><w:r><w:t>![remote](https://example.com/pixel.png)</w:t></w:r></w:p>')
        md, images = self.parse(payload, 'docx')
        self.assertNotIn('<img', md_to_html(md))
        self.assertIn('example.com/pixel.png', md_to_html(md))
        self.assertFalse(images)

    def test_docx_literal_punctuation_and_html_are_displayed_once(self):
        payload = docx_payload('<w:p><w:r><w:t>A_1 * B &amp; &lt;tag&gt; [x]</w:t></w:r></w:p>')
        md, _ = self.parse(payload, 'docx')
        rendered = md_to_html(md)
        self.assertIn('A_1 * B &amp; &lt;tag&gt; [x]', rendered)
        self.assertNotIn('A\\_1', rendered)
        self.assertNotIn('&amp;amp;', rendered)

    def test_bad_docx_and_expansion_limit_fail_explicitly(self):
        with self.assertRaisesMessage(ValueError, 'DOCX'):
            self.parse(b'not a document', 'docx')
        with patch('kb.document_formats.MAX_DOCX_EXPANDED_BYTES', 20):
            with self.assertRaisesMessage(ValueError, '解压内容过大'):
                self.parse(docx_payload('<w:p/>'), 'docx')

    def test_eml_preserves_decoded_headers_body_and_attachment_names(self):
        message = EmailMessage()
        message['Subject'] = '每日运营报告'
        message['From'] = 'Alice <alice@example.com>'
        message['To'] = 'bob@example.com'
        message['Bcc'] = 'private@example.com'
        message['Date'] = 'Wed, 11 Nov 2026 10:00:00 +0800'
        message.set_content('今日开放：旋转木马 09:00—21:00')
        message.add_alternative('<p>Duplicate HTML body</p>', subtype='html')
        message.add_attachment(b'private attachment body', maintype='application',
                               subtype='octet-stream', filename='附件.xlsx')
        md, images = self.parse(message.as_bytes(), 'eml')
        self.assertIn('每日运营报告', md)
        self.assertIn('alice@example.com', md)
        self.assertIn('旋转木马', md)
        self.assertIn('附件.xlsx', md)
        self.assertIn('**Bcc:** private@example.com', md)
        self.assertNotIn('private attachment body', md)
        self.assertNotIn('Duplicate HTML body', md)
        self.assertEqual(images, {})

    def test_html_only_email_reads_text_without_remote_resources(self):
        message = EmailMessage()
        message['Subject'] = 'HTML report'
        message.set_content('<script>execute me</script><p>Temperature</p>'
                            '<table><tr><td>设备</td><td>27</td></tr></table>'
                            '<img src="https://example.com/pixel">', subtype='html')
        md, _ = self.parse(message.as_bytes(), 'eml')
        self.assertIn('Temperature', md)
        self.assertIn('设备', md)
        self.assertIn('27', md)
        self.assertNotIn('execute me', md)
        self.assertNotIn('https://', md)

    def test_email_markdown_is_literal_and_html_links_are_preserved_as_text(self):
        message = EmailMessage()
        message['Subject'] = '![tracking](https://example.com/subject.png)'
        message.set_content('![tracking](https://example.com/body.png)\n```\n'
                            '<img src="https://example.com/raw.png">')
        md, _ = self.parse(message.as_bytes(), 'eml')
        rendered = md_to_html(md)
        self.assertNotIn('<img', rendered)
        self.assertIn('example.com/body.png', rendered)
        message.set_content('<p><a href="https://example.com/report">Download report</a></p>', subtype='html')
        md, _ = self.parse(message.as_bytes(), 'eml')
        self.assertIn('Download report (https://example.com/report)', md)

    def test_matching_fences_preserve_normal_code_blocks_and_nested_shorter_fences(self):
        normal = md_to_html('```python\nprint(1)\n```\n\nAfter code')
        self.assertIn('<pre><code>print(1)</code></pre>', normal)
        self.assertIn('<p>After code</p>', normal)
        nested = md_to_html('````text\n```\n<img src="https://example.com/pixel">\n````')
        self.assertNotIn('<img', nested)
        self.assertIn('```', nested)

    def test_msg_unicode_body_sender_and_recipient(self):
        def string(value):
            return (value + '\0').encode('utf-16-le')
        payload = msg_payload({
            '__substg1.0_0037001F': string('运营邮件'),
            '__substg1.0_1000001F': string('设备运行正常。'),
            '__substg1.0_0C1A001F': string('Alice'),
            '__substg1.0_5D01001F': string('alice@example.com'),
            '__recip_version1.0_#00000000/__substg1.0_39FE001F': string('bob@example.com'),
            '__attach_version1.0_#00000000/__substg1.0_3707001F': string('报表.xlsx'),
        })
        md, _ = self.parse(payload, 'msg')
        for value in ('运营邮件', '设备运行正常。', 'alice@example.com', 'bob@example.com', '报表.xlsx'):
            self.assertIn(value, md)

    def test_msg_ansi_codepage_and_sent_date(self):
        sent = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)
        ticks = int((sent - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds()) * 10_000_000
        properties = b'\0' * 32 + struct.pack('<IIQ', 0x3FFD0003, 0, 1252) + struct.pack('<IIQ', 0x00390040, 0, ticks)
        payload = msg_payload({'__properties_version1.0': properties,
            '__substg1.0_0037001E': b'Caf\xe9\0', '__substg1.0_1000001E': b'R\xe9sum\xe9\0'})
        md, _ = self.parse(payload, 'msg')
        self.assertIn('Café', md)
        self.assertIn('Résumé', md)
        self.assertIn('2026-11-11T02:00:00+00:00', md)

    def test_msg_blind_recipients_are_not_labeled_as_to(self):
        payload = msg_payload({
            '__substg1.0_1000001F': '正文\0'.encode('utf-16-le'),
            '__recip_version1.0_#00000000/__properties_version1.0':
                b'\0' * 8 + struct.pack('<IIQ', 0x0C150003, 0, 3),
            '__recip_version1.0_#00000000/__substg1.0_39FE001F': 'secret@example.com\0'.encode('utf-16-le'),
        })
        md, _ = self.parse(payload, 'msg')
        self.assertIn('**Bcc:** secret@example.com', md)
        self.assertNotIn('**To:** secret@example.com', md)

    def test_msg_html_and_rtf_only_failure(self):
        md, _ = self.parse(msg_payload({'__substg1.0_10130102': b'<p>Useful body</p>'}), 'msg')
        self.assertIn('Useful body', md)
        with self.assertRaisesMessage(ValueError, 'RTF'):
            self.parse(msg_payload({'__substg1.0_10090102': b'compressed rtf'}), 'msg')

    def test_invalid_email_and_input_or_text_limits(self):
        for payload, kind in ((b'plain text', 'eml'), (b'not an ole file', 'msg')):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.parse(payload, kind)
        message = EmailMessage()
        message['Subject'] = 'test'
        message.set_content('long source')
        with patch('kb.document_formats.MAX_INPUT_BYTES', 2):
            with self.assertRaisesMessage(ValueError, '25 MB'):
                self.parse(message.as_bytes(), 'eml')
        with patch('kb.document_formats.MAX_TEXT_CHARS', 10):
            with self.assertRaisesMessage(ValueError, '未保存截断内容'):
                self.parse(message.as_bytes(), 'eml')

    def test_pipeline_uses_local_email_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mail.eml'
            path.write_bytes(b'Subject: Report\r\n\r\nLocal source body')
            with patch('kb.pipeline._ocr_pdf') as remote_pdf:
                md, images, layout = run_ocr_with_images(path, 'eml')
            self.assertIn('Local source body', md)
            self.assertEqual(images, {})
            self.assertIsNone(layout)
            remote_pdf.assert_not_called()


class NewFormatPipelineTests(TestCase):
    def test_word_email_and_spreadsheet_files_index_and_start_tracksheet_extraction(self):
        from kb.models import Document, KnowledgeBase
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(['Attraction', 'Duration'])
        workbook.active.append(['Carousel', 90])
        spreadsheet = io.BytesIO()
        workbook.save(spreadsheet)
        email = b'Subject: Operation report\r\n\r\nCarousel duration 90 minutes'
        word = docx_payload('<w:p><w:r><w:t>Carousel duration 90 minutes</w:t></w:r></w:p>')
        outlook = msg_payload({'__substg1.0_1000001F': 'Carousel duration 90 minutes\0'.encode('utf-16-le')})
        sources = [('docx', word), ('eml', email), ('msg', outlook), ('xlsx', spreadsheet.getvalue()),
                   ('csv', b'Attraction,Duration\nCarousel,90\n')]
        with tempfile.TemporaryDirectory() as directory, override_settings(
                MEDIA_ROOT=Path(directory) / 'media', DATA_DIR=Path(directory) / 'data'):
            kb = KnowledgeBase.objects.create(name='Existing KB', slug='new-format-pipeline')
            for kind, raw in sources:
                with self.subTest(kind=kind):
                    doc = Document(kb=kb, original_name='source.' + kind, file_type=kind)
                    doc.file.save(doc.original_name, ContentFile(raw), save=True)
                    with patch('kb.pipeline.run_indexing', return_value=2) as index, \
                         patch('kb.pipeline._ocr_pdf') as remote_pdf, \
                         patch('kb.tracker.run_extraction_async') as extraction:
                        process_document(str(doc.pk))
                    doc.refresh_from_db()
                    self.assertEqual(doc.status, Document.Status.COMPLETED, doc.error_msg)
                    self.assertEqual(doc.chunk_count, 2)
                    self.assertIn('Carousel', doc.md_content)
                    self.assertTrue(doc.html_content)
                    self.assertEqual(index.call_args.args[1:3], (kb.slug, doc.original_name))
                    extraction.assert_called_once_with(doc.pk)
                    remote_pdf.assert_not_called()
            self.assertEqual(KnowledgeBase.objects.count(), 1)
