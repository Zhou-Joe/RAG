from __future__ import annotations

"""连接 Django 采集任务、加密凭据与可独立运行的浏览器采集器。"""

from pathlib import Path

from django.conf import settings

from .browser_runtime import BrowserSession, origin
from .connector_tools import declared_urls, redact
from .feed_credentials import credential_profiles, credential_urls, request_headers, secret_values, unpack


def screenshot_path(job_id) -> Path:
    return Path(settings.DATA_DIR)/'connector-browser'/str(job_id)/'preview.png'


def session(resource,*,checkpoint=None,screenshot=None,timezone=None) -> BrowserSession:
    """先在同步上下文读取凭据，浏览器线程内不访问 Django。"""
    url=getattr(resource,'api_url','') or getattr(resource,'url','')
    urls=declared_urls(resource) if hasattr(resource,'prompt') else [url]
    if not getattr(resource,'credential_id',None) and not resource.header_env:
        urls.extend(getattr(resource,'browser_state',{}).get('observed_urls',[]))
    if url and url not in urls:urls.append(url)
    urls.extend(credential_urls(resource))
    credential=getattr(resource,'credential',None)
    login=unpack(credential).get('login',{}) if credential else {}
    headers=request_headers(resource,url) if url else {}
    profiles=[{key:profile[key] for key in ('name','url','headers','login')} for profile in credential_profiles(resource)]
    return BrowserSession(approved_urls=urls,headers=headers,credential_origin=url if credential or headers else None,login=login,
        credential_profiles=profiles,
        allow_private=resource.allow_private,screenshot_path=screenshot,checkpoint=checkpoint,
        timezone=timezone or getattr(resource,'timezone','Asia/Shanghai'))


def collect(spec,resource,*,limit=None,checkpoint=None,screenshot=None) -> dict:
    """重放已验证的网页步骤，输出交给既有映射/发布管线的数据。"""
    browser=session(resource,checkpoint=checkpoint,screenshot=screenshot,timezone=spec.get('timezone','Asia/Shanghai'))
    try:return redact(browser.call('collect',spec=spec,limit=limit),secret_values(resource))
    finally:browser.close()
