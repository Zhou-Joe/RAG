from __future__ import annotations

"""有限 API 拉取：校验 DNS、固定连接地址、保留 TLS 主机校验，不跟随重定向。"""

import ipaddress
import os
import re
import socket
import time
from urllib.parse import urlsplit

import httpx

from .feed_protocol import MAX_BYTES,load_batch,validate_url


def validate_headers(mapping):
    if not isinstance(mapping,dict) or len(mapping)>20:raise ValueError('请求头配置必须是最多20项的 JSON 对象。')
    for key,value in mapping.items():
        if (not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',key)
            or key.lower() in {'host','content-length','connection','cookie','transfer-encoding'}
            or not isinstance(value,str) or not re.fullmatch(r'FOS_FEED_[A-Z0-9_]{1,118}',value)):
            raise ValueError('请求头只能引用 FOS_FEED_ 前缀的环境变量，不能设置 Host、Cookie 或连接头。')
    return mapping


def resolve_target(url,allow_private=False):
    validate_url(url);parts=urlsplit(url);host=parts.hostname
    try:addresses=list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host,parts.port or (443 if parts.scheme=='https' else 80),type=socket.SOCK_STREAM)))
    except OSError as exc:raise ValueError('API 主机解析失败。') from exc
    if not addresses:raise ValueError('API 主机没有可用地址。')
    for address in addresses:
        ip=ipaddress.ip_address(address)
        if ip.version==6 and ip.ipv4_mapped:ip=ip.ipv4_mapped
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or (ip.is_reserved and not (allow_private and ip.is_loopback)) or (not ip.is_global and not allow_private):
            raise ValueError('API 指向受限网络地址；内网服务需由全局管理员明确启用。')
    return host,addresses[0]


def fetch_batch(feed):
    if getattr(feed,'adapter_spec',None):
        from .connector_runtime import request_url,build_batch
        spec=feed.adapter_spec
        if spec.get('source_type')=='browser':
            from .connector_browser import collect
            from .feed_protocol import validate_batch
            return validate_batch(build_batch(spec,collect(spec,feed,checkpoint=getattr(feed,'_run_checkpoint',None)))),''
        from .feed_credentials import request_headers
        target=request_url(spec,feed.cursor)
        status,headers,raw=read_url(target,allow_private=feed.allow_private,headers=request_headers(feed,target))
        if status in (401,403):raise ValueError('API 认证失败或权限不足（HTTP '+str(status)+'），请在页面更新凭据；原有数据保留。')
        if status!=200:raise ValueError('原始 API 返回 HTTP '+str(status)+'，本次未更新数据。')
        if 'json' not in headers.get('content-type','').lower():raise ValueError('原始 API 必须返回 JSON。')
        import json
        try:
            data=json.loads(raw)
            if getattr(feed,'credential_id',None):
                from .connector_tools import redact
                from .feed_credentials import secret_values
                data=redact(data,secret_values(feed))
        except (ValueError,UnicodeDecodeError) as exc:raise ValueError('原始 API 返回的 JSON 无效。') from exc
        from .feed_protocol import validate_batch
        return validate_batch(build_batch(spec,data)),''
    host,address=resolve_target(feed.url,feed.allow_private)
    headers={'Accept':'application/json','Accept-Encoding':'identity','User-Agent':'FOS-Knowledge-Feed/1'}
    from .feed_credentials import request_headers
    headers.update(request_headers(feed,feed.url))
    url=httpx.URL(feed.url)
    headers['Host']=url.netloc.decode('ascii')
    pinned=url.copy_with(host=address)
    if feed.cursor:pinned=pinned.copy_merge_params({'cursor':feed.cursor})
    elif feed.etag and getattr(feed,'write_mode','upsert')!='append':headers['If-None-Match']=feed.etag
    started=time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(20,connect=5),follow_redirects=False,trust_env=False) as client:
            with client.stream('GET',pinned,headers=headers,extensions={'sni_hostname':host}) as response:
                if response.status_code==304:return None,feed.etag
                if response.status_code in (401,403):raise ValueError('API 认证失败或权限不足（HTTP '+str(response.status_code)+'），请在页面更新凭据；原有数据保留。')
                if response.status_code!=200:raise ValueError(f'API 返回 HTTP {response.status_code}；未提交本次更新。')
                if 'application/json' not in response.headers.get('content-type','').lower():raise ValueError('API 必须返回 application/json。')
                if response.headers.get('content-encoding','identity').lower() not in ('','identity'):raise ValueError('API 请返回未压缩 JSON，以便按字节限制响应大小。')
                data=bytearray()
                for chunk in response.iter_raw():
                    if time.monotonic()-started>30:raise ValueError('API 响应超过30秒，已停止本次拉取。')
                    data.extend(chunk)
                    if len(data)>MAX_BYTES:raise ValueError('API 返回数据超过500MB上限。')
                payload=load_batch(bytes(data))
                if getattr(feed,'credential_id',None):
                    from .connector_tools import redact
                    from .feed_credentials import secret_values
                    from .feed_protocol import validate_batch
                    payload=validate_batch(redact(payload,secret_values(feed)))
                etag=response.headers.get('etag','')[:500] if not feed.cursor and not payload.get('cursor') else ''
                return payload,etag
    except httpx.HTTPError as exc:
        raise ValueError('API 连接失败或超时（'+type(exc).__name__+'），原有数据保留。') from exc


def environment_headers(mapping):
    result={}
    for name,env in validate_headers(mapping).items():
        value=os.environ.get(env)
        if not value or '\r' in value or '\n' in value:raise ValueError('未配置请求头环境变量：'+env)
        result[name]=value
    return result


def read_url(url,*,allow_private=False,headers=None,method='GET',json_body=None,limit=MAX_BYTES):
    """供连接器工具使用的有限请求，复用地址校验与固定 DNS 连接。"""
    host,address=resolve_target(url,allow_private);original=httpx.URL(url)
    values={'Accept-Encoding':'identity','User-Agent':'FOS-Connector-Agent/1',**(headers or {}),'Host':original.netloc.decode('ascii')}
    start=time.monotonic();data=bytearray()
    try:
        with httpx.Client(timeout=httpx.Timeout(20,connect=5),follow_redirects=False,trust_env=False) as client:
            with client.stream(method,original.copy_with(host=address),headers=values,json=json_body,extensions={'sni_hostname':host}) as response:
                if response.headers.get('content-encoding','identity').lower() not in ('','identity'):raise ValueError('服务返回压缩内容，无法进行有界采样。')
                for chunk in response.iter_raw():
                    if time.monotonic()-start>30:raise ValueError('工具请求超过30秒。')
                    data.extend(chunk)
                    if len(data)>limit:raise ValueError('工具响应超过大小限制。')
                return response.status_code,dict(response.headers),bytes(data)
    except httpx.HTTPError as exc:raise ValueError('工具网络请求失败：'+type(exc).__name__) from exc
