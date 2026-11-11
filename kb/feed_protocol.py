from __future__ import annotations

"""FOS Knowledge Feed v1：显式增量写入、删除与游标，无隐式全量覆盖。"""

import json
import re
from urllib.parse import urlsplit

from django.utils.dateparse import parse_datetime

SCHEMA='fos-rag-feed/v1'
MAX_BYTES=500_000_000
MAX_ITEMS=50_000
MAX_CONTENT=100_000
MAX_FIELDS_CHARS=20_000
MAX_METADATA_CHARS=20_000


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def load_batch(raw: bytes) -> dict:
    if len(raw)>MAX_BYTES:raise ValueError('单批数据不能超过500MB。')
    try:payload=json.loads(raw.decode('utf-8'),parse_constant=lambda value: (_ for _ in ()).throw(ValueError('非有限 JSON 数值')))
    except (ValueError,UnicodeDecodeError,RecursionError) as exc:raise ValueError('请提交 UTF-8 JSON 数据。') from exc
    return validate_batch(payload)


def validate_url(value,required=True,allow_fragment=False):
    if not value and not required:return ''
    if not isinstance(value,str) or len(value)>2000 or any(ord(ch)<32 for ch in value):raise ValueError('URL 格式无效。')
    parts=urlsplit(value)
    try:port=parts.port
    except ValueError as exc:raise ValueError('URL 端口无效。') from exc
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or (parts.fragment and not allow_fragment):
        raise ValueError('仅支持不含用户名、密码或片段的 HTTP/HTTPS URL。')
    if port is not None and not 1<=port<=65535:raise ValueError('URL 端口无效。')
    return value


def validate_batch(payload) -> dict:
    if not isinstance(payload,dict) or payload.get('schema')!=SCHEMA:raise ValueError('数据必须使用 schema="fos-rag-feed/v1"。')
    if not payload.keys()<={'schema','batch_id','items','deleted','cursor'}:raise ValueError('数据包含不支持的字段，不能使用隐式 snapshot 删除。')
    items=payload.get('items');deleted=payload.get('deleted',[])
    if not isinstance(items,list) or not isinstance(deleted,list) or len(items)+len(deleted)>MAX_ITEMS:raise ValueError('每批最多50000项写入和删除；items 必须是数组。')
    batch_id=payload.get('batch_id','')
    if not isinstance(batch_id,str) or (batch_id and not re.fullmatch(r'[A-Za-z0-9._:-]{1,160}',batch_id)):raise ValueError('batch_id 请使用1至160位字母、数字、点、冒号、下划线或连字符。')
    if 'cursor' in payload and (not isinstance(payload['cursor'],str) or len(payload['cursor'])>2048):raise ValueError('cursor 必须是最多2048字符的字符串。')
    ids=set()
    def identity(value):
        if not isinstance(value,str) or not value.strip() or len(value)>200 or any(ord(ch)<32 for ch in value):raise ValueError('每条数据必须有不超过200字符的稳定字符串 id。')
        if value in ids:raise ValueError('同一批数据不能重复写入或同时删除同一 id。')
        ids.add(value)
    for item in items:
        if not isinstance(item,dict) or not item.keys()<={'id','title','content','format','source_url','updated_at','metadata','fields'}:raise ValueError('item 字段不符合接入标准。')
        identity(item.get('id'))
        if not isinstance(item.get('title'),str) or not item['title'].strip() or len(item['title'])>200 or any(ord(ch)<32 for ch in item['title']):raise ValueError('title 必须是1至200字符，不能包含控制字符。')
        if not isinstance(item.get('content'),str) or not item['content'].strip() or len(item['content'])>MAX_CONTENT:raise ValueError('content 必须是非空正文，最多100000字符。')
        if item.get('format','markdown') not in ('markdown','text'):raise ValueError('format 仅支持 markdown 或 text。')
        validate_url(item.get('source_url',''),required=False,allow_fragment=True)
        if not isinstance(item.get('metadata',{}),dict) or len(canonical(item.get('metadata',{})))>MAX_METADATA_CHARS:raise ValueError(f'metadata 必须是最多{MAX_METADATA_CHARS}字符的 JSON 对象。')
        fields=item.get('fields',{})
        if not isinstance(fields,dict) or len(canonical(fields))>MAX_FIELDS_CHARS or any(not isinstance(key,str) or len(key)>200 or any(ord(ch)<32 for ch in key) or not isinstance(value,(str,int,float,bool)) for key,value in fields.items()):raise ValueError(f'fields 必须是标量值、最多{MAX_FIELDS_CHARS}字符的 JSON 对象，供追踪表展示。')
        if 'updated_at' in item:
            try:stamp=parse_datetime(item['updated_at']) if isinstance(item['updated_at'],str) else None
            except ValueError:stamp=None
            if stamp is None or stamp.tzinfo is None:raise ValueError('updated_at 必须是包含时区的 ISO 8601 时间。')
    for value in deleted:identity(value)
    if len(canonical(payload).encode('utf-8'))>MAX_BYTES:raise ValueError('单批数据不能超过500MB。')
    return payload
