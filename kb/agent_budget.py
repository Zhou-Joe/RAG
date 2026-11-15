"""Reserve a final model call instead of exhausting the graph on retrieval loops."""
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

DEFAULT_MAX_ROUNDS = 12
PRESENTATION_RESERVE_ROUNDS = 3
MAX_IDENTICAL_ROUNDS = 3
PRESENTATION_TOOLS = frozenset({
    'tracker_visualize', 'tracker_analyze', 'tracker_show_value', 'kb_show_value',
    'write_analysis', 'check_analysis', 'request_verified_export',
})
GRAPH_RECURSION_LIMIT = DEFAULT_MAX_ROUNDS * 2 + 12


def _tool_name(tool):
    if isinstance(tool, dict):
        return tool.get('name') or tool.get('function', {}).get('name')
    return getattr(tool, 'name', getattr(tool, '__name__', ''))


class RetrievalBudget(AgentMiddleware):
    """Reserve presentation and selection within the current turn's tool allowance."""
    def __init__(self, max_rounds=DEFAULT_MAX_ROUNDS, selection=None):
        self.max_rounds = max_rounds
        self.selection = selection
        self.repairs = 0

    def prepare(self, request):
        rounds = 0
        last_signature = None
        identical_rounds = 0
        import json
        for message in request.messages:
            if isinstance(message, HumanMessage):
                rounds = 0
                last_signature = None
                identical_rounds = 0
            elif isinstance(message, AIMessage) and message.tool_calls:
                rounds += 1
                signature = tuple(sorted((call['name'],json.dumps(call.get('args',{}),sort_keys=True,ensure_ascii=False))
                    for call in message.tool_calls))
                identical_rounds = identical_rounds + 1 if signature == last_signature else 1
                last_signature = signature
        repeated = identical_rounds >= MAX_IDENTICAL_ROUNDS
        repeated_names = {name for name, _ in last_signature} if repeated else set()
        remaining = self.max_rounds - rounds
        has_presentation = any(_tool_name(tool) in PRESENTATION_TOOLS for tool in request.tools)
        needs_selection = self.selection is not None and (self.selection.pending or
            self.selection.state.get('used') and not self.selection.state.get('finalized'))
        reserve = PRESENTATION_RESERVE_ROUNDS if has_presentation else int(bool(needs_selection))
        retrieval_finished = remaining <= reserve or bool(repeated_names - PRESENTATION_TOOLS)
        presentation_finished = bool(repeated_names & PRESENTATION_TOOLS)
        original = request.system_message.content if request.system_message else ''
        if remaining > 0 and needs_selection and retrieval_finished:
            tools = [tool for tool in request.tools if _tool_name(tool)=='kb_select_evidence']
            if tools:
                return request.override(tools=tools,
                    tool_choice={'type':'function','function':{'name':'kb_select_evidence'}},
                    system_message=SystemMessage(content=str(original)+'\n先确认最终采用的已读证据编号，再回答。'
                        '只允许选择已读编号；不足时选空数组后澄清，不把候选短预览当作事实。'))
        tools = [tool for tool in request.tools if remaining > 0
                 if not (presentation_finished if _tool_name(tool) in PRESENTATION_TOOLS else retrieval_finished)]
        if tools:
            instructions = []
            if retrieval_finished:
                instructions.append(f'本轮还可调用{remaining}轮工具，已预留给图表、值卡片、分析及导出，不再搜索或逐页取数；'
                    '按当前问题所需继续调用可用的呈现工具，完成后再回答。tracker_analyze需生成图表或文件，不再请求options/preview。'
                    '不要因检索额度耗尽而声称不能画图或让用户下一轮再要求呈现。')
            if presentation_finished:
                instructions.append('呈现请求连续重复且无进展；只继续必要的检索和证据确认，基于实际已生成的图表与原文回答。')
            if self.selection is not None and self.selection.pending:
                instructions.append('已读证据尚未完成最终选择。可以继续使用可用工具检索、分页查询或读取其他有权访问的知识库；'
                    '读取一个知识库不会锁定本轮范围。最终回答前用kb_select_evidence统一选择实际采用的已读编号。')
            if not instructions:
                return request
            return request.override(tools=tools, tool_choice=None,
                system_message=SystemMessage(content=str(original)+'\n'+'\n'.join(instructions)))
        reason = '本轮工具调用预算已用完。' if remaining <= 0 else '连续重复请求已停止。'
        instruction = ('\n'+reason+'根据会话中已有的原文证据、追踪表查询/统计结果或已核对的图谱事实回答当前问题；'
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
        recovery=self.recovery_request(prepared,response)
        return handler(recovery) if recovery is not None else response

    async def awrap_model_call(self, request, handler):
        prepared=self.prepare(request)
        response=await handler(prepared)
        recovery=self.recovery_request(prepared,response)
        return await handler(recovery) if recovery is not None else response
