from django.test import SimpleTestCase
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage
from kb.agent_budget import RetrievalBudget

class RetrievalBudgetTests(SimpleTestCase):
    def request(self, count):
        messages = [HumanMessage(content='比较检查频率')]
        for i in range(count):
            messages += [AIMessage(content='', tool_calls=[{'id':str(i),'name':'search','args':{'query':str(i)}}]),
                         ToolMessage(content='有出处的原文', tool_call_id=str(i))]
        return ModelRequest(model=None,messages=messages,tools=[{'name':'search'}],system_message=SystemMessage(content='原规则'))

    def test_budget_reserves_final_answer_with_existing_evidence(self):
        request=self.request(5)
        result=RetrievalBudget().prepare(request)
        self.assertEqual(result.tools, [])
        self.assertEqual(result.messages, request.messages)
        self.assertIn('原规则', result.system_message.content)
        self.assertIn('缺失项', result.system_message.content)

    def test_under_budget_keeps_tools(self):
        request=self.request(4)
        self.assertIs(RetrievalBudget().prepare(request), request)

    def test_identical_search_loop_ends_early(self):
        request=self.request(2)
        request.messages[-2].tool_calls[0]['args']={'query':'0'}
        self.assertEqual(RetrievalBudget().prepare(request).tools, [])

    def test_new_user_turn_resets_budget(self):
        request=self.request(5)
        request.messages.append(HumanMessage(content='新问题'))
        self.assertIs(RetrievalBudget().prepare(request), request)
