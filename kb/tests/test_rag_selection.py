from __future__ import annotations

"""候选目录 → 选择性读取 → 最终引用，以及流式/持久化引用替换回归。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from kb.agent import _build_agent, run_agent_stream
from kb.agent_budget import RetrievalBudget
from kb.models import Document, KnowledgeBase
from kb.publication import validate_sources
from kb.rag_selection import MAX_DISCOVERY_CHARS, MAX_READ_CHARS, MAX_RESPONSE_CHARS, RagSelection


class RagFixture:
    def setUp(self):
        self.user = get_user_model().objects.create_user('rag-choice-reader')
        self.folder = KnowledgeBase.objects.create(name='资料',slug='rag-folder',is_folder=True)
        self.kbs = [KnowledgeBase.objects.create(name=f'手册{i}',slug=f'rag-{i}',parent=self.folder) for i in range(2)]
        self.docs = [Document.objects.create(kb=kb,original_name=f'Manual-{i}.pdf',status='completed',
            md_content=f'原始手册{i}') for i,kb in enumerate(self.kbs)]
        self.rows = [{'doc_id':str(doc.pk),'source':doc.original_name,'kb_slug':doc.kb.slug,
            'chunk_id':f'chunk-{i}','section':f'章节{i}','page_label':f'第 {i+1} 页',
            'text':('A' if i==0 else 'B')*300+f'ONLY_FULL_TEXT_{i}'} for i,doc in enumerate(self.docs)]
        self.evidence = []; self.citations = []; self.state = {}
        self.session = RagSelection(lambda kb:kb.department=='通用',self.evidence,self.citations,self.state)

    def discover(self):
        return json.loads(self.session.discover(self.rows,[kb.slug for kb in self.kbs],query='问题',limit=20))

    def tools(self):
        with patch('langchain.agents.create_agent') as create,patch('kb.agent._get_llm'):
            _build_agent(self.folder.slug,'rag-choice',{},5,None,evidence=self.evidence,citations=self.citations,
                retrieval_state=self.state)
        return {tool.__name__:tool for tool in create.call_args.kwargs['tools']}


class RagSelectionTests(RagFixture, TestCase):
    def test_discovery_summarizes_documents_and_does_not_publish_full_text_or_citations(self):
        result = self.discover()
        self.assertEqual(result['candidate_count'],2)
        self.assertIsNone(result['total_matching_chunks'])
        self.assertEqual({d['kb_slug'] for d in result['documents']},{kb.slug for kb in self.kbs})
        self.assertEqual([d['candidate_count'] for d in result['documents']],[1,1])
        self.assertNotIn('ONLY_FULL_TEXT',json.dumps(result))
        self.assertEqual(self.evidence,[])
        self.assertEqual(self.citations,[])
        again = self.discover()
        self.assertEqual([r['candidate_id'] for r in again['candidates']],['C1','C2'])

    def test_only_selected_read_chunks_appear_in_final_evidence_and_citations(self):
        self.discover()
        read = json.loads(self.session.read(['C1','C2']))
        self.assertIn('ONLY_FULL_TEXT_1',read['chunks'][1]['text'])
        self.assertEqual(self.citations,[])
        self.assertTrue(self.session.pending)
        result = json.loads(self.session.select(['C2']))
        self.assertEqual(result['selected_chunks'],1)
        self.assertEqual([e['chunk_id'] for e in self.evidence],['chunk-1'])
        self.assertEqual([c['doc_id'] for c in self.citations],[str(self.docs[1].pk)])
        self.assertEqual(self.citations[0]['selected_chunk_ids'],['chunk-1'])
        self.assertEqual(self.citations[0]['chunks'][0]['chunk_id'],'chunk-1')
        self.assertFalse(self.session.pending)
        with patch('kb.keyword_index.get_chunks',return_value=[self.rows[1]]):
            self.assertTrue(validate_sources(self.evidence,self.user.pk))

    def test_new_read_invalidates_previous_choice_and_choice_replaces_instead_of_merging(self):
        self.discover(); self.session.read(['C1']); self.session.select(['C1'])
        self.citations.append({'kind':'tracker','result_digest':'keep'})
        self.evidence.append({'kind':'tracker','text':'keep'})
        self.session.read(['C2'])
        self.assertEqual(self.citations,[{'kind':'tracker','result_digest':'keep'}])
        self.session.select(['C2'])
        self.assertEqual({c.get('doc_id') for c in self.citations if c.get('rag_selected')},{str(self.docs[1].pk)})
        self.assertEqual(len(self.citations),2)
        self.assertEqual([e['chunk_id'] for e in self.evidence if e.get('rag_selected')],['chunk-1'])

    def test_cannot_read_guessed_ids_select_unread_evidence_or_reuse_revoked_documents(self):
        self.discover()
        self.assertIn('error',json.loads(self.session.read(['C999'])))
        self.assertIn('error',json.loads(self.session.select(['C1'])))
        self.kbs[0].department='机械';self.kbs[0].save(update_fields=['department'])
        self.assertIn('error',json.loads(self.session.read(['C1'])))
        self.docs[1].md_content='新版内容';self.docs[1].save(update_fields=['md_content'])
        self.assertIn('error',json.loads(self.session.read(['C2'])))
        self.assertEqual(self.citations,[])

    def test_hidden_and_out_of_search_scope_documents_do_not_enter_candidate_counts(self):
        self.kbs[0].department='机械';self.kbs[0].save(update_fields=['department'])
        result = self.discover()
        self.assertEqual(result['candidate_count'],1)
        self.assertEqual(result['documents'][0]['source'],self.docs[1].original_name)
        self.assertNotIn(self.docs[0].original_name,json.dumps(result))
        result=json.loads(self.session.discover([self.rows[1]],[self.kbs[0].slug]))
        self.assertEqual(result['candidate_count'],0)

    def test_new_discovery_uses_a_fresh_candidate_after_document_version_changes(self):
        self.discover()
        self.docs[0].md_content='新版';self.docs[0].save(update_fields=['md_content'])
        result=self.discover()
        fresh=next(c['candidate_id'] for c in result['candidates'] if c['doc_id']==str(self.docs[0].pk))
        self.assertNotEqual(fresh,'C1')
        self.assertNotIn('error',json.loads(self.session.read([fresh])))

    def test_no_hits_does_not_require_selecting_nonexistent_evidence(self):
        result=json.loads(self.session.discover([], [kb.slug for kb in self.kbs]))
        self.assertEqual(result['candidate_count'],0)
        self.assertFalse(self.state.get('used'))

    def test_large_chunks_are_not_silently_truncated_and_already_read_text_is_not_replayed(self):
        self.rows[0]['text']='x'*11000
        self.rows[1]['text']='y'*2000
        self.discover()
        result=json.loads(self.session.read(['C1','C2']))
        self.assertEqual(result['not_loaded'],['C1'])
        self.assertEqual(result['chunks'][0]['text'],'y'*2000)
        again=json.loads(self.session.read(['C2']))
        self.assertEqual(again['already_loaded'],['C2'])
        self.assertEqual(again['chunks'],[])
        self.assertLessEqual(self.session.read_chars,MAX_READ_CHARS)

    def test_empty_final_selection_explicitly_allows_clarification_and_removes_document_citations(self):
        self.discover();self.session.read(['C1']);self.session.select(['C1'])
        self.session.select([])
        self.assertTrue(self.state['finalized'])
        self.assertEqual(self.citations,[])
        self.assertEqual(self.evidence,[])

    def test_agent_search_and_filtered_fetch_are_both_candidate_only(self):
        tools=self.tools()
        with patch('kb.retriever.search_folder',return_value=self.rows),patch('kb.provenance.annotate_results'):
            result=json.loads(tools['kb_search']('问题',kb_slug=self.folder.slug))
        self.assertEqual(result['kind'],'rag_candidates')
        self.assertEqual(self.citations,[])
        with patch('kb.retriever.fetch_doc',return_value=[self.rows[1]]),patch('kb.provenance.annotate_results'):
            fetched=json.loads(tools['kb_fetch_doc'](kb_slug=self.kbs[1].slug,contains='条件'))
        self.assertEqual(fetched['candidate_count'],1)
        self.assertNotIn('ONLY_FULL_TEXT',json.dumps(fetched))
        loaded=json.loads(tools['kb_read_chunks'](['C2']))
        self.assertEqual(loaded['chunks'][0]['source'],self.docs[1].original_name)
        tools['kb_select_evidence'](['C2'])
        self.assertEqual(len(self.citations),1)

    def test_middleware_reserves_final_selection_even_after_retrieval_round_limit(self):
        self.discover();self.session.read(['C1'])
        request=ModelRequest(model=None,messages=[HumanMessage(content='问题')],
            tools=[{'name':'kb_search'},{'name':'kb_select_evidence'}],system_message=SystemMessage(content='规则'))
        result=RetrievalBudget(max_rounds=0,selection=self.session).prepare(request)
        self.assertEqual(result.tools,[{'name':'kb_select_evidence'}])
        self.assertEqual(result.tool_choice['function']['name'],'kb_select_evidence')
        self.session.select(['C1'])
        self.assertEqual(RetrievalBudget(max_rounds=0,selection=self.session).prepare(request).tools,[])

    def test_context_caps_apply_across_multiple_discoveries(self):
        self.rows[0]['text']='检索内容'*2000
        for _ in range(5):
            text=self.session.discover(self.rows,[kb.slug for kb in self.kbs])
            self.assertLessEqual(len(text),MAX_RESPONSE_CHARS)
        self.assertLessEqual(self.session.discovery_chars,MAX_DISCOVERY_CHARS)

    def test_fallback_citation_view_shows_exact_selected_ids_not_every_similar_chunk(self):
        from django.urls import reverse
        self.client.force_login(self.user)
        rows=[{'chunk_id':'discarded','section':'一','text':'Shared words UNSELECTED_BODY'},
              {'chunk_id':'chosen','section':'二','text':'Shared words CHOSEN_BODY'}]
        with patch('kb.keyword_index.get_chunks',return_value=rows):
            response=self.client.get(reverse('kb:document_slices',args=[self.docs[0].pk]),{'h':'Shared words','chunk':'chosen'})
            self.assertEqual(response.status_code,200)
            self.assertContains(response,'CHOSEN_BODY')
            self.assertNotContains(response,'UNSELECTED_BODY')
            stale=self.client.get(reverse('kb:document_slices',args=[self.docs[0].pk]),{'chunk':'not-in-document'})
            self.assertEqual(stale.status_code,410)


class SelectionStreamTests(SimpleTestCase):
    def test_unselected_chunk_prose_is_not_published_through_tool_progress(self):
        state={}
        class FakeAgent:
            async def astream_events(self,*args,**kwargs):
                yield {'event':'on_chat_model_start','data':{}}
                yield {'event':'on_chat_model_stream','data':{'chunk':SimpleNamespace(content='UNSELECTED PRIVATE PROSE')}}
                yield {'event':'on_chat_model_end','data':{'output':SimpleNamespace(content='UNSELECTED PRIVATE PROSE',
                    tool_calls=[{'name':'kb_select_evidence','args':{'candidate_ids':['C1']}}],response_metadata={})}}
                state.update(finalized=True)
                yield {'event':'on_tool_end','name':'kb_select_evidence','data':{'output':'selected'}}
                yield {'event':'on_chat_model_start','data':{}}
                yield {'event':'on_chat_model_stream','data':{'chunk':SimpleNamespace(content='已选正文的回答')}}
                yield {'event':'on_chat_model_end','data':{'output':SimpleNamespace(content='已选正文的回答',tool_calls=[],response_metadata={})}}
        def build(*args,**kwargs):
            nonlocal state
            state=kwargs['retrieval_state'];state.update(used=True,finalized=False)
            return FakeAgent()
        async def collect():
            return [event async for event in run_agent_stream('参数?','progress','test',{'llm':{},'top_k':5})]
        with patch('kb.agent._build_agent',side_effect=build):events=asyncio.run(collect())
        self.assertNotIn('UNSELECTED PRIVATE PROSE',repr(events))
        self.assertTrue(any(kind=='token' and '已选正文' in data['text'] for kind,data in events))

    def test_unselected_candidate_answer_is_not_streamed_even_if_model_ignores_tool_choice(self):
        class FakeAgent:
            async def astream_events(self,*args,**kwargs):
                yield {'event':'on_chat_model_start','data':{}}
                yield {'event':'on_chat_model_stream','data':{'chunk':SimpleNamespace(content='UNSELECTED SECRET FACT')}}
                yield {'event':'on_chat_model_end','data':{'output':SimpleNamespace(
                    content='UNSELECTED SECRET FACT',tool_calls=[],response_metadata={'finish_reason':'stop'})}}
        def build(*args,**kwargs):
            kwargs['retrieval_state'].update(used=True,finalized=False)
            return FakeAgent()
        async def collect():
            return [event async for event in run_agent_stream('参数?','test','test',{'llm':{},'top_k':5})]
        with patch('kb.agent._build_agent',side_effect=build):events=asyncio.run(collect())
        self.assertNotIn('UNSELECTED SECRET FACT',repr(events))
        self.assertTrue(any(kind=='error' and data.get('code')=='evidence_not_selected' for kind,data in events))


class PlannedRagModel(FakeMessagesListChatModel):
    def bind_tools(self,tools,**kwargs):return self


class RagAgentExecutionTests(RagFixture, TransactionTestCase):
    def test_final_citation_snapshot_replaces_earlier_sources_in_saved_conversation(self):
        from django.urls import reverse
        from kb.models import Message
        self.client.force_login(self.user);self.async_client.cookies=self.client.cookies
        initial={'doc_id':str(self.docs[0].pk),'source':self.docs[0].original_name}
        chosen={'doc_id':str(self.docs[1].pk),'source':self.docs[1].original_name}
        for index, final in enumerate(([chosen],[])):
            async def events(*args,**kwargs):
                yield 'citations',{'citations':[initial],'replace':True}
                yield 'token',{'text':'回答内容'}
                yield 'citations',{'citations':final,'replace':True}
            async def collect():
                response=await self.async_client.post(reverse('kb:stream'),{
                    'message':'查询参数','thread_id':f'replace-selected-{index}','kb_slug':self.folder.slug})
                self.assertEqual(response.status_code,200)
                return [part async for part in response.streaming_content]
            with patch('kb.agent.run_agent_stream',side_effect=events),patch('kb.config.llm_settings',return_value={'qa_enhance':False}):
                asyncio.run(collect())
            answer=Message.objects.get(conversation__thread_id=f'replace-selected-{index}',role='ai')
            self.assertEqual(answer.citations,final)

    def test_real_agent_reads_only_the_chosen_document_then_confirms_final_citations(self):
        model=PlannedRagModel(responses=[
            AIMessage(content='',tool_calls=[{'id':'s','name':'kb_search','args':{'query':'参数','kb_slug':self.folder.slug}}]),
            AIMessage(content='',tool_calls=[{'id':'r','name':'kb_read_chunks','args':{'candidate_ids':['C2']}}]),
            AIMessage(content='',tool_calls=[{'id':'c','name':'kb_select_evidence','args':{'candidate_ids':['C2']}}]),
            AIMessage(content='根据第二份手册回答。')])
        with patch('kb.agent._get_llm',return_value=model),patch('kb.retriever.search_folder',return_value=self.rows),patch('kb.provenance.annotate_results'):
            agent=_build_agent(self.folder.slug,'planned-rag',{},5,None,citations=self.citations,evidence=self.evidence)
            result=agent.invoke({'messages':[{'role':'user','content':'查询第二份手册的参数'}]})
        tool_results=[m.content for m in result['messages'] if isinstance(m,ToolMessage)]
        self.assertNotIn('ONLY_FULL_TEXT_0',str(tool_results))
        self.assertIn('ONLY_FULL_TEXT_1',str(tool_results))
        self.assertEqual([c['doc_id'] for c in self.citations],[str(self.docs[1].pk)])
