"""Exercise the real streaming entry point without external model/network I/O."""
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, messages_to_dict

from kb.agent import run_agent_stream


def immediate_sync_to_async(function, **options):
    async def call(*args, **kwargs):
        return function(*args, **kwargs)
    return call


class FakeStreamingAgent:
    def __init__(self):
        self.inputs = []

    async def astream_events(self, inputs, **options):
        self.inputs.append(inputs)
        answer = AIMessage(content='Ready.', response_metadata={'finish_reason': 'stop'})
        yield {'event': 'on_chat_model_start', 'data': {}}
        yield {'event': 'on_chat_model_stream', 'data': {'chunk': AIMessageChunk(content='Ready.')}}
        yield {'event': 'on_chat_model_end', 'data': {'output': answer}}
        yield {'event': 'on_chain_end', 'parent_ids': [],
               'data': {'output': {'messages': inputs['messages'] + [answer]}}}


class AgentStreamStartupTests(SimpleTestCase):
    def stream(self, config, *, scope=None):
        agent = FakeStreamingAgent()
        planner = AsyncMock(return_value=('', {'input_tokens': 0, 'output_tokens': 0}))
        verifier = AsyncMock(return_value=({'verdict': 'pass'}, {'input_tokens': 0, 'output_tokens': 0}))

        async def collect():
            return [event async for event in run_agent_stream('Hello', 'startup-regression', '', config)]

        with patch('kb.agent.sync_to_async', immediate_sync_to_async, create=True), \
             patch('kb.history.field_visibility_scope', return_value=scope or {}), \
             patch('kb.agent._build_agent', return_value=agent), \
             patch('kb.qa_steps.plan_query', planner), patch('kb.qa_steps.verify_answer', verifier), \
             patch('kb.publication.validate_sources', return_value=True):
            # All dependencies complete immediately. Driving the coroutine also
            # works on hosts where an asyncio loop's localhost socket is blocked.
            coroutine = collect()
            try:
                pending = coroutine.send(None)
            except StopIteration as complete:
                events = complete.value
            else:
                coroutine.close()
                self.fail(f'Unexpected external asynchronous work: {pending!r}')
        return events, agent, planner

    def test_first_reply_streams_and_saves_context_after_loading_field_visibility(self):
        config = {'llm': {'qa_enhance': False}, 'top_k': 5}
        scope = {'operations': ['Name', 'Count']}
        events, agent, _ = self.stream(config, scope=scope)
        self.assertFalse([data for kind, data in events if kind == 'error'], events)
        self.assertEqual(''.join(data['text'] for kind, data in events if kind == 'token'), 'Ready.')
        self.assertEqual(len(agent.inputs), 1)
        self.assertEqual(config['next_context']['field_visibility'], scope)
        self.assertEqual(config['next_context']['messages'][-1]['data']['content'], 'Ready.')

    def test_enhanced_reply_completes_without_a_local_import_shadowing_the_async_helper(self):
        config = {'llm': {'qa_enhance': True}, 'top_k': 5}
        events, _, planner = self.stream(config)
        self.assertFalse([data for kind, data in events if kind == 'error'], events)
        self.assertTrue(any(kind == 'verify' and data['ok'] for kind, data in events), events)
        self.assertTrue(any(kind == 'token' and data['text'] == 'Ready.' for kind, data in events))
        planner.assert_awaited_once()
        self.assertIn('next_context', config)

    def test_stale_hidden_field_history_is_omitted_from_both_model_and_planner(self):
        previous = [HumanMessage(content='OLD_HIDDEN_INPUT'), AIMessage(content='OLD_HIDDEN_VALUE')]
        config = {'llm': {'qa_enhance': True}, 'top_k': 5,
                  'conversation_context': {'version': 1, 'messages': messages_to_dict(previous),
                                           'field_visibility': {}, 'evidence': [], 'citations': []},
                  'published_history': [{'role': 'assistant', 'content': 'OLD_HIDDEN_VALUE'}]}
        events, agent, planner = self.stream(config, scope={'operations': ['Name']})
        self.assertFalse([data for kind, data in events if kind == 'error'], events)
        self.assertNotIn('OLD_HIDDEN', repr(agent.inputs))
        self.assertEqual(planner.call_args.kwargs['history'], [])
