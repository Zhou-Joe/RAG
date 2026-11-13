from __future__ import annotations

"""采集任务的结构化工作记忆、上下文压缩与真实用量统计。"""

import hashlib
import json


from fos_rag.ai_clock import clock_context


def conversation_context(job):
    """保留多轮修改及问题/回答，最近的非空方案不被一轮追问清空。"""
    history=[];candidate=job.parent if job.parent_id else None;seen=set();spec={};preview={};report={};report_approved=False
    while candidate and candidate.pk not in seen and len(history)<100:
        seen.add(candidate.pk)
        if not report and candidate.spec.get('kind')=='report':
            report=candidate.spec;report_approved=candidate.status=='enabled'
        if not spec and candidate.spec and candidate.spec.get('kind')!='report':spec=candidate.spec;preview=candidate.preview
        history.append({'user':candidate.feedback or candidate.prompt,'assistant_questions':candidate.questions,'assistant_answer':candidate.preview.get('answer','')})
        candidate=candidate.parent
    from .feed_reports import delivery_context,source_feed
    from .feed_names import current_name,named_spec
    feed=source_feed(job,required=False)
    if feed:
        if not report or report_approved:
            from .models import FeedReportPlan
            plan=FeedReportPlan.objects.filter(feed=feed).first()
            if plan and plan.spec.get('kind')=='report':report=plan.spec
        name=current_name(feed);spec=named_spec(spec,name);report=named_spec(report,name)
    return {'conversation':list(reversed(history)),'previous_connector':spec,'previous_report':report,'previous_examples':preview,'review_feedback':job.feedback,'report_delivery':delivery_context(job)}


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()[:20]


def normalize_arguments(arguments):
    """兼容本地模型将对象参数二次JSON编码的常见格式，不执行任何代码。"""
    if not isinstance(arguments,dict):return arguments
    result=dict(arguments)
    for key in ('spec','report','digest','query','fields','computed','record'):
        value=result.get(key)
        if isinstance(value,str) and value.lstrip().startswith('{'):
            try:parsed=json.loads(value)
            except (ValueError,TypeError):continue
            if isinstance(parsed,dict):result[key]=parsed
    return result


def message_size(message) -> int:
    content=message.content
    if isinstance(content,list):
        count=sum(1600 if isinstance(block,dict) and block.get('type')=='image_url' else len(str(block)) for block in content)
    else:count=len(str(content))
    return count+len(str(getattr(message,'tool_calls',[])))


class WorkingMemory:
    """记住真实页面/样例及失败定位，避免压缩上下文后重新猜测。"""
    def __init__(self):
        self.pages={};self.failures=[];self.samples=[];self.seen=set();self.stalled_turns=0;self.advanced=False

    def begin_turn(self):self.advanced=False

    def observe(self,name,args,result):
        if not isinstance(result,dict):return
        if result.get('url') and result.get('revision'):
            page={key:result.get(key) for key in ('url','title','revision','root_selector')}
            page['structures']=result.get('structures',[])[:8]
            page['elements']=[{k:item.get(k) for k in ('ref','text','selector','role')} for item in result.get('elements',[])[:12]]
            page['text']=result.get('text','')[:900]
            self.pages[page['url']]=page
            key='page:'+str(result['revision'])
            if key not in self.seen:self.advanced=True;self.seen.add(key)
        rows=result.get('records') or result.get('preview')
        if isinstance(rows,list) and rows:
            useful=[{key:value for key,value in row.items() if value not in (None,'',[])} for row in rows[:3] if isinstance(row,dict)]
            if any(useful):
                key='data:'+fingerprint([sorted(str(value) for value in row.values()) for row in useful])
                if key not in self.seen:
                    self.advanced=True;self.seen.add(key);self.samples=(self.samples+[{'tool':name,'rows':useful}])[-3:]
        if result.get('error') or result.get('warning') or result.get('ok') is False:
            self.failures=(self.failures+[{'tool':name,'selector':args.get('selector') or args.get('rows'),
                'reason':str(result.get('error') or result.get('warning') or result.get('missing_fields'))[:250]}])[-10:]
        if name=='probe_api' and result.get('sample'):
            key='api:'+fingerprint(result['sample'])
            if key not in self.seen:self.advanced=True;self.seen.add(key)

    def end_turn(self):
        self.stalled_turns=0 if self.advanced else self.stalled_turns+1

    def summary(self):
        return {'known_pages':list(self.pages.values())[-4:],'real_samples':self.samples,'failed_selectors_do_not_repeat':self.failures,
                'turns_without_new_data':self.stalled_turns}


def compact_context(messages,memory:WorkingMemory,limit=64000) -> tuple[list,bool]:
    """保持追加式前缀；只有越界时才一次腾出足够空间，并携带工作记忆。"""
    if sum(message_size(message) for message in messages)<=limit:return messages,False
    from langchain_core.messages import HumanMessage,ToolMessage
    groups=[];index=2
    while index<len(messages):
        end=index+1
        while end<len(messages) and isinstance(messages[end],ToolMessage):end+=1
        groups.append(messages[index:end]);index=end
    summary=HumanMessage(content='已完成步骤的真实工作记忆（不是新任务，不要重新探索已知页面）：\n'+json.dumps(memory.summary(),ensure_ascii=False,sort_keys=True))
    kept=[];total=sum(message_size(message) for message in [*messages[:2],summary])
    for group in reversed(groups):
        size=sum(message_size(message) for message in group)
        if total+size>max(28000,limit//2):break
        kept.insert(0,group);total+=size
    return [*messages[:2],summary,*[message for group in kept for message in group]],True


def usage_stats(response) -> dict:
    """仅解析服务返回的 token/cache 数字，不将缺失缓存字段伪装为0%命中。"""
    usage=getattr(response,'usage_metadata',None) or {}
    metadata=getattr(response,'response_metadata',None) or {}
    raw=metadata.get('token_usage') or metadata.get('usage') or {}
    details=usage.get('input_token_details') or {}
    raw_details=raw.get('prompt_tokens_details') or raw.get('input_tokens_details') or {}
    cached=details.get('cache_read',raw_details.get('cached_tokens'))
    return {'input_tokens':int(usage.get('input_tokens') or raw.get('prompt_tokens') or raw.get('input_tokens') or 0),
            'output_tokens':int(usage.get('output_tokens') or raw.get('completion_tokens') or raw.get('output_tokens') or 0),
            'cached_tokens':int(cached or 0),'cache_reported':cached is not None}
