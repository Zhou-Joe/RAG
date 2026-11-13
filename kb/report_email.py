"""Fixed email branding, independent of report data and model-generated text."""
from html import escape
from urllib.parse import urljoin

from django.templatetags.static import static


def email_signature(public_base=''):
    # The wordmark is typeset in Ink Free at 3x resolution. Using an
    # ordinary static image keeps its handwriting in Outlook and other clients
    # without requiring an installed/web font or a new workflow attachment map.
    wordmark = ('<span style="font-family:\'Ink Free\',\'Segoe Print\',cursive;'
                'font-size:24px;font-weight:600;line-height:28px;color:#08775f;">EnGenie</span>')
    if public_base:
        source = urljoin(public_base.rstrip('/')+'/', static('icons/engenie-email-wordmark.png'))
        wordmark = ('<img src="'+escape(source,quote=True)+'" alt="EnGenie" width="84" height="24" '
                    'border="0" style="display:inline-block;width:84px;height:24px;border:0;'
                    'vertical-align:-5px;font-family:\'Ink Free\',\'Segoe Print\',cursive;'
                    'font-size:24px;font-weight:600;color:#08775f;">')
    return ('<table role="presentation" data-engenie-signature="true" border="0" cellpadding="0" cellspacing="0" '
            'width="100%" style="width:100%;margin-top:32px;border-collapse:collapse;'
            'font-family:\'Segoe UI\',Arial,sans-serif;">'
            '<tr><td style="border-top:1px solid #e3e6eb;padding:18px 0 0;'
            'font-family:\'Segoe UI\',Arial,sans-serif;font-size:16px;font-weight:400;'
            'line-height:28px;color:#8d929e;">Powered by&nbsp;'+wordmark+
            '<span style="font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;'
            'font-weight:400;color:#8d929e;">&nbsp;&middot;&nbsp;Intelligent AI Agent built by FOS</span>'
            '</td></tr><tr><td style="padding:5px 0 0;font-family:\'Segoe UI\',Arial,sans-serif;'
            'font-size:16px;font-weight:600;line-height:23px;color:#08775f;">'
            'Engineering the Magic!</td></tr></table>')
