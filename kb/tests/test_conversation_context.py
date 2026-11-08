"""Exercise durable, append-only conversations through real agent/tool execution."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, messages_from_dict, messages_to_dict
from langchain_core.outputs import ChatGenerationChunk
from pydantic import Field

from kb.agent import _build_agent
from kb.history import load_context
from kb.models import Conversation, Document, KnowledgeBase, Message
from kb.publication import digest
from kb.rag_selection import RagSelection


class ConversationModel(FakeMessagesListChatModel):
    seen: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        response = self.responses[self.i]
        self.i = (self.i + 1) % len(self.responses)
        yield ChatGenerationChunk(message=AIMessageChunk(content=response.content,
            tool_calls=response.tool_calls, response_metadata={'finish_reason': 'tool_calls' if response.tool_calls else 'stop'}))


def call(name, args, ident):
    return AIMessage(content='', tool_calls=[{'id':ident,'name':name,'args':args}])


class ConversationExecutionTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('conversation-memory', is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='Memory manual',slug='memory-manual')
        self.doc = Document.objects.create(kb=self.kb, original_name='Memory manual',status='completed',md_content='bearing manual')
        self.rows = [{'doc_id':str(self.doc.pk),'source':self.doc.original_name,'kb_slug':self.kb.slug,
            'chunk_id':'bearing-1','text':'The bearing inspection interval is daily.','section':'Maintenance'}]
        self.client.force_login(self.user)
        self.async_client.cookies = self.client.cookies

    async def ask(self, question, thread='memory-thread'):
        response = await self.async_client.post(reverse('kb:stream'), {
            'message':question,'thread_id':thread,'kb_slug':self.kb.slug})
        self.assertEqual(response.status_code,200)
        return b''.join([chunk async for chunk in response.streaming_content]).decode()

    def test_followup_reuses_full_tool_context_with_an_identical_prefix_and_no_search(self):
        model = ConversationModel(responses=[
            call('kb_search',{'query':'bearing interval','kb_slug':self.kb.slug},'search-1'),
            call('kb_read_chunks',{'candidate_ids':['C1']},'read-1'),
            call('kb_select_evidence',{'candidate_ids':['C1']},'select-1'),
            AIMessage(content='Inspect the bearing daily.'),
            AIMessage(content='Daily means once each day.'),
        ])
        with patch('kb.config.llm_settings',return_value={'qa_enhance':False}), patch('kb.agent._get_llm',return_value=model), patch(
            'kb.retriever.search', return_value=self.rows,
        ) as search, patch('kb.provenance.annotate_results'), patch('kb.keyword_index.get_chunks',return_value=self.rows):
            asyncio.run(self.ask('What is the inspection interval?'))
            first = Conversation.objects.get(thread_id='memory-thread').agent_context
            self.assertEqual(len(first['messages']), 8)
            self.assertIn('daily',repr(first['messages']))
            wire = asyncio.run(self.ask('In Memory manual, explain what daily means.'))
        self.assertIn('Daily means once each day.',wire)
        self.assertEqual(search.call_count,1)
        self.assertEqual(len(model.seen),5)
        expected = messages_from_dict(first['messages'])
        self.assertEqual(messages_to_dict(model.seen[-1][1:-1]),messages_to_dict(expected))
        self.assertEqual(model.seen[0][0].content, model.seen[-1][0].content)
        restored = Conversation.objects.get(thread_id='memory-thread').agent_context
        self.assertEqual(restored['messages'][:len(first['messages'])],first['messages'])
        self.assertEqual(len(restored['messages']),10)
        self.assertEqual(restored['rag']['loaded'],['C1'])
        self.assertEqual(restored['citations'][0]['selected_chunk_ids'],['bearing-1'])
        self.assertEqual(Message.objects.filter(conversation__thread_id='memory-thread',completion_status='complete',role='ai').count(),2)

    def test_model_can_choose_a_new_search_on_a_later_turn(self):
        model = ConversationModel(responses=[AIMessage(content='Hello.'),
            call('kb_search',{'query':'bearing interval','kb_slug':self.kb.slug},'search'),
            call('kb_read_chunks',{'candidate_ids':['C1']},'read'),
            call('kb_select_evidence',{'candidate_ids':['C1']},'select'),
            AIMessage(content='The manual says daily.')])
        with patch('kb.config.llm_settings',return_value={'qa_enhance':False}), patch('kb.agent._get_llm',return_value=model), patch(
            'kb.retriever.search',return_value=self.rows,
        ) as search, patch('kb.provenance.annotate_results'):
            asyncio.run(self.ask('Hello'))
            wire = asyncio.run(self.ask('Find the bearing inspection interval.'))
        search.assert_called_once()
        self.assertIn('The manual says daily.',wire)

    def test_enhanced_followup_reuses_retained_evidence_without_planning_or_search(self):
        from kb.agent import run_agent_stream
        context = {'messages':messages_to_dict([HumanMessage(content='interval?'),AIMessage(content='daily')]),
            'evidence':[{'text':'daily','source':'manual.txt'}], 'citations':[], 'rag':{}}
        config = {'conversation_context':context,'llm':{'qa_enhance':True},'top_k':5,'user_id':self.user.pk}
        model = ConversationModel(responses=[AIMessage(content='Daily means once per day.')])
        async def consume():
            return [event async for event in run_agent_stream('Explain that interval.','enhanced-memory',self.kb.slug,config)]
        with patch('kb.agent._get_llm',return_value=model), patch('kb.qa_steps.plan_query',new_callable=AsyncMock) as planner, patch(
            'kb.qa_steps.verify_answer',new_callable=AsyncMock,return_value=({'verdict':'pass'}, {'input_tokens':0,'output_tokens':0}),
        ) as verify, patch('kb.publication.validate_sources',return_value=True), patch('kb.retriever.search') as search:
            events=asyncio.run(consume())
        planner.assert_not_called();search.assert_not_called()
        self.assertEqual(verify.call_args.args[3],context['evidence'])
        self.assertIn('Daily means once per day.',repr(events))
        self.assertIn('next_context',config)

    def test_failed_turn_never_overwrites_the_last_completed_context(self):
        conv = Conversation.objects.create(user=self.user,kb=self.kb,thread_id='failed-context',
            agent_context={'version':1,'messages':messages_to_dict([HumanMessage(content='hello'),AIMessage(content='Hi')]),
                           'rag':{},'evidence':[],'citations':[]})
        previous = deepcopy(conv.agent_context)
        async def broken(message, thread, slug, config):
            config['next_context'] = {'version':1,'messages':[], 'private':'incomplete'}
            yield 'token',{'text':'unfinished'}
            yield 'error',{'message':'interrupted'}
        with patch('kb.config.llm_settings',return_value={'qa_enhance':False}), patch('kb.agent.run_agent_stream',side_effect=broken):
            asyncio.run(self.ask('new question',thread=conv.thread_id))
        conv.refresh_from_db()
        self.assertEqual(conv.agent_context,previous)


class ContextSourceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('memory-source-user')
        self.kb = KnowledgeBase.objects.create(name='Public source',slug='memory-source')
        self.doc = Document.objects.create(kb=self.kb, original_name='manual.txt',status='completed',md_content='daily')
        self.context = {'version':1,'messages':messages_to_dict([HumanMessage(content='question'),AIMessage(content='daily')]),
            'rag':{'candidates':{'C1':{'snapshot':{'doc_id':str(self.doc.pk),'document_digest':digest('daily')}}}},
            'evidence':[],'citations':[]}
        self.conv = Conversation.objects.create(user=self.user,kb=self.kb,thread_id='source-memory',agent_context=self.context)

    def test_unchanged_source_keeps_the_exact_transcript(self):
        self.assertEqual(load_context(self.conv,self.user),self.context)

    def test_permission_or_content_changes_invalidate_old_tool_material(self):
        self.kb.department = 'restricted';self.kb.save()
        self.assertEqual(load_context(self.conv,self.user),{})
        self.kb.department = '通用';self.kb.save()
        self.doc.md_content = 'weekly';self.doc.save()
        self.assertEqual(load_context(self.conv,self.user),{})

    def test_resumed_selection_retains_ids_and_allows_new_candidates(self):
        evidence, citations = [], []
        first = RagSelection(lambda kb:True,evidence,citations)
        row = {'doc_id':str(self.doc.pk),'source':self.doc.original_name,'chunk_id':'old','text':'daily'}
        first.discover([row],[self.kb.slug]);first.read(['C1']);first.select(['C1'])
        second = RagSelection(lambda kb:True,evidence,citations,saved=json.loads(json.dumps(first.snapshot())))
        second.select(['C1'])
        self.assertEqual(citations[0]['selected_chunk_ids'],['old'])
        result = json.loads(second.discover([{**row,'chunk_id':'new','text':'more details'}],[self.kb.slug]))
        self.assertEqual(result['candidates'][0]['candidate_id'],'C2')
        second.read(['C2']);second.select(['C2'])
        self.assertEqual(citations[0]['selected_chunk_ids'],['new'])

    def test_default_scope_changes_do_not_rewrite_the_system_prefix(self):
        prompts = []
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'):
            for slug in ('first-scope','next-scope'):
                _build_agent(slug,'stable',{},5,None)
                prompts.append(create.call_args.kwargs['system_prompt'])
        self.assertEqual(prompts[0],prompts[1])

    def test_manual_overview_keeps_the_same_system_and_tool_prefix_for_followups(self):
        state = {}
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'), patch(
            'kb.keyword_index.get_chunks',return_value=[{'section':'Chapter 1','text':'original chapter evidence','chunk_id':'old'}],
        ), patch('kb.provenance.annotate_results'):
            _build_agent(self.kb.slug,'overview-prefix',{},5,None,overview=True,retrieval_state=state)
            first = create.call_args.kwargs
            _build_agent(self.kb.slug,'overview-prefix',{},5,None)
            next_turn = create.call_args.kwargs
        self.assertIn('original chapter evidence',state['input_context'])
        self.assertEqual(first['system_prompt'],next_turn['system_prompt'])
        self.assertEqual([(t.__name__,t.__doc__) for t in first['tools']],
                         [(t.__name__,t.__doc__) for t in next_turn['tools']])
