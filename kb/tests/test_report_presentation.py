"""Safe source links and offline HTML artifacts; no workflow requests are made."""
import base64
from html.parser import HTMLParser
import json
import re
from unittest.mock import patch

from django.test import SimpleTestCase

from kb.report_digest import build_facts
from kb.report_presentation import (
    email_html_attachment, extract_source_links, render_html_report,
    safe_source_url, source_links_html, teams_source_actions,
)


class DocumentAudit(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.script_content = []
        self._script = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.elements.append((tag, values))
        if tag == 'script':
            self._script = {'attrs': values, 'text': ''}
            self.script_content.append(self._script)

    def handle_endtag(self, tag):
        if tag == 'script':
            self._script = None

    def handle_data(self, data):
        if self._script is not None:
            self._script['text'] += data


class SourceLinkTests(SimpleTestCase):
    def test_accepts_intact_absolute_web_links_including_encoded_spaces(self):
        for url in ('https://example.test/report?id=7&view=full#section',
                    'HTTP://EXAMPLE.TEST/My%20Report.pdf',
                    'https://例子.测试/资料'):
            with self.subTest(url=url):
                self.assertEqual(safe_source_url(url), url)

    def test_rejects_active_schemes_control_characters_and_credentials(self):
        values = ['javascript:alert(1)', 'data:text/html,<script>', 'file:///etc/passwd',
                  '//example.test/path', '\nhttps://example.test/', 'https://example.test/\r', 'https://user:password@example.test/',
                  'https://example.test/path?access_token=secret',
                  'https://example.test/path?X-Amz-Signature=secret',
                  'https://example.test/#token=secret', 'https://example.test/a\nscript',
                  'https://example.test/a%0d%0ascript', 'https://example.test/a%250ascript',
                  'https://example.test/" onclick="alert(1)', 'https://example.test:99999/',
                  'https://example.test\\@evil.test', 'https://host%40evil.test/']
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(safe_source_url(value))

    def test_extracts_only_selected_cells_and_discloses_omissions(self):
        rows = [{'Name': 'Ride <A>', 'Article': 'https://EXAMPLE.test/one', 'Hidden': 'https://hidden.test'},
                {'Name': 'Ride A', 'Article': 'https://example.test/one'},
                {'Name': 'Ride B', 'Article': 'https://example.test/two'},
                {'Name': 'Bad', 'Article': 'javascript:alert(1)'}]
        result = extract_source_links(['Name', 'Article'], rows, limit=1)
        self.assertEqual(result['total_links'], 2)
        self.assertEqual(result['omitted_links'], 1)
        self.assertEqual(result['rejected_links'], 1)
        self.assertEqual(result['links'], [{'label': 'Ride <A> · Article', 'url': 'https://EXAMPLE.test/one'}])
        self.assertNotIn('hidden.test', json.dumps(result))

    def test_renders_links_as_escaped_anchors_and_safe_card_actions(self):
        selection = {'links': [{'label': '<img src=x onerror=alert(1)>', 'url': 'https://example.test/?a=1&b=2'}],
                     'omitted_links': 3, 'rejected_links': 1}
        html = source_links_html(selection)
        self.assertIn('&lt;img', html)
        self.assertNotIn('<img', html)
        self.assertIn('?a=1&amp;b=2', html)
        self.assertIn('3 additional links', html)
        self.assertEqual(teams_source_actions(selection)[0]['type'], 'Action.OpenUrl')
        with self.assertRaises(ValueError):
            source_links_html({'links': [{'label': 'unsafe', 'url': 'javascript:alert(1)'}]})
        with self.assertRaises(ValueError):
            teams_source_actions({'links': [{'label': 'unsafe', 'url': 'data:text/html,x'}]})

    def test_unselected_and_prose_embedded_urls_are_not_promoted_to_links(self):
        rows = [{'Note': 'See https://example.test/article for details', 'Hidden': 'https://example.test/hidden'}]
        self.assertEqual(extract_source_links(['Note'], rows)['links'], [])


class HtmlReportTests(SimpleTestCase):
    def setUp(self):
        self.headers = ['Name', 'Value', 'URL']
        self.rows = [{'Name': 'Ride <A>', 'Value': 0, 'URL': 'https://example.test/?a=1&b=2'},
                     {'Name': '</script><script>alert(1)</script>\u2028', 'Value': 5, 'URL': ''}]
        self.spec = {'title': 'Operations <img src=x onerror=alert(1)>',
                     'digest': {'enabled': True, 'language': 'en', 'include_source_links': True,
                                'metrics': [{'op': 'sum', 'field': 'Value', 'label': 'Total'}]}}
        self.digest = {'text': 'Summary <script>https://invented.test</script>',
                       'facts': build_facts(self.headers, self.rows, self.spec['digest']),
                       'generated_at': '2026-11-13T09:00:00+00:00'}

    def test_dashboard_escapes_user_markup_and_only_embeds_trusted_scripts(self):
        html = render_html_report(self.spec, self.headers, self.rows, self.digest)
        audit = DocumentAudit()
        audit.feed(html)
        self.assertIn('report-hero', html)
        self.assertIn('report-metrics', html)
        self.assertIn('Search Data', html)
        self.assertIn('report-prev', html)
        self.assertIn('Print / Save PDF', html)
        self.assertIn('&lt;img', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('<td>0</td>', html)
        self.assertFalse(any(tag in {'img', 'iframe', 'object', 'embed'} for tag, _ in audit.elements))
        self.assertFalse(any(key.startswith('on') for _, attrs in audit.elements for key in attrs))
        scripts = audit.script_content
        self.assertEqual(len(scripts), 3)
        nonces = {script['attrs'].get('nonce') for script in scripts}
        self.assertEqual(len(nonces), 1)
        self.assertNotIn(None, nonces)
        self.assertTrue(all('src' not in script['attrs'] for script in scripts))
        data = next(script for script in scripts if script['attrs'].get('type') == 'application/json')
        self.assertNotIn('</script>', data['text'].lower())
        self.assertIsInstance(json.loads(data['text']), list)
        hrefs = [attrs['href'] for tag, attrs in audit.elements if tag == 'a' and 'href' in attrs]
        self.assertIn('https://example.test/?a=1&b=2', hrefs)
        self.assertNotIn('https://invented.test', hrefs)
        self.assertIn("connect-src 'none'", html)

    def test_can_disable_links_and_charts_without_changing_table_values(self):
        self.spec['digest'].update(include_source_links=False, charts=[])
        html = render_html_report(self.spec, self.headers, self.rows, self.digest)
        audit = DocumentAudit()
        audit.feed(html)
        self.assertFalse(any(attrs.get('href', '').startswith('https:') for tag, attrs in audit.elements if tag == 'a'))
        self.assertIn('https://example.test/?a=1&amp;b=2', html)
        self.assertIn('<td>0</td>', html)

    def test_html_file_payload_roundtrips_unicode_and_keeps_a_safe_name(self):
        html = render_html_report(self.spec, self.headers, self.rows, self.digest)
        item = email_html_attachment('../reports/运行报告.html', html)
        self.assertEqual(set(item), {'Name', 'ContentBytes'})
        self.assertEqual(item['Name'], '运行报告.html')
        self.assertEqual(base64.b64decode(item['ContentBytes']).decode('utf-8'), html)
        self.assertLess(len(json.dumps({'attachments': [item]}).encode('utf-8')), 2_000_000)

    def test_size_limits_fail_instead_of_returning_partial_documents(self):
        with patch('kb.report_presentation.MAX_HTML_BYTES', 100):
            with self.assertRaisesMessage(ValueError, '未截断报告'):
                render_html_report(self.spec, self.headers, self.rows, self.digest)
            with self.assertRaises(ValueError):
                email_html_attachment('report.html', 'x' * 101)
        with self.assertRaises(ValueError):
            email_html_attachment('report\n.html', '<html>safe</html>')
        with self.assertRaises(ValueError):
            render_html_report(self.spec, self.headers, self.rows, self.digest, report_url='javascript:alert(1)')

    def test_group_and_metric_details_preserve_full_available_facts(self):
        self.spec['digest']['group_by'] = ['Name']
        self.digest['facts'] = build_facts(self.headers, self.rows, self.spec['digest'])
        html = render_html_report(self.spec, self.headers, self.rows, self.digest)
        self.assertIn('View All Aggregate Figures', html)
        self.assertIn('Name=Ride &lt;A&gt;', html)
        self.assertIn('Total', html)
        self.assertNotIn('<script>alert(1)</script>', html)

