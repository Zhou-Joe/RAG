"""Keep user-supplied access inputs usable without placing secrets in model text."""
from __future__ import annotations

import json
import re
import uuid

from .feed_credentials import api_origin

SECRET_REF = re.compile(r'\[SOURCE_SECRET:[0-9a-f]{32}\]')
_LABEL = (r'username|user[_ -]?name|account|password|passwd|pwd|'
          r'api[_ -]?key|access[_ -]?token|api[_ -]?token|token|secret|账号|帐号|账户|用户名|密码|口令|密钥|令牌')
_PAIRS = re.compile(
    r'(?<![\w?&])(?:["\']?)(?P<label>' + _LABEL + r')(?:["\']?)\s*'
    r'(?:[:=：]|\bis\b|为|是)\s*'
    r'(?P<value>\[SOURCE_SECRET:[0-9a-f]{32}\]|\[(?:REDACTED|Access credentials provided|已提供访问凭据)\]|"(?:\\.|[^"\\\r\n])*"|\'(?:\\.|[^\'\\\r\n])*\'|[^\s,，;；\}\]<>]+)', re.I,
)
_BEARER = re.compile(r'\bBearer[ \t]+(?P<value>[A-Za-z0-9._~+/=-]+)', re.I)
_URL = re.compile(r'https?://[^\s<>"\]\)）]+')


def secret_field(field):
    """Recognize credential fields using their saved key/type and ordinary labels."""
    key = str(field.get('key', '')).lower()
    label = str(field.get('label', '')).strip()
    if (field.get('type') == 'password' or field.get('secret') is True
            or re.search(r'(?:^|_)(?:username|user_name|account|password|passwd|pwd|api_key|apikey|access_token|api_token|token|secret)$', key)
            or re.fullmatch(_LABEL + r'[：:]?', label, re.I)):
        return True
    return False


def protect_inputs(text, *, fields=(), answers=None, urls=()):
    """Replace explicit credential input with opaque references before redaction."""
    allowed = set()
    for url in [*urls, *_URL.findall(text)]:
        if not url:
            continue
        try:
            allowed.add(api_origin(url.rstrip('.,，。；;')))
        except ValueError:
            continue
    inputs = {}
    references = {}

    def reference(value, label):
        if SECRET_REF.fullmatch(value):
            return value
        if value.strip().upper() == '[REDACTED]':
            raise ValueError('请填写实际凭据，不要填写脱敏占位符。')
        if not value or len(value) > 8000:
            raise ValueError('访问凭据为空或过长，请重新填写。')
        if value not in references:
            key = '[SOURCE_SECRET:' + uuid.uuid4().hex + ']'
            inputs[key] = {'value': value, 'origins': sorted(allowed), 'field': label}
            references[value] = key
        return references[value]

    saved_fields = {field['key']: field for field in fields}
    supplied = []
    for answer in answers or []:
        field = saved_fields.get(answer.get('key'))
        if not field or not secret_field(field):
            continue
        value = answer.get('value', '')
        if not value:
            continue
        ref = reference(value, field['key'])
        # The browser submits secret values separately rather than echoing them
        # into the visible chat composer. Older clients may still echo them.
        username = bool(re.search(r'username|user_name|account|账号|帐号|账户|用户名',field['key']+' '+field['label'],re.I))
        protected = SECRET_REF.pattern + ('|' + _URL.pattern if username else '')
        parts = re.split('(' + protected + ')', text)
        for index in range(0, len(parts), 2):
            parts[index] = (parts[index].replace(value, ref) if len(value)>2 else
                re.sub(r'(?<!\w)' + re.escape(value) + r'(?!\w)', lambda _match:ref, parts[index]))
        text = ''.join(parts)
        supplied.append(field['label'] + ' (' + field['key'] + '): ' + ref)

    replacements = []
    for match in [*_PAIRS.finditer(text), *_BEARER.finditer(text)]:
        value = match['value']
        quote = value[0] if value and value[0] in ('"', "'") else ''
        actual = value[1:-1] if quote else value
        if quote == '"':
            try:actual = json.loads(value)
            except ValueError:pass
        elif quote:
            actual = re.sub(r"\\(['\\])", r'\1', actual)
        if SECRET_REF.fullmatch(actual):
            continue
        if actual in ('[Access credentials provided]', '[已提供访问凭据]'):
            continue
        label = match.groupdict().get('label') or ''
        if re.fullmatch(r'username|user[_ -]?name|account|账号|帐号|账户|用户名',label,re.I) and not re.search(
                r'password|passwd|pwd|token|secret|api[_ -]?key|login|credential|authorization|密码|口令|密钥|令牌|登录|认证',text,re.I):
            continue
        if actual.lower() == 'bearer' and _BEARER.match(text, match.start('value')):
            continue
        ref = reference(actual, match.groupdict().get('label') or 'bearer_token')
        start, end = match.span('value')
        replacements.append((start, end, quote + ref + quote))
    selected = []
    for start, end, ref in sorted(replacements, key=lambda item: item[1] - item[0], reverse=True):
        if not any(start < other_end and end > other_start for other_start, other_end, _ in selected):
            selected.append((start, end, ref))
    for start, end, ref in sorted(selected, reverse=True):
        text = text[:start] + ref + text[end:]
    if supplied:
        text += '\n\nUser-provided source access inputs:\n' + '\n'.join(supplied)
    return text, inputs


def resolve_authentication(job, url, authentication):
    """Resolve only references belonging to this conversation and target service."""
    from .connector_storage import input_secrets

    target = api_origin(url)
    cache = None
    used = []

    def resolve(value):
        nonlocal cache
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, str):
            return value
        bearer = re.fullmatch(r'Bearer\s+(' + SECRET_REF.pattern + ')', value, re.I)
        reference = bearer[1] if bearer else value
        if not SECRET_REF.fullmatch(reference):return value
        if cache is None:
            cache = input_secrets(job,references=set(SECRET_REF.findall(json.dumps(authentication))))
        entry = cache.get(reference)
        if not entry or entry.get('origins') and target not in entry['origins']:
            raise ValueError('这项凭据引用不属于当前对话或接收服务，请重新提供对应服务的访问凭据。')
        resolved = ('Bearer ' if bearer else '') + entry['value']
        used.extend([entry['value'], resolved])
        return resolved

    if authentication.get('type') == 'multiple':
        profiles = []
        for row in authentication.get('profiles', []):
            auth, values = resolve_authentication(job, row['url'], row['authentication'])
            profiles.append({**row, 'authentication': auth})
            used.extend(values)
        return {**authentication, 'profiles': profiles}, used
    return resolve(authentication), used
