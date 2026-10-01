from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from kb.agent import _get_llm
from kb.config import embedding_settings, normalize_openai_base_url
from kb.models import SiteConfig
from kb.pipeline import _embeddings


class LocalAIConfigurationTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="local-ai-admin", password=None, is_staff=True,
        )

        self.client.force_login(self.user)

    def test_ollama_url_gets_scheme_and_openai_prefix(self):
        self.assertEqual(
            normalize_openai_base_url("127.0.0.1:11434", ollama=True),
            "http://127.0.0.1:11434/v1",
        )

    # .env 里有真实云端 key（回退链第二层），置空后才能测「全空 → 占位符」逻辑
    @override_settings(EMBEDDING_API_KEY="")
    def test_empty_local_keys_can_construct_runtime_clients(self):
        llm = _get_llm({
            "model": "local-chat", "api_key": "",
            "base_url": "http://localhost:1234/v1", "temperature": 0.2,
        })
        self.assertEqual(llm.openai_api_key.get_secret_value(), "local-no-key")

        cfg = SiteConfig.get()
        cfg.embedding_base_url = "127.0.0.1:11434"
        cfg.embedding_dimensions = 2
        cfg.embedding_model = "bge-m3:latest"
        cfg.embedding_api_key = ""
        cfg.save()
        self.assertEqual(embedding_settings()["base_url"], "http://127.0.0.1:11434/v1")
        embeddings = _embeddings()
        self.assertEqual(embeddings.openai_api_key.get_secret_value(), "local-no-key")

    def test_settings_page_has_embedded_field_definitions(self):
        """设置页预设化后字段由 JS CFG_FIELDS 定义（无服务端渲染表单）。"""
        response = self.client.get(reverse("kb:settings"))
        self.assertContains(response, "embedding_base_url")
        self.assertContains(response, "cfgDialog")

    def test_chat_uses_automatically_selected_kb_slug(self):
        """The stream must not receive an empty default scope."""
        from unittest.mock import AsyncMock
        from kb.models import KnowledgeBase

        kb = KnowledgeBase.objects.create(
            name="Indexed manual", slug="indexed-manual", is_folder=False,
            chunk_count=3, created_by=self.user,
        )

        async def no_events(*args, **kwargs):
            if False:
                yield None

        with patch("kb.agent.run_agent_stream", side_effect=no_events) as stream:
            async def consume():
                self.async_client.cookies = self.client.cookies
                response = await self.async_client.post(reverse("kb:stream"), {
                    "message": "test", "thread_id": "auto-kb-thread",
                })
                return [chunk async for chunk in response.streaming_content]
            import asyncio
            asyncio.run(consume())

        self.assertEqual(stream.call_args.args[2], kb.slug)

    # 预设化后「测试」按钮只传 target，端点按【已保存配置】实际调用
    # （UI 上明确标注「使用已保存配置」）。
    @override_settings(EMBEDDING_API_KEY="")
    @patch("httpx.post")
    def test_connection_test_uses_saved_config(self, post):
        cfg = SiteConfig.get()
        cfg.embedding_base_url = "127.0.0.1:11434"
        cfg.embedding_dimensions = 2
        cfg.embedding_model = "bge-m3:latest"
        cfg.embedding_api_key = ""
        cfg.save()

        post.return_value.status_code = 200
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"data": [{"embedding": [0.1, 0.2]}]}

        response = self.client.post(reverse("kb:settings_test"), {"target": "embedding"})

        self.assertTrue(response.json()["results"]["embedding"]["ok"])
        called_url = post.call_args.args[0]
        self.assertEqual(called_url, "http://127.0.0.1:11434/v1/embeddings")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer local-no-key")

    @override_settings(LLM_BASE_URL="http://localhost:1234/v1", LLM_MODEL="configured-model")
    @patch("httpx.post")
    def test_chat_empty_or_truncated_is_not_success(self, post):
        post.return_value.status_code = 200
        for content, reason in [("", "stop"), ("OK", "length"), ("thinking", "stop")]:
            post.return_value.json.return_value = {"choices":[{"message":{"content":content},"finish_reason":reason}]}
            response=self.client.post(reverse("kb:settings_test"), {"target":"llm"})
            self.assertFalse(response.json()["results"]["llm"]["ok"])

    @override_settings(LLM_BASE_URL="http://localhost:1234/v1", LLM_MODEL="configured-model")
    @patch("httpx.post")
    def test_chat_reports_actual_model(self, post):
        post.return_value.status_code=200
        post.return_value.json.return_value={"model":"actual-loaded-model","choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]}
        result=self.client.post(reverse("kb:settings_test"), {"target":"llm"}).json()["results"]["llm"]
        self.assertIsNone(result["ok"])
        self.assertIn("actual-loaded-model",result["detail"])

    @patch("httpx.get")
    def test_ocr_health_is_success(self, get):
        get.return_value.status_code=200
        result=self.client.post(reverse("kb:settings_test"), {"target":"mineru"}).json()["results"]["mineru"]
        self.assertTrue(result["ok"])

    def test_all_button_and_invalid_target(self):
        self.assertContains(self.client.get(reverse("kb:settings")), 'data-test="all"')
        self.assertEqual(self.client.post(reverse("kb:settings_test"), {"target":"unknown"}).status_code,400)

    def test_wemm_openai_endpoint_does_not_use_native_protocol(self):
        cfg = SiteConfig.get()
        cfg.embedding_base_url='http://127.0.0.1:8081/v1'
        cfg.embedding_model='WeMM-Embedding-9B'
        cfg.embedding_dimensions=4096
        cfg.save()
        client=_embeddings()
        self.assertEqual(client.openai_api_base,'http://127.0.0.1:8081/v1')
        self.assertEqual(client.dimensions,4096)

    @patch('kb.rerank.rerank_settings',return_value={'enabled':True,'base_url':'http://127.0.0.1:8766/rerank','model':'test','api_key':''})
    @patch('httpx.post')
    def test_complete_rerank_url_is_not_appended_twice(self, post, cfg):
        from kb.rerank import rerank
        post.return_value.json.return_value={'results':[{'index':0,'relevance_score':1.0}]}
        self.assertIsNotNone(rerank('q',['a']))
        self.assertEqual(post.call_args.args[0],'http://127.0.0.1:8766/rerank')



from django.test import TransactionTestCase

class StreamPersistenceTests(TransactionTestCase):
    def setUp(self):
        from kb.models import KnowledgeBase
        self.user = get_user_model().objects.create_user(username='stream-admin',is_staff=True)
        self.client.force_login(self.user)
        self.async_client.cookies = self.client.cookies
        KnowledgeBase.objects.create(name='stream',slug='stream',is_folder=False,chunk_count=1,created_by=self.user)

    def test_stream_persists_before_done_and_marks_failed_partial(self):
        import asyncio
        from asgiref.sync import sync_to_async
        from kb.models import Message
        for fail in (False,True):
            thread='stream-order-'+str(fail)
            async def events(*args, **kwargs):
                yield 'token', {'text':'answer'}
                if fail: yield 'error', {'message':'截断'}
            async def consume():
                response=await self.async_client.post(reverse('kb:stream'),{'message':'q','thread_id':thread})
                async for raw in response.streaming_content:
                    if b'event: done' in raw:
                        row=await sync_to_async(lambda:Message.objects.get(conversation__thread_id=thread,role='ai'))()
                        self.assertEqual(row.completion_status,'incomplete' if fail else 'complete')
            with patch('kb.agent.run_agent_stream',side_effect=events): asyncio.run(consume())

    def test_switching_page_keeps_generation_running_and_saves_final_answer(self):
        import asyncio
        from kb.models import Message
        from kb.answer_tasks import _runs
        async def check():
            release=asyncio.Event()
            async def events(*args, **kwargs):
                yield 'token', {'text':'first'}
                await release.wait()
                yield 'token', {'text':' final'}
            with patch('kb.agent.run_agent_stream',side_effect=events):
                response=await self.async_client.post(reverse('kb:stream'),{'message':'q','thread_id':'switch'})
                run=next(iter(_runs.values()))
                async for raw in response.streaming_content:
                    if b'event: token' in raw:
                        await response._iterator.aclose()
                        break
                self.assertFalse(run.task.done())
                # Another turn in this conversation cannot race with the active one.
                duplicate=await self.async_client.post(reverse('kb:stream'),{'message':'again','thread_id':'switch'})
                self.assertEqual(duplicate.status_code,409)
                history=(await self.async_client.get(reverse('kb:conv_messages',args=['switch']))).json()
                self.assertIsNotNone(history['active_answer'])
                release.set()
                await run.task
        asyncio.run(check())
        row=Message.objects.get(conversation__thread_id='switch',role='ai')
        self.assertEqual(row.content,'first final')
        self.assertEqual(row.completion_status,'complete')

    def test_failure_before_first_token_is_saved(self):
        import asyncio
        from kb.models import Message
        async def events(*args, **kwargs):
            yield 'error', {'message':'模型请求超时'}
        async def consume():
            response=await self.async_client.post(reverse('kb:stream'), {'message':'q','thread_id':'empty'})
            return [raw async for raw in response.streaming_content]
        with patch('kb.agent.run_agent_stream',side_effect=events): asyncio.run(consume())
        row=Message.objects.get(conversation__thread_id='empty',role='ai')
        self.assertEqual(row.failure_reason,'模型请求超时')
        self.assertEqual(row.completion_status,'incomplete')

    def test_only_explicit_stop_cancels_background_answer(self):
        import asyncio
        from kb.models import Message
        from kb.answer_tasks import _runs
        async def check():
            started=asyncio.Event()
            async def events(*args, **kwargs):
                started.set()
                await asyncio.sleep(20)
                yield 'token', {'text':'late'}
            with patch('kb.agent.run_agent_stream',side_effect=events):
                response=await self.async_client.post(reverse('kb:stream'), {'message':'q','thread_id':'cancel'})
                run=next(iter(_runs.values()))
                await started.wait()
                await response._iterator.aclose()
                stop=await self.async_client.post(reverse('kb:conv_stop',args=['cancel']))
                self.assertEqual(stop.status_code,200)
                await run.task
        asyncio.run(check())
        row=Message.objects.get(conversation__thread_id='cancel',role='ai')
        self.assertEqual(row.content,'')
        self.assertEqual(row.completion_status,'incomplete')
        self.assertIn('按要求停止',row.failure_reason)

    def test_other_user_cannot_read_or_stop_task(self):
        from kb.models import Conversation, KnowledgeBase
        other=get_user_model().objects.create_user('other')
        Conversation.objects.create(user=other,kb=KnowledgeBase.objects.get(slug='stream'),thread_id='private')
        self.assertEqual(self.client.post(reverse('kb:conv_stop',args=['private'])).status_code,404)
        self.assertEqual(self.client.get(reverse('kb:conv_messages',args=['private'])).status_code,404)

    def test_queue_serializes_model_work_and_queued_job_can_stop(self):
        import asyncio
        from kb.answer_tasks import _runs
        from kb.models import Message
        async def check():
            entered=[]
            release=asyncio.Event()
            async def events(message,*args,**kwargs):
                entered.append(message)
                await release.wait()
                yield 'token', {'text':'done'}
            with patch('kb.agent.run_agent_stream',side_effect=events):
                await self.async_client.post(reverse('kb:stream'),{'message':'first','thread_id':'queue1'})
                first=list(_runs.values())[0]
                for _ in range(50):
                    if entered: break
                    await asyncio.sleep(.01)
                await self.async_client.post(reverse('kb:stream'),{'message':'second','thread_id':'queue2'})
                second=[run for run in _runs.values() if run is not first][0]
                await asyncio.sleep(.02)
                self.assertEqual(entered,['first'])
                await self.async_client.post(reverse('kb:conv_stop',args=['queue2']))
                await second.task
                release.set()
                await first.task
        asyncio.run(check())
        self.assertEqual(Message.objects.get(conversation__thread_id='queue2',role='ai').completion_status,'incomplete')

    def test_restart_marks_orphaned_task_incomplete(self):
        from kb.models import Conversation,KnowledgeBase,Message
        from django.utils import timezone
        from datetime import timedelta
        conv=Conversation.objects.create(user=self.user,kb=KnowledgeBase.objects.get(slug='stream'),thread_id='orphan')
        answer=Message.objects.create(conversation=conv,role='ai',content='partial',completion_status='running')
        Message.objects.filter(pk=answer.pk).update(created_at=timezone.now()-timedelta(minutes=1))
        data=self.client.get(reverse('kb:conv_messages',args=['orphan'])).json()
        self.assertIsNone(data['active_answer'])
        self.assertIn('已重启',data['messages'][-1]['failure_reason'])
        self.assertEqual(data['messages'][-1]['content'],'partial')
