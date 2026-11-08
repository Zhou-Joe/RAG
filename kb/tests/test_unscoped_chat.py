"""General chats never pick an arbitrary KB or persist one as a default."""
import asyncio
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from kb import access
from kb.agent import _build_agent
from kb.history import remove_legacy_kb_defaults
from kb.models import Conversation, KnowledgeBase, Message
from kb.tests.test_conversation_context import ConversationModel


class LegacyDefaultCleanupTests(SimpleTestCase):
    def test_cleanup_removes_only_injected_human_prefix_and_preserves_original_objects(self):
        source=HumanMessage(content='【当前默认知识库：old-kb】\nActual question',id='user-1')
        ordinary=HumanMessage(content='Quoted text: 【当前默认知识库：old-kb】\nDo not modify this quote.')
        answer=AIMessage(content='【当前默认知识库：old-kb】\nOld answer')
        tool=ToolMessage(content='【当前默认知识库：source】\nOriginal data',tool_call_id='call-1')
        cleaned=remove_legacy_kb_defaults([source,ordinary,answer,tool])
        self.assertEqual(cleaned[0].content,'Actual question')
        self.assertEqual(cleaned[0].id,'user-1')
        self.assertEqual(source.content,'【当前默认知识库：old-kb】\nActual question')
        self.assertIs(cleaned[1],ordinary)
        self.assertIs(cleaned[2],answer)
        self.assertIs(cleaned[3],tool)


class ExplicitRetrievalTargetTests(TestCase):
    def test_retrieval_tools_do_not_fall_back_even_when_builder_receives_a_kb(self):
        kb=KnowledgeBase.objects.create(name='Do not default here',slug='not-a-default')
        with patch('langchain.agents.create_agent') as create,patch('kb.agent._get_llm'):
            _build_agent(kb.slug,'no-default-tools',{},5,None)
        tools={tool.__name__:tool for tool in create.call_args.kwargs['tools']}
        with patch('kb.retriever.search') as search,patch('kb.retriever.search_folder') as folder, \
             patch('kb.retriever.fetch_doc') as fetch,patch('kb.knowledge_graph.lookup_facts') as graph, \
             patch('kb.tracker_query.TrackerQuery.call') as tracker:
            outputs=[tools['kb_search']('query'),tools['kb_graph_search']('query'),
                     tools['kb_image_search']('query'),tools['kb_fetch_doc'](),
                     tools['tracker_preview'](),tools['tracker_lookup'](),
                     tools['kb_neighbor_chunk'](source='manual.pdf',anchor='anchor')]
        for output in outputs:self.assertIn('kb_slug',output)
        for tool in (search,folder,fetch,graph,tracker):tool.assert_not_called()
        self.assertIn(kb.name,tools['list_knowledge_bases']())


class UnscopedConversationTests(TransactionTestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('unscoped-owner')
        self.other=get_user_model().objects.create_user('unscoped-other',is_staff=True)
        self.client.force_login(self.user)
        self.async_client.cookies=self.client.cookies

    def ask(self,message='Hello',thread='unscoped-chat',**extra):
        async def consume():
            response=await self.async_client.post(reverse('kb:stream'),{'message':message,'thread_id':thread,**extra})
            self.assertEqual(response.status_code,200)
            return b''.join([part async for part in response.streaming_content]).decode()
        return asyncio.run(consume())

    def test_can_chat_without_any_knowledge_base_and_manage_own_history(self):
        model=ConversationModel(responses=[AIMessage(content='Hello. How can I help?')])
        with patch('kb.config.llm_settings',return_value={'qa_enhance':False}), \
             patch('kb.agent._get_llm',return_value=model),patch('kb.retriever.search') as search:
            wire=self.ask()
        self.assertIn('Hello. How can I help?',wire)
        search.assert_not_called()
        conv=Conversation.objects.get(thread_id='unscoped-chat')
        self.assertIsNone(conv.kb_id)
        self.assertNotIn('当前默认知识库',repr(model.seen))
        self.assertNotIn('默认范围',repr(model.seen))
        response=self.client.get(reverse('kb:conv_messages',args=[conv.thread_id]))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['kb_slug'],'')
        self.assertContains(self.client.get(reverse('kb:ask'),{'embed':'1'}),conv.title)
        self.assertIn(conv.thread_id,str(self.client.get(reverse('kb:conv_states')).json()))
        answer=conv.messages.get(role='ai')
        self.assertEqual(self.client.post(reverse('kb:conv_read',args=[conv.thread_id]),{'answer_id':answer.pk}).status_code,200)
        self.assertEqual(self.client.post(reverse('kb:conv_stop',args=[conv.thread_id])).status_code,200)
        self.assertEqual(self.client.post(reverse('kb:conv_delete',args=[conv.thread_id]),HTTP_X_REQUESTED_WITH='XMLHttpRequest').status_code,200)
        self.assertFalse(Conversation.objects.filter(pk=conv.pk).exists())

    def test_existing_kbs_and_old_client_parameter_cannot_assign_a_default(self):
        public=KnowledgeBase.objects.create(name='Public',slug='public-kb')
        private=KnowledgeBase.objects.create(name='Other private',slug='other-private',
            visibility='individual',created_by=self.other)
        seen=[]
        async def events(message,thread,slug,config):
            seen.append((slug,config))
            yield 'token',{'text':'Answered without a default.'}
        with patch('kb.agent.run_agent_stream',side_effect=events):
            self.ask('What can you do?',kb_slug=private.slug)
        conv=Conversation.objects.get(thread_id='unscoped-chat')
        self.assertIsNone(conv.kb_id)
        self.assertEqual(seen[0][0],'')
        self.assertEqual(seen[0][1]['allowed_kb_slugs'],[public.slug])

    def test_resuming_legacy_chat_does_not_send_its_old_kb_as_default(self):
        kb=KnowledgeBase.objects.create(name='Old selection',slug='old-selection')
        conv=Conversation.objects.create(user=self.user,kb=kb,thread_id='legacy-chat')
        conv.agent_context={'version':1,'messages':messages_to_dict([
            HumanMessage(content='【当前默认知识库：old-selection】\nOriginal question'),
            AIMessage(content='Earlier answer.')]),'citations':[],'evidence':[],'rag':{}}
        conv.save(update_fields=['agent_context'])
        model=ConversationModel(responses=[AIMessage(content='Continuing normally.')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.config.llm_settings',return_value={'qa_enhance':False}):
            self.ask('Continue.',thread=conv.thread_id)
        self.assertNotIn('当前默认知识库',repr(model.seen))
        self.assertIn('Original question',repr(model.seen))
        self.assertIn('Earlier answer.',repr(model.seen))
        conv.refresh_from_db()
        self.assertNotIn('当前默认知识库',repr(conv.agent_context))
        self.assertEqual(self.client.get(reverse('kb:conv_messages',args=[conv.thread_id])).json()['kb_slug'],'')

    def test_unscoped_chats_remain_owner_only(self):
        mine=Conversation.objects.create(user=self.user,thread_id='my-general-chat',title='My private question')
        theirs=Conversation.objects.create(user=self.other,thread_id='other-general-chat',title='Other private question')
        Message.objects.create(conversation=mine,role='ai',content='My answer')
        self.assertTrue(access.conversation_accessible(self.user,mine))
        self.assertFalse(access.conversation_accessible(self.other,mine))
        self.assertFalse(Conversation.objects.filter(access.conversation_q(self.user),pk=theirs.pk).exists())
        self.assertEqual(self.client.get(reverse('kb:conv_messages',args=[theirs.thread_id])).status_code,404)
        self.assertNotIn(theirs.thread_id,str(self.client.get(reverse('kb:conv_states')).json()))
        self.assertNotContains(self.client.get(reverse('kb:ask'),{'embed':'1'}),theirs.title)
        from dashboard.views import _conversations
        self.assertFalse(_conversations(self.other).filter(pk=mine.pk).exists())

    def test_legacy_private_chat_stays_inaccessible_to_a_different_owner(self):
        private=KnowledgeBase.objects.create(name='Private',slug='legacy-private',visibility='individual',created_by=self.other)
        conv=Conversation.objects.create(user=self.user,kb=private,thread_id='revoked-legacy')
        self.assertFalse(access.conversation_accessible(self.user,conv))
        self.assertEqual(self.client.get(reverse('kb:conv_messages',args=[conv.thread_id])).status_code,404)
        self.assertNotIn(conv.thread_id,str(self.client.get(reverse('kb:conv_states')).json()))
