"""Offline Word and email extraction; no network requests or attachment execution.

Limits are checked explicitly. Oversized or unreadable content fails ingestion
instead of silently producing a partial document.
"""
from __future__ import annotations

import base64
import hashlib
import html
import mimetypes
import posixpath
import re
import struct
import zipfile
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 5_000_000
MAX_DOCX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_DOCX_MEMBERS = 5000
MAX_EMAIL_PARTS = 2000
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'


def _checked_text(text):
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError('提取内容超过 500 万字符，请拆分文件后上传；未保存截断内容。')
    if not text.strip():
        raise ValueError('文件没有可读取的文字或图片。')
    return text.strip()


def _payload(path):
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError('Word 或邮件文件不能超过 25 MB，请拆分后上传。')
    raw = path.read_bytes()
    if not raw:
        raise ValueError('文件为空。')
    return raw


class _HTMLText(HTMLParser):
    """Keep HTML email text and table cell boundaries; discard active content."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self.hidden += 1
        if not self.hidden:
            if tag == 'a':
                self.links.append(dict(attrs).get('href', ''))
            if tag in ('p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'blockquote'):
                self.parts.append('\n')
            elif tag in ('td', 'th'):
                self.parts.append('\t')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head') and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in ('p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'blockquote'):
            self.parts.append('\n')
        elif not self.hidden and tag == 'a' and self.links:
            target = self.links.pop()
            if target:
                self.parts.append(' (' + target + ')')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_to_text(markup):
    reader = _HTMLText()
    reader.feed(markup)
    return re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', ''.join(reader.parts)).strip()


def _literal(text):
    """Plain source text is not interpreted as arbitrary HTML in the viewer."""
    return html.escape(str(text), quote=False)


def _source_literal(text):
    return re.sub(r'([\\`*_{}\[\]()#!|~<>])', r'\\\1', str(text))


def _decode(raw, charset=None):
    for encoding in dict.fromkeys([charset, 'utf-8', 'gb18030', 'windows-1252']):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    raise ValueError('邮件文字编码无法识别，请另存为 UTF-8 EML 文件。')


def _email_markdown(headers, body, attachments):
    literal = _source_literal
    lines = ['# ' + literal(headers.get('Subject') or 'Email record')]
    for key in ('From', 'To', 'Cc', 'Bcc', 'Date', 'Message-ID'):
        if headers.get(key):
            lines.append('**' + key + ':** ' + literal(headers[key]))
    if body.strip():
        fence = '`' * max(3, 1 + max([len(match) for match in re.findall(r'`+', body)] or [0]))
        # Source Markdown in an email is literal text. A dynamic fence prevents
        # remote image syntax or embedded fences from becoming viewer content.
        lines.extend(['', fence + 'text\n' + body + '\n' + fence])
    if attachments:
        lines.extend(['', '## Attachments', *['- ' + literal(name) for name in attachments]])
    return _checked_text('\n\n'.join(lines))


def _read_eml(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    parts = list(message.walk())
    if len(parts) > MAX_EMAIL_PARTS:
        raise ValueError('邮件超过 2000 个 MIME 部分，请拆分后上传。')
    if not any(message.get(key) for key in ('Subject', 'From', 'To', 'Date', 'Message-ID')):
        raise ValueError('不是有效的 EML 邮件文件：缺少邮件头。')
    selected = message.get_body(preferencelist=('plain', 'html'))
    body = ''
    if selected is not None:
        payload = selected.get_payload(decode=True) or b''
        body = _decode(payload, selected.get_content_charset())
        if selected.get_content_type() == 'text/html':
            body = html_to_text(body)
    attachments = [str(part.get_filename() or 'unnamed attachment') for part in parts
                   if part.get_content_disposition() == 'attachment' or part.get_filename()]
    headers = {key: str(message.get(key) or '') for key in ('Subject', 'From', 'To', 'Cc', 'Bcc', 'Date', 'Message-ID')}
    return _email_markdown(headers, body, attachments)


def _read_docx(raw):
    import io
    images = {}
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError('不是有效的 DOCX 文件；旧版 Word 请另存为 .docx。') from exc
    with archive:
        members = archive.infolist()
        if len(members) > MAX_DOCX_MEMBERS or sum(item.file_size for item in members) > MAX_DOCX_EXPANDED_BYTES:
            raise ValueError('DOCX 解压内容过大，请拆分后上传。')
        if any(item.flag_bits & 1 for item in members):
            raise ValueError('无法读取加密的 DOCX，请先取消密码保护。')
        def xml(name):
            try:
                value = archive.read(name)
                if b'<!DOCTYPE' in value.upper() or b'<!ENTITY' in value.upper():
                    raise ValueError('DOCX 包含不支持的 XML 实体。')
                return ET.fromstring(value)
            except (ET.ParseError, KeyError) as exc:
                raise ValueError('DOCX 内容损坏或缺少正文。') from exc
        document = xml('word/document.xml')
        names = {item.filename for item in members}
        rels = {}
        if 'word/_rels/document.xml.rels' in names:
            for relation in xml('word/_rels/document.xml.rels'):
                if relation.get('TargetMode') != 'External':
                    rels[relation.get('Id')] = posixpath.normpath('word/' + relation.get('Target', ''))
        def paragraph(node):
            content = []
            for child in node.iter():
                if child.tag == W + 't':
                    content.append(_source_literal(child.text or ''))
                elif child.tag == W + 'tab':
                    content.append('\t')
                elif child.tag in (W + 'br', W + 'cr'):
                    content.append('\n')
                elif child.tag == A + 'blip':
                    target = rels.get(child.get(R + 'embed'), '')
                    extension = Path(target).suffix.lower()
                    if target in names and extension in ('.png', '.jpg', '.jpeg', '.webp'):
                        data = archive.read(target)
                        name = hashlib.sha256(data).hexdigest() + extension
                        mime = mimetypes.guess_type(name)[0] or 'image/png'
                        images[name] = 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')
                        content.append('\n![](images/' + name + ')\n')
            text = ''.join(content).strip()
            style = node.find('./' + W + 'pPr/' + W + 'pStyle')
            heading = re.search(r'(?i)^heading([1-6])$', style.get(W + 'val', '')) if style is not None else None
            return ('#' * int(heading.group(1)) + ' ' if heading else '') + text
        def table(node):
            from .md2html import md_to_html
            rows = []
            for row in node.findall(W + 'tr'):
                cells = []
                for cell in row.findall(W + 'tc'):
                    text = ''.join(md_to_html(paragraph(p)).replace('\n', '') if p.tag == W + 'p' else table(p)
                                       for p in cell if p.tag in (W + 'p', W + 'tbl'))
                    cells.append('<td>' + text + '</td>')
                rows.append('<tr>' + ''.join(cells) + '</tr>')
            if not rows:
                return ''
            return '<table>' + ''.join(rows) + '</table>'
        body = document.find(W + 'body')
        if body is None:
            raise ValueError('DOCX 缺少正文。')
        blocks = [paragraph(node) if node.tag == W + 'p' else table(node)
                  for node in body if node.tag in (W + 'p', W + 'tbl')]
        for name in sorted(names):
            if re.fullmatch(r'word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml', name):
                extra = xml(name)
                text = '\n\n'.join(paragraph(node) for node in extra.iter(W + 'p'))
                if text.strip():
                    blocks.append('## ' + Path(name).stem + '\n\n' + text)
        return _checked_text('\n\n'.join(block for block in blocks if block.strip())), images


def _read_msg(raw):
    import io
    import olefile
    try:
        archive = olefile.OleFileIO(io.BytesIO(raw))
    except (OSError, IOError, ValueError) as exc:
        raise ValueError('不是有效的 Outlook MSG 文件。') from exc
    with archive:
        paths = archive.listdir(streams=True, storages=False)
        if not any(path[-1].startswith('__substg1.0_') for path in paths):
            raise ValueError('不是有效的 Outlook MSG 邮件。')
        def properties(storage=None):
            path = ([storage] if storage else []) + ['__properties_version1.0']
            if not archive.exists(path):
                return {}
            data = archive.openstream(path).read()
            offset = 8 if storage else 32
            return {struct.unpack_from('<I', data, index)[0]: data[index + 8:index + 16]
                    for index in range(offset, len(data) - 15, 16)}
        props = properties()
        codepage = next((struct.unpack('<I', props[tag][:4])[0]
                         for tag in (0x3FFD0003, 0x3FDE0003) if tag in props), 1252)
        def text(prop, storage=None):
            prefix = [storage] if storage else []
            for kind, encoding in (('001F', 'utf-16-le'), ('001E', 'cp' + str(codepage))):
                stream = prefix + ['__substg1.0_' + prop + kind]
                if archive.exists(stream):
                    data = archive.openstream(stream).read()
                    return _decode(data, encoding).rstrip('\x00')
            return ''
        headers_text = text('007D')
        headers_message = BytesParser(policy=policy.default).parsebytes(headers_text.encode('utf-8')) if headers_text else None
        headers = {key: str(headers_message.get(key) or '') if headers_message else ''
                   for key in ('Subject', 'From', 'To', 'Cc', 'Bcc', 'Date', 'Message-ID')}
        headers['Subject'] = text('0037') or headers['Subject']
        sender_name, sender_email = text('0C1A'), text('5D01') or text('0C1F')
        headers['From'] = headers['From'] or (f'{sender_name} <{sender_email}>' if sender_email else sender_name)
        recipients = {}
        copied = {}
        blind_copied = {}
        attachments = []
        for path in paths:
            storage = path[0]
            if storage.startswith('__recip_version1.0_'):
                address = text('39FE', storage) or text('3003', storage) or text('3001', storage)
                recipient_type = properties(storage).get(0x0C150003, b'\x01\x00\x00\x00')
                kind = struct.unpack('<I', recipient_type[:4])[0]
                (copied if kind == 2 else blind_copied if kind == 3 else recipients)[storage] = address
            elif storage.startswith('__attach_version1.0_'):
                name = text('3707', storage) or text('3704', storage) or text('3001', storage)
                if name and name not in attachments:
                    attachments.append(name)
        headers['To'] = headers['To'] or '; '.join(value for value in recipients.values() if value)
        headers['Cc'] = headers['Cc'] or '; '.join(value for value in copied.values() if value)
        headers['Bcc'] = headers['Bcc'] or '; '.join(value for value in blind_copied.values() if value)
        if not headers['Date']:
            ticks = next((struct.unpack('<Q', props[tag])[0]
                          for tag in (0x00390040, 0x0E060040) if tag in props), None)
            if ticks is not None:
                try:
                    headers['Date'] = (datetime(1601, 1, 1, tzinfo=timezone.utc)
                                       + timedelta(microseconds=ticks // 10)).isoformat()
                except OverflowError:
                    pass
        body = text('1000')
        if not body.strip() and archive.exists('__substg1.0_10130102'):
            markup = _decode(archive.openstream('__substg1.0_10130102').read())
            body = html_to_text(markup)
        if not body.strip():
            markup = text('1013')
            if markup:
                body = html_to_text(markup)
        if not body.strip() and archive.exists('__substg1.0_10090102'):
            raise ValueError('此 MSG 仅含 RTF 正文，请在 Outlook 中另存为 EML 或 DOCX 后上传。')
        if not body.strip() and not any(headers.values()) and not attachments:
            raise ValueError('MSG 中没有可读取的邮件内容。')
        return _email_markdown(headers, body, attachments)


def office_email_to_markdown(path: Path, file_type: str):
    """Return Markdown and protected embedded DOCX image assets."""
    raw = _payload(path)
    if file_type == 'docx':
        return _read_docx(raw)
    if file_type == 'eml':
        return _read_eml(raw), {}
    if file_type == 'msg':
        return _read_msg(raw), {}
    raise ValueError('旧版 Word .doc 请另存为 .docx 后上传。')
