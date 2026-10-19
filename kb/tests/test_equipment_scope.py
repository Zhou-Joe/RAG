from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth import get_user_model
from kb.models import KnowledgeBase
from kb.views import _route_kb_by_question
from kb.agent import _build_agent
from kb.answer_text import AnswerText

class EquipmentScopeTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('scope',is_staff=True)
        self.folder=KnowledgeBase.objects.create(name='小矮人',slug='dwarfs',is_folder=True)
        self.cn=KnowledgeBase.objects.create(name='Manual_CN.pdf',slug='dwarfs-cn',parent=self.folder)
        self.en=KnowledgeBase.objects.create(name='Manual_EN.pdf',slug='dwarfs-en',parent=self.folder)
        self.other=KnowledgeBase.objects.create(name='金马手册.pdf',slug='other')

    def test_folder_name_routes_without_hardcoded_equipment_alias(self):
        self.assertEqual(_route_kb_by_question('小矮人束缚装置的检查要求，中英文列出来',self.user),self.folder)
        self.folder.name='新设备';self.folder.save()
        self.assertEqual(_route_kb_by_question('新设备的频率',self.user),self.folder)

    def tools(self):
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'):
            _build_agent('dwarfs','thread',{},5,None,allowed_kb_slugs=['dwarfs','dwarfs-cn','dwarfs-en'])
        return {tool.__name__:tool for tool in create.call_args.kwargs['tools']}

    def test_all_tool_channels_reject_unrelated_library(self):
        tools=self.tools()
        listing=tools['list_knowledge_bases']()
        self.assertIn('dwarfs-cn',listing);self.assertIn('dwarfs-en',listing)
        self.assertNotIn('金马',listing)
        with patch('kb.retriever.search') as search,patch('kb.retriever.fetch_doc') as fetch:
            self.assertIn('不存在',tools['kb_search']('检查',kb_slug='other'))
            self.assertIn('不存在',tools['kb_fetch_doc'](kb_slug='other'))
            self.assertIn('不存在',tools['tracker_lookup'](kb_slug='other'))
            search.assert_not_called();fetch.assert_not_called()

    def test_folder_search_passes_only_its_children(self):
        tools=self.tools()
        with patch('kb.retriever.search_folder',return_value=[]) as search:
            tools['kb_search']('检查')
            self.assertEqual(set(search.call_args.args[0]),{'dwarfs-cn','dwarfs-en'})

    def test_split_tool_markup_is_not_published(self):
        guard=AnswerText()
        self.assertEqual(guard.feed('<tool_'),'')
        with self.assertRaises(ValueError):guard.feed('call>\n<function=kb_fetch_doc>')

    def test_normal_paragraphs_and_final_line_survive(self):
        guard=AnswerText()
        self.assertEqual(guard.feed('正常解释'),'')
        self.assertEqual(guard.feed('\n\n后续'),'正常解释\n\n')
        self.assertEqual(guard.feed('',final=True),'后续')

from django.test import TransactionTestCase
from django.urls import reverse
class ScopeConversationTests(TransactionTestCase):
    def test_named_folder_overrides_wrong_previous_library_and_survives_followup(self):
        import asyncio
        from kb.models import Conversation,Message
        user=get_user_model().objects.create_user('scope-conv',is_staff=True)
        self.client.force_login(user);self.async_client.cookies=self.client.cookies
        wrong=KnowledgeBase.objects.create(name='金马手册.pdf',slug='wrong')
        folder=KnowledgeBase.objects.create(name='小矮人',slug='small',is_folder=True)
        KnowledgeBase.objects.create(name='cn.pdf',slug='cn',parent=folder)
        KnowledgeBase.objects.create(name='en.pdf',slug='en',parent=folder)
        conv=Conversation.objects.create(user=user,kb=wrong,thread_id='old-wrong')
        Message.objects.create(conversation=conv,role='user',content='金马手册.pdf')
        Message.objects.create(conversation=conv,role='ai',content='old wrong content')
        configs=[]
        async def events(message,thread,slug,config):
            configs.append((slug,config))
            yield 'token', {'text':'answer'}
        async def run():
            for question in ['小矮人束缚装置的检查要求，中英文列出来，用表格','每天需要检查什么？']:
                response=await self.async_client.post(reverse('kb:stream'),{'message':question,'thread_id':conv.thread_id,'kb_slug':'wrong'})
                self.assertEqual(response.status_code,200)
                _=[chunk async for chunk in response.streaming_content]
        with patch('kb.agent.run_agent_stream',side_effect=events):asyncio.run(run())
        for slug,cfg in configs:
            self.assertEqual(slug,'small')
            self.assertEqual(set(cfg['allowed_kb_slugs']),{'small','cn','en'})
        self.assertEqual(configs[0][1]['published_history'],[])
        conv.refresh_from_db();self.assertEqual(conv.kb_id,folder.pk)
