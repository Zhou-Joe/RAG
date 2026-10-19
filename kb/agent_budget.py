"""Reserve a final model call instead of exhausting the graph on retrieval loops."""
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


class RetrievalBudget(AgentMiddleware):
    """Limit tool rounds per current user turn; history does not consume the budget."""
    def __init__(self, max_rounds=5):
        self.max_rounds = max_rounds

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
        if rounds < self.max_rounds and not repeated:
            return request
        original = request.system_message.content if request.system_message else ''
        instruction = ('\n检索阶段已结束。只能使用本轮工具返回的原文证据回答当前问题；'
                       '禁止继续调用工具、补写不存在的数据或把旧答案当事实。'
                       '若当前对象不明确，直接请求用户确认；若资料不足，明确列出缺失项。'
                       '不要声称已完成完整比较，除非各方资料都支持；请用简洁中文给出最终回答。')
        return request.override(tools=[], tool_choice=None,
                                system_message=SystemMessage(content=str(original) + instruction))

    def wrap_model_call(self, request, handler):
        return handler(self.prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self.prepare(request))
