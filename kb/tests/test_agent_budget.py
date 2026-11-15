from django.test import SimpleTestCase
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage
from kb.agent_budget import (DEFAULT_MAX_ROUNDS, GRAPH_RECURSION_LIMIT, MAX_IDENTICAL_ROUNDS,
    PRESENTATION_RESERVE_ROUNDS, RetrievalBudget)

class RetrievalBudgetTests(SimpleTestCase):
    def request(self, count):
        messages = [HumanMessage(content='比较检查频率')]
        for i in range(count):
            messages += [AIMessage(content='', tool_calls=[{'id':str(i),'name':'search','args':{'query':str(i)}}]),
                         ToolMessage(content='有出处的原文', tool_call_id=str(i))]
        return ModelRequest(model=None,messages=messages,tools=[{'name':'search'}],system_message=SystemMessage(content='原规则'))

    def test_budget_reserves_final_answer_with_existing_evidence(self):
        request=self.request(DEFAULT_MAX_ROUNDS)
        result=RetrievalBudget().prepare(request)
        self.assertEqual(result.tools, [])
        self.assertEqual(result.messages, request.messages)
        self.assertIn('原规则', result.system_message.content)
        self.assertIn('缺失项', result.system_message.content)

    def test_under_budget_keeps_tools(self):
        request=self.request(DEFAULT_MAX_ROUNDS-1)
        self.assertIs(RetrievalBudget().prepare(request), request)

    def test_only_consecutive_identical_rounds_end_exploration_early(self):
        request=self.request(MAX_IDENTICAL_ROUNDS)
        for message in request.messages:
            if isinstance(message,AIMessage):message.tool_calls[0]['args']={'query':'same'}
        self.assertEqual(RetrievalBudget().prepare(request).tools, [])

    def test_a_retry_or_returning_to_an_earlier_query_does_not_disable_other_tools(self):
        for count in (2,4):
            request=self.request(count)
            request.messages[-2].tool_calls[0]['args']={'query':'0'}
            self.assertIs(RetrievalBudget().prepare(request),request)

    def test_chart_tools_remain_available_when_retrieval_reaches_the_reserved_rounds(self):
        request=self.request(DEFAULT_MAX_ROUNDS-PRESENTATION_RESERVE_ROUNDS)
        presentation=[{'name':name} for name in ('tracker_visualize','tracker_analyze','tracker_show_value','kb_show_value')]
        request.tools.extend(presentation)
        prepared=RetrievalBudget().prepare(request)
        self.assertEqual(prepared.tools,presentation)
        self.assertNotIn('预算已用完',prepared.system_message.content)

    def test_repeated_retrieval_does_not_disable_chart_tools(self):
        request=self.request(MAX_IDENTICAL_ROUNDS)
        for message in request.messages:
            if isinstance(message,AIMessage):message.tool_calls[0]['args']={'query':'same'}
        request.tools.append({'name':'tracker_visualize'})
        self.assertEqual(RetrievalBudget().prepare(request).tools,[{'name':'tracker_visualize'}])

    def test_more_than_two_charts_can_be_created_but_the_total_allowance_is_twelve(self):
        self.assertEqual(DEFAULT_MAX_ROUNDS,12)
        request=self.request(0)
        request.tools.append({'name':'tracker_visualize'})
        for index in range(DEFAULT_MAX_ROUNDS):
            self.assertIn({'name':'tracker_visualize'},RetrievalBudget().prepare(request).tools)
            request.messages.extend([
                AIMessage(content='',tool_calls=[{'id':str(index),'name':'tracker_visualize','args':{'title':str(index)}}]),
                ToolMessage(content='Rendered',tool_call_id=str(index))])
        self.assertEqual(RetrievalBudget().prepare(request).tools,[])

    def test_new_user_turn_resets_budget(self):
        request=self.request(5)
        request.messages.append(HumanMessage(content='新问题'))
        self.assertIs(RetrievalBudget().prepare(request), request)

    def test_evidence_recovery_cannot_restore_tools_after_the_twelve_round_limit(self):
        from types import SimpleNamespace
        selection=SimpleNamespace(pending=True,loaded={'C1'},presented={'C1'},
            state={'used':True,'finalized':False})
        request=self.request(DEFAULT_MAX_ROUNDS)
        request.tools.extend([{'name':'kb_select_evidence'},{'name':'kb_read_chunks'}])
        calls=[]
        def handler(prepared):
            calls.append(prepared)
            return SimpleNamespace(result=[AIMessage(content='Insufficient evidence')])
        RetrievalBudget(selection=selection).wrap_model_call(request,handler)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].tools,[])

    def test_real_agent_can_fetch_nine_pages_then_render_three_charts_in_twelve_rounds(self):
        from langchain.agents import create_agent
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.tools import tool

        class PlannedModel(FakeMessagesListChatModel):
            available: list[list[str]] = []

            def bind_tools(self,tools,**kwargs):
                names=[tool.name for tool in tools]
                self.available.append(names)
                for call in self.responses[self.i].tool_calls:
                    if call['name'] not in names:
                        raise AssertionError('The next requested chart tool was removed: '+call['name'])
                return self

        @tool
        def search(page:int)->str:
            """Read the requested page of source data."""
            return str(page)

        @tool
        def tracker_visualize(title:str)->str:
            """Render a chart from the retrieved data."""
            return 'Rendered '+title

        responses=[AIMessage(content='',tool_calls=[{'id':str(i),'name':'search','args':{'page':i}}])
                   for i in range(9)]
        responses.extend(AIMessage(content='',tool_calls=[{'id':'chart'+str(i),'name':'tracker_visualize',
            'args':{'title':'Chart '+str(i)}}]) for i in range(3))
        model=PlannedModel(responses=responses+[AIMessage(content='Complete')])
        agent=create_agent(model=model,tools=[search,tracker_visualize],middleware=[RetrievalBudget()])
        result=agent.invoke({'messages':[HumanMessage(content='Compare the data and show three charts')]},
            config={'recursion_limit':GRAPH_RECURSION_LIMIT})
        outputs=[message for message in result['messages'] if isinstance(message,ToolMessage)]
        self.assertEqual(len(outputs),12)
        self.assertEqual([message.name for message in outputs[-3:]],['tracker_visualize']*3)
        self.assertEqual(model.available[-3:],[['tracker_visualize']]*3)
        self.assertEqual(result['messages'][-1].content,'Complete')
