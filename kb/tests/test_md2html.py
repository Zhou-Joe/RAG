"""md2html 消毒器安全测试（XSS 白名单，含审查实证的绕过 payload）。"""
from django.test import SimpleTestCase

from kb.md2html import md_to_html


class SanitizerTests(SimpleTestCase):
    def test_onattr_bypass_payloads_are_neutralized(self):
        # 审查实证的两种绕过：斜杠分隔属性 / 引号后紧跟属性（旧正则均漏）
        for payload in (
            '<img/src="x"/onerror="alert(1)">',
            '<img src="x"onerror="alert(1)">',
        ):
            out = md_to_html(payload)
            self.assertNotIn("onerror", out, payload)
            self.assertNotIn("alert", out, payload)

    def test_whitelisted_html_survives(self):
        out = md_to_html('<img src="images/abc.jpg" alt="图">')
        self.assertIn('src="images/abc.jpg"', out)
        out = md_to_html('<table><tr><td rowspan=1 colspan=1>ok</td></tr></table>')
        self.assertIn("ok", out)
        self.assertIn('colspan="1"', out)

    def test_script_and_dangerous_urls_removed(self):
        self.assertNotIn("<script>", md_to_html("<script>alert(1)</script>"))
        out = md_to_html('<img src="javascript:alert(1)">')
        self.assertNotIn("javascript", out)

    def test_markdown_side_links_sanitized(self):
        out = md_to_html("[点我](javascript:alert(1))")
        self.assertNotIn("javascript", out)
        for source in (r'[x](javascript\:alert)', r'![x](javascript\:alert)'):
            self.assertNotIn('javascript', md_to_html(source))

    def test_escaped_quotes_cannot_create_link_or_image_attributes(self):
        from html.parser import HTMLParser

        class Tags(HTMLParser):
            def __init__(self):
                super().__init__()
                self.seen = []

            def handle_starttag(self, tag, attrs):
                if tag in ('a', 'img'):
                    self.seen.append((tag, dict(attrs)))

        for source in (r'[x](https://example.test/\"onmouseover=\"bad)',
                       r'![x](https://example.test/\"onerror=\"bad)'):
            reader = Tags()
            reader.feed(md_to_html(source))
            self.assertEqual(len(reader.seen), 1)
            tag, attrs = reader.seen[0]
            self.assertEqual(set(attrs), {'href'} if tag == 'a' else {'src', 'alt'})
