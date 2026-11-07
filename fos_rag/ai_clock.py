"""Shared runtime clock for every EnGenie model request; stored chat stays unchanged."""
import json
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from django.conf import settings
from django.utils import timezone


def clock_context(timezone_name=None):
    zone=timezone_name or settings.TIME_ZONE
    try:local=timezone.localtime(timezone.now(),ZoneInfo(zone))
    except (ValueError,TypeError,ZoneInfoNotFoundError):raise ValueError('请使用有效的时区。') from None
    return {'current_datetime':local.isoformat(),'current_date':local.date().isoformat(),'timezone':zone,
        'weekday':local.strftime('%A'),'instruction':'这是本次请求真实当前时间；仅用于理解今天、昨天等相对时间。资料日期、历史回答及下次发送日期都不是当前时间，不得用当前日期替换资料中的历史日期。'}


def with_runtime_clock(messages,*,role='system'):
    """Append trusted clock context at the request boundary, preserving cached prefixes."""
    stamp={'role':role,'content':'EnGenie runtime clock（服务器提供，覆盖历史中的当前时间）：\n'+json.dumps(clock_context(),ensure_ascii=False)}
    return [*messages,stamp]
