"""Reserve a final model call instead of exhausting the graph on retrieval loops."""
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


class RetrievalBudget(AgentMiddleware):
    """Limit tool rounds per current user turn; history does not consume the budget."""
    def __init__(self, max_rounds=5, selection=None):
        self.max_rounds = max_rounds
        self.selection = selection
        self.repairs = 0

    def prepare(self, request):
        rounds = 0
        seen = set()
        repeated = False
        import json
        for message in request.messages:
            if isinstance(message, HumanMessage):
                rounds = 0
                seen.clear()
                repeated = False
            elif isinstance(message, AIMessage) and message.tool_calls:
                rounds += 1
                for call in message.tool_calls:
                    key = (call['name'], json.dumps(call.get('args', {}), sort_keys=True, ensure_ascii=False))
                    repeated = repeated or key in seen
                    seen.add(key)
        original = request.system_message.content if request.system_message else ''
        if self.selection is not None and (self.selection.pending or (
                (rounds>=self.max_rounds or repeated) and self.selection.state.get('used')
                and not self.selection.state.get('finalized'))):
            def name(tool):
                if isinstance(tool,dict):return tool.get('name') or tool.get('function',{}).get('name')
                return getattr(tool,'name',getattr(tool,'__name__',''))
            tools = [tool for tool in request.tools if name(tool)=='kb_select_evidence']
            if tools:
                return request.override(tools=tools,
                    tool_choice={'type':'function','function':{'name':'kb_select_evidence'}},
                    system_message=SystemMessage(content=str(original)+'\n先确认最终采用的已读证据编号，再回答。'
                        '只允许选择已读编号；不足时选空数组后澄清，不把候选短预览当作事实。'))
        if rounds < self.max_rounds and not repeated:
            return request
        instruction = ('\n本轮工具调用预算已用完。根据会话中已有的原文证据、追踪表查询/统计结果或已核对的图谱事实回答当前问题；'
                       '禁止继续调用工具、补写不存在的数据或把旧答案当事实。'
                       '若当前对象不明确，直接请求用户确认；若资料不足，明确列出缺失项。'
                       '不要声称已完成完整比较，除非各方资料都支持；按当前界面语言给出最终回答。')
        return request.override(tools=[], tool_choice=None,
                                system_message=SystemMessage(content=str(original) + instruction))

    def recovery_request(self, request, response):
        if self.selection is None or self.repairs>=1 or not self.selection.state.get('used') or self.selection.state.get('finalized'):
            return None
        final=next((message for message in reversed(response.result) if isinstance(message,AIMessage)),None)
        if final is None or final.tool_calls or (final.response_metadata or {}).get('finish_reason') in ('length','content_filter'):
            return None
        self.repairs+=1
        def name(tool):
            return (tool.get('name') or tool.get('function',{}).get('name')) if isinstance(tool,dict) else getattr(tool,'name',getattr(tool,'__name__',''))
        allowed={'kb_select_evidence'}
        if not self.selection.loaded:allowed.add('kb_read_chunks')
        tools=[tool for tool in request.tools if name(tool) in allowed]
        if not tools:return None
        instruction=('当前回答尚未通过证据选择，草稿不会发布。仅恢复一次收尾步骤，不重新搜索。'
            '需要事实时先读取对应候选；已有已读图像/原文则选择实际采用的编号。'
            '没有适用依据时选空数组并如实澄清，不能复述未读候选中的事实。'
            '已读编号：'+str(sorted(self.selection.loaded))+'；候选编号：'+str(sorted(self.selection.presented)))
        original=request.system_message.content if request.system_message else ''
        return request.override(tools=tools,tool_choice='required',
            system_message=SystemMessage(content=str(original)+'\n'+instruction))

    def wrap_model_call(self, request, handler):
        prepared=self.prepare(request)
        response=handler(prepared)
        recovery=self.recovery_request(request,response)
        return handler(recovery) if recovery is not None else response

    async def awrap_model_call(self, request, handler):
        prepared=self.prepare(request)
        response=await handler(prepared)
        recovery=self.recovery_request(request,response)
        return await handler(recovery) if recovery is not None else response
