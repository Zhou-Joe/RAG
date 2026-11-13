from html.parser import HTMLParser
from pathlib import Path
import struct

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from kb.feed_reports import payload
from kb.report_email import email_signature


class SignatureContent(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.text = []
        self.images = []
        self.feed(html)

    def handle_data(self, data):
        self.text.append(data)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'img':
            self.images.append(attrs)
            self.text.append(attrs.get('alt', ''))


@override_settings(FEED_REPORT_PUBLIC_BASE='https://reports.example.test')
class ReportEmailSignatureTests(SimpleTestCase):
    def test_signature_keeps_the_exact_english_text_and_reference_typography(self):
        html = email_signature('https://reports.example.test/app')
        parsed = SignatureContent(html)
        self.assertEqual(''.join(parsed.text).replace('\xa0', ' '),
                         'Powered by EnGenie · Intelligent AI Agent built by FOSEngineering the Magic!')
        self.assertIn('border-top:1px solid #e3e6eb', html)
        self.assertIn('color:#8d929e', html)
        self.assertIn('color:#08775f', html)
        self.assertIn('Ink Free', html)
        self.assertIn('Segoe UI', html)
        self.assertEqual(len(parsed.images), 1)
        self.assertEqual(parsed.images[0]['src'], 'https://reports.example.test/static/icons/engenie-email-wordmark.png')
        self.assertEqual(parsed.images[0]['width'], '84')
        self.assertEqual(parsed.images[0]['height'], '24')
        self.assertNotIn('<script', html)

    def test_signature_has_live_text_fallback_without_a_public_host(self):
        html = email_signature()
        self.assertIn('EnGenie</span>', html)
        self.assertNotIn('<img', html)
        self.assertIn('Engineering the Magic!', html)
        with override_settings(STATIC_URL='/assets/'):
            self.assertIn('https://reports.example.test/assets/icons/engenie-email-wordmark.png',
                          email_signature('https://reports.example.test'))

    def test_retina_wordmark_has_no_embedded_font_dependency(self):
        content = (Path(settings.BASE_DIR)/'static/icons/engenie-email-wordmark.png').read_bytes()
        self.assertEqual(content[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', content[16:24]), (252, 72))
        self.assertLess(len(content), 15000)
        self.assertIn(b'Ink Free', content)

    def test_scheduled_and_condition_email_bodies_have_one_fixed_signature_after_content(self):
        config = {'kind': 'email', 'recipient': 'owner@example.test'}
        for conditional in (False, True):
            spec = {'title': 'Equipment alerts'}
            if conditional:spec['trigger'] = {'condition': {'field': 'Value', 'op': 'gte', 'value': 3}}
            result = payload(config, spec, ['Name', 'Value'], [{'Name': '<Equipment>', 'Value': 3}],
                             'https://reports.example.test/download')
            body = result['b']
            self.assertEqual(body.count('data-engenie-signature='), 1)
            self.assertTrue(body.endswith('Engineering the Magic!</td></tr></table>'))
            self.assertLess(body.index('Download complete Excel report'), body.index('data-engenie-signature='))
            self.assertEqual(result['s'], 'Equipment alerts')
            self.assertIn('&lt;Equipment&gt;', body)

    def test_digest_link_and_html_attachment_emails_include_the_signature(self):
        config = {'kind': 'email', 'recipient': 'owner@example.test', 'html_attachments_supported': True}
        for format in ('body', 'html_link', 'html_attachment'):
            spec = {'title': 'Daily report', 'digest': {'enabled': True, 'language': 'en', 'email_format': format}}
            result = payload(config, spec, ['Name'], [{'Name': 'A'}], 'https://reports.example.test/download',
                             digest={'text': 'Requested summary', 'facts': {}},
                             html_url='https://reports.example.test/report.html', html_content='<!doctype html><p>Report</p>')
            self.assertEqual(result['b'].count('data-engenie-signature='), 1)
            self.assertIn('Intelligent AI Agent built by FOS', result['b'])
            if format == 'html_attachment':
                self.assertEqual(len(result['attachments']), 1)
                self.assertEqual(result['attachments'][0]['Name'], 'EnGenie-report.html')
            else:
                self.assertNotIn('attachments', result)

    def test_teams_cards_do_not_receive_an_email_signature(self):
        result = payload({'kind': 'teams'}, {'title': 'Alert'}, ['Value'], [{'Value': 3}],
                         'https://reports.example.test/download')
        self.assertNotIn('Engineering the Magic!', str(result))
        self.assertNotIn('engenie-email-wordmark', str(result))
