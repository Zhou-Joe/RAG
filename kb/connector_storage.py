from __future__ import annotations

"""保存可恢复的代理消息和工作记忆；浏览器进程只留在临时会话中。"""

import json

from cryptography.fernet import InvalidToken
from langchain_core.messages import messages_from_dict,messages_to_dict

from .connector_context import WorkingMemory
from .feed_credentials import _cipher
from .models import ConnectorDraft


def _payload(job):
    if not job.context_snapshot:return {}
    try:
        data=json.loads(_cipher().decrypt(job.context_snapshot.encode()))
        if data.get('version')!=1 or data.get('job')!=str(job.pk) or data.get('owner')!=job.user_id:
            raise ValueError('上下文归属不匹配。')
        return data
    except (InvalidToken,TypeError,json.JSONDecodeError) as exc:
        raise ValueError('已保存的 AI 上下文无法读取，请检查部署密钥；原定时任务仍可运行。') from exc


def save_input_secrets(job,values):
    if not values:return
    payload=_payload(job) or {'version':1,'job':str(job.pk),'owner':job.user_id}
    payload['input_secrets']={**payload.get('input_secrets',{}),**values}
    job.context_snapshot=_cipher(create=not ConnectorDraft.objects.exclude(context_snapshot='').exists()).encrypt(
        json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()).decode()
    ConnectorDraft.objects.filter(pk=job.pk,user_id=job.user_id).update(context_snapshot=job.context_snapshot)


def input_secrets(job,*,references=None):
    values={};node=job;seen=set()
    while node and node.pk not in seen and len(seen)<100:
        if node.user_id!=job.user_id:raise ValueError('对话归属不匹配。')
        seen.add(node.pk)
        values.update(_payload(node).get('input_secrets',{}))
        if references is not None and references<=values.keys():break
        node=node.parent if node.parent_id else None
    return values


def save(job,state):
    memory=state['memory']
    payload={'version':1,'job':str(job.pk),'owner':job.user_id,
        'messages':messages_to_dict(state['messages']),
        'memory':{'pages':memory.pages,'failures':memory.failures,'samples':memory.samples,'seen':list(memory.seen)},
        **{key:state.get(key) for key in ('sample','browser_sample','browser_digest','sources')},
        'input_secrets':_payload(job).get('input_secrets',{})}
    encrypted=_cipher(create=not ConnectorDraft.objects.exclude(context_snapshot='').exists()).encrypt(json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()).decode()
    ConnectorDraft.objects.filter(pk=job.pk,user_id=job.user_id).exclude(status='context_cleared').update(context_snapshot=encrypted)


def load(job_id,user_id):
    job=ConnectorDraft.objects.filter(pk=job_id,user_id=user_id).only('id','user_id','context_snapshot').first()
    if not job or not job.context_snapshot:return None
    try:
        data=_payload(job)
        if 'messages' not in data or 'memory' not in data:return None
        memory=WorkingMemory();saved=data['memory']
        memory.pages=saved['pages'];memory.failures=saved['failures'];memory.samples=saved['samples'];memory.seen=set(saved['seen'])
        return {'messages':messages_from_dict(data['messages']),'memory':memory,'sources':data.get('sources') or [],
            'sample':None,'browser_sample':None,'browser_digest':'','restored_from_storage':True}
    except (InvalidToken,KeyError,TypeError,json.JSONDecodeError) as exc:
        raise ValueError('已保存的 AI 上下文无法读取，请检查部署密钥；原定时任务仍可运行。') from exc


def retain_chain(job):
    """批准任务后保留整个对话链，页面离开仅释放临时浏览器。"""
    ids=[];current=job;seen=set()
    while current and current.pk not in seen and len(ids)<100:
        if current.user_id!=job.user_id:raise ValueError('对话归属不匹配。')
        seen.add(current.pk);ids.append(current.pk);current=current.parent if current.parent_id else None
    ConnectorDraft.objects.filter(pk__in=ids,user_id=job.user_id).update(retain_context=True)
