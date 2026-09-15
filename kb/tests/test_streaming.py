import asyncio
from django.test import SimpleTestCase
from kb.streaming import bounded_events

class BoundedStreamTests(SimpleTestCase):
    def test_waiting_heartbeats_then_timeout_and_cancels_producer(self):
        async def check():
            closed=[]
            async def source():
                try:
                    await asyncio.sleep(5)
                    yield 'token', {'text':'late'}
                finally: closed.append(True)
            rows=[x async for x in bounded_events(source(),timeout=.035,heartbeat=.01)]
            self.assertGreaterEqual(sum(e=='heartbeat' for e,p in rows),2)
            self.assertEqual(rows[-1][1]['code'],'turn_timeout')
            self.assertTrue(closed)
            self.assertFalse(any(e=='token' for e,p in rows))
        asyncio.run(check())

    def test_complete_stream_passes_tokens_without_timeout(self):
        async def check():
            async def source():
                yield 'token', {'text':'answer'}
            rows=[x async for x in bounded_events(source(),timeout=1,heartbeat=.01)]
            self.assertEqual(rows[-1],('token',{'text':'answer'}))
        asyncio.run(check())

    def test_consumer_disconnect_closes_waiting_producer(self):
        async def check():
            closed=[]
            async def source():
                try:
                    await asyncio.sleep(5)
                    yield 'token', {'text':'late'}
                finally:closed.append(True)
            stream=bounded_events(source(),timeout=1,heartbeat=.01)
            await anext(stream)
            await anext(stream)
            await stream.aclose()
            self.assertTrue(closed)
        asyncio.run(check())

class HistoryBudgetTests(SimpleTestCase):
    def test_failed_questions_and_old_tool_context_are_not_replayed(self):
        from types import SimpleNamespace as M
        from kb.history import recent_history
        def msg(role,text):return M(role=role,content=text,verified=False,citations=[])
        rows=recent_history([msg('user','failed'),msg('user','new'),msg('ai','answer'),msg('user','pending')],enhanced=False,visible=lambda c:True)
        self.assertEqual([r['content'] for r in rows],['new','answer'])

    def test_budget_does_not_split_answer_or_replay_revoked_sources(self):
        from types import SimpleNamespace as M
        from kb.history import recent_history
        items=[M(role='user',content='q'),M(role='ai',content='long answer',verified=True,citations=[{}])]
        self.assertEqual(recent_history(items,enhanced=False,visible=lambda c:False),[])
        self.assertEqual(recent_history(items,enhanced=False,visible=lambda c:True,max_chars=4),[])
