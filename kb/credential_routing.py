"""Origin-scoped credential routing shared by managed and exported collectors."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

MAX_CREDENTIAL_PROFILES = 8
PROFILE_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,39}')
CREDENTIAL_REF = re.compile(r'(?:(?P<profile>[A-Za-z][A-Za-z0-9_-]{0,39})\.)?(?P<field>username|password)')


def credential_origin(url):
    if not isinstance(url, str) or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError('凭据接收地址无效。')
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('凭据接收地址须为不含账号密码的 HTTP(S) 地址。')
    return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == 'https' else 80)


def origin_url(url):
    scheme, host, port = credential_origin(url)
    host = '[' + host + ']' if ':' in host else host
    return scheme + '://' + host + (':' + str(port) if port != (443 if scheme == 'https' else 80) else '')


def scoped_headers(profiles, url):
    target = credential_origin(url)
    headers, names = {}, {}
    for profile in profiles:
        if credential_origin(profile['url']) != target:
            continue
        for name, value in profile.get('headers', {}).items():
            key = name.lower()
            if key in names and headers[names[key]] != value:
                raise ValueError('同一服务的认证请求头冲突；同名请求头只能配置一个值。')
            if key not in names:
                names[key] = name
                headers[name] = value
    return headers


def login_value(profiles, url, reference):
    match = CREDENTIAL_REF.fullmatch(reference or '')
    if match is None:
        raise ValueError('登录凭据使用 username/password 或 名称.username/名称.password 引用。')
    target = credential_origin(url)
    matches = [profile for profile in profiles
               if credential_origin(profile['url']) == target and profile.get('login')
               and (not match['profile'] or profile['name'] == match['profile'])]
    if not matches:
        raise ValueError('当前页面没有匹配的已保存登录凭据，不能使用其他服务的账号密码。')
    if len(matches) != 1:
        raise ValueError('当前服务有多组登录账号，请使用 名称.username 或 名称.password 指定。')
    value = matches[0]['login'].get(match['field'])
    if not isinstance(value, str) or not value:
        raise ValueError('请保存完整的网页登录账号与密码。')
    return value


def environment_profiles(config):
    """Resolve exported non-secret references; never print credential values."""
    if not isinstance(config, list) or not 1 <= len(config) <= MAX_CREDENTIAL_PROFILES:
        raise ValueError('凭据配置须为1至8组。')
    result, seen = [], set()
    for profile in config:
        if (not isinstance(profile, dict) or not isinstance(profile.get('name'), str)
                or not PROFILE_NAME.fullmatch(profile['name'])
                or profile['name'] in seen):
            raise ValueError('凭据名称须唯一，使用字母、数字、下划线或连字符。')
        seen.add(profile['name'])
        url = origin_url(profile.get('url'))
        def resolve(mapping, *, login=False):
            if not isinstance(mapping, dict):
                raise ValueError('凭据环境变量映射无效。')
            if len(mapping)>20:
                raise ValueError('凭据请求头过多。')
            values = {}
            for key, variable in mapping.items():
                if (not isinstance(key,str) or (key not in ('username','password') if login else
                        not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',key) or key.lower() in
                        {'host','cookie','content-length','connection','transfer-encoding'})):
                    raise ValueError('凭据字段名称无效。')
                if not isinstance(variable, str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', variable):
                    raise ValueError('凭据环境变量名称无效。')
                value = os.environ.get(variable, '')
                if not value:
                    raise ValueError('Missing credential environment variable: ' + variable)
                if any(ord(char) < 32 or ord(char) == 127 for char in value):
                    raise ValueError('凭据不能包含控制字符。')
                values[key] = value
            return values
        result.append({'name': profile['name'], 'url': url,
            'headers': resolve(profile.get('header_env', {})), 'login': resolve(profile.get('login_env', {}),login=True)})
    for profile in result:
        scoped_headers(result, profile['url'])
    return result
