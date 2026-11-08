"""Image results must survive evidence selection without publishing unread previews."""
import asyncio
import base64
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TransactionTestCase
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from kb import access
from kb.agent import _build_agent, run_agent_stream
from kb.agent_budget import RetrievalBudget
from kb.history import load_context
from kb.models import Conversation, Document, KnowledgeBase
from kb.publication import image_source_digest, validate_sources, visible_citations
from kb.rag_selection import RagSelection
from kb.tests.test_conversation_context import ConversationModel, call

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/wCEAAAAASUVORK5CYII=')


class ImageEvidenceFlowTests(TransactionTestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        override=self.settings(DATA_DIR=self.root,MEDIA_ROOT=self.root);override.enable();self.addCleanup(override.disable)
        self.user=get_user_model().objects.create_user('image-evidence-reader')
        self.kb=KnowledgeBase.objects.create(name='Picture manual',slug='picture-manual')
        self.doc=Document.objects.create(kb=self.kb,original_name='Manual.pdf',status='completed',md_content='Caption: bearing diagram')
        self.image='a'*64+'.png'
        directory=self.root/'doc_images'/str(self.doc.pk);directory.mkdir(parents=True)
        self.path=directory/self.image;self.path.write_bytes(PNG)
        self.picture={'type':'image','text':'Bearing diagram','source':self.doc.original_name,'doc_id':str(self.doc.pk),
            'kb_slug':self.kb.slug,'image':self.image,'chunk_id':'image-chunk','page_label':'第1页','distance':0.3}
        self.text={'type':'text','text':'TEXT_CANDIDATE_NOT_READ','source':self.doc.original_name,'doc_id':str(self.doc.pk),
            'kb_slug':self.kb.slug,'chunk_id':'text-chunk'}

    def execute(self,responses,*,vision=False,enhanced=False,images=None):
        model=ConversationModel(responses=responses)
        config={'llm':{'vision':vision,'qa_enhance':enhanced},'top_k':5,'user_id':self.user.pk}
        async def consume():
            return [event async for event in run_agent_stream('Find a bearing diagram.','image-flow','',config)]
        with patch('kb.agent._get_llm',return_value=model), \
             patch('kb.pipeline._embeddings',return_value=SimpleNamespace(embed_query=lambda q:[1,0])), \
             patch('kb.retriever._image_lane',return_value=[self.picture] if images is None else images), \
             patch('kb.retriever.search',return_value=[self.text]),patch('kb.provenance.annotate_results'), \
             patch('kb.qa_steps.plan_query',new_callable=AsyncMock,return_value=(None,{'input_tokens':0,'output_tokens':0})), \
             patch('kb.qa_steps.verify_answer',new_callable=AsyncMock,return_value=({'verdict':'pass'},{'input_tokens':0,'output_tokens':0})) as verify, \
             patch('kb.agent._judge_candidate',return_value={'match':True,'caption':'Bearing diagram','confidence':99}) as judge:
            events=asyncio.run(consume())
        return events,config,model,verify,judge

    def test_mixed_text_preview_and_image_search_recovers_a_skipped_selection(self):
        events,config,model,_,_=self.execute([
            call('kb_search',{'query':'bearing','kb_slug':self.kb.slug},'text-search'),
            call('kb_image_search',{'query':'bearing diagram','kb_slug':self.kb.slug},'image-search'),
            AIMessage(content='PREMATURE_UNSELECTED_DRAFT'),
            call('kb_select_evidence',{'candidate_ids':['C2']},'select-image'),
            AIMessage(content='Here is an unverified image candidate from Manual.pdf.')])
        self.assertFalse(any(kind=='error' for kind,_ in events),events)
        self.assertNotIn('PREMATURE_UNSELECTED_DRAFT',repr(events))
        self.assertNotIn('PREMATURE_UNSELECTED_DRAFT',repr(config['next_context']['messages']))
        self.assertTrue(any(kind=='token' and 'Here is' in data['text'] for kind,data in events))
        selected=config['next_context']['evidence']
        self.assertEqual(len(selected),1)
        self.assertEqual(selected[0]['kind'],'image')
        self.assertFalse(selected[0]['visual_confirmed'])
        self.assertTrue(validate_sources(selected,self.user.pk))
        citation=config['next_context']['citations'][0]
        self.assertEqual(citation['images'],[f'/kb/doc/{self.doc.pk}/img/{self.image}'])
        self.assertTrue(visible_citations([citation],self.user))
        self.assertEqual(len(model.seen),5)

    def test_vision_images_are_registered_and_selected_without_reading_text_candidates(self):
        events,config,model,_,judge=self.execute([
            call('kb_image_search',{'query':'bearing diagram','kb_slug':self.kb.slug},'image'),
            call('kb_select_evidence',{'candidate_ids':['C1']},'select'),
            AIMessage(content='The matched bearing diagram is attached.')],vision=True)
        self.assertFalse(any(kind=='error' for kind,_ in events),events)
        self.assertTrue(config['next_context']['evidence'][0]['visual_confirmed'])
        judge.assert_called_once()
        tool_results=[message for message in model.seen[-1] if isinstance(message,ToolMessage)]
        self.assertTrue(any(isinstance(message.content,list) and any(block.get('type')=='image' for block in message.content) for message in tool_results))

    def test_enhanced_answer_accepts_validated_image_sources(self):
        events,config,_,verify,_=self.execute([
            call('kb_image_search',{'query':'bearing diagram','kb_slug':self.kb.slug},'image'),
            call('kb_select_evidence',{'candidate_ids':['C1']},'select'),
            AIMessage(content='The image is from Manual.pdf.')],enhanced=True)
        self.assertFalse(any(kind=='error' for kind,_ in events),events)
        self.assertTrue(any(kind=='verify' and data['ok'] for kind,data in events))
        verify.assert_awaited_once()
        self.assertEqual(verify.call_args.args[3][0]['kind'],'image')
        self.assertIn('next_context',config)

    def test_no_image_result_can_clarify_after_discarding_unread_text_candidates(self):
        events,config,_,_,_=self.execute([
            call('kb_search',{'query':'bearing','kb_slug':self.kb.slug},'text'),
            call('kb_image_search',{'query':'bearing','kb_slug':self.kb.slug},'images'),
            AIMessage(content='PREMATURE'),
            call('kb_select_evidence',{'candidate_ids':[]},'no-evidence'),
            AIMessage(content='No image candidates were found. Can you describe the diagram more specifically?')],images=[])
        self.assertFalse(any(kind=='error' for kind,_ in events),events)
        self.assertEqual(config['next_context']['citations'],[])

    def test_second_ignored_selection_still_blocks_and_does_not_save_a_draft(self):
        events,config,model,_,_=self.execute([
            call('kb_image_search',{'query':'bearing','kb_slug':self.kb.slug},'image'),
            AIMessage(content='FIRST_UNSELECTED'),AIMessage(content='SECOND_UNSELECTED')])
        self.assertTrue(any(kind=='error' and data.get('code')=='evidence_not_selected' for kind,data in events))
        self.assertNotIn('UNSELECTED',repr(events))
        self.assertNotIn('next_context',config)
        self.assertEqual(len(model.seen),3)

    def session(self):
        evidence=[];citations=[]
        selection=RagSelection(lambda kb:access.kb_accessible(self.user,kb),evidence,citations)
        selection.discover([self.picture],[self.kb.slug])
        selection.read(['C1'],include_images=False)
        return selection,evidence,citations

    def test_replaced_or_deleted_image_is_rejected_before_selection_and_history_reuse(self):
        selection,evidence,citations=self.session()
        selection.select(['C1'])
        self.assertTrue(validate_sources(evidence,self.user.pk))
        self.assertTrue(visible_citations(citations,self.user))
        conv=Conversation.objects.create(user=self.user,thread_id='image-memory',agent_context={
            'version':1,'messages':[],'rag':selection.snapshot(),'citations':citations,'evidence':evidence})
        self.assertTrue(load_context(conv,self.user))
        self.path.write_bytes(b'changed image')
        self.assertFalse(validate_sources(evidence,self.user.pk))
        self.assertFalse(visible_citations(citations,self.user))
        self.assertEqual(load_context(conv,self.user),{})
        self.assertIn('error',json.loads(selection.select(['C1'])))
        self.path.unlink()
        self.assertFalse(validate_sources(evidence,self.user.pk))

    def test_image_permissions_are_rechecked_and_foreign_lane_rows_are_not_reviewed(self):
        other=get_user_model().objects.create_user('image-other')
        private=KnowledgeBase.objects.create(name='Private',slug='private-image',visibility='individual',created_by=other)
        foreign=Document.objects.create(kb=private,original_name='Secret.pdf',status='completed')
        events,config,_,_,judge=self.execute([
            call('kb_image_search',{'query':'bearing','kb_slug':self.kb.slug},'image'),
            AIMessage(content='No accessible image results were found.')],vision=True,
            images=[dict(self.picture,doc_id=str(foreign.pk),source='SECRET_SOURCE')])
        judge.assert_not_called()
        self.assertNotIn('SECRET_SOURCE',repr(config.get('next_context')))
        self.assertFalse(any(kind=='error' for kind,_ in events))

    def test_changed_image_is_blocked_before_the_first_answer_token(self):
        selection,evidence,citations=self.session();selection.select(['C1'])
        self.path.write_bytes(b'changed before release')
        class Agent:
            async def astream_events(agent,*args,**kwargs):
                yield {'event':'on_chat_model_start','data':{}}
                yield {'event':'on_chat_model_stream','data':{'chunk':SimpleNamespace(content='STALE_IMAGE_ANSWER')}}
                yield {'event':'on_chat_model_end','data':{'output':AIMessage(content='STALE_IMAGE_ANSWER')}}
        def build(*args,**kwargs):
            kwargs['evidence'].extend(evidence);kwargs['citations'].extend(citations)
            return Agent()
        async def consume():
            return [event async for event in run_agent_stream('Find an image','changed-image','',{'llm':{},'top_k':5,'user_id':self.user.pk})]
        with patch('kb.agent._build_agent',side_effect=build):events=asyncio.run(consume())
        self.assertNotIn('STALE_IMAGE_ANSWER',repr(events))
        self.assertTrue(any(kind=='error' and data.get('code')=='source_changed' for kind,data in events))

    def test_pdf_page_source_fingerprint_changes_with_the_source_file(self):
        pdf=self.root/'source.pdf';pdf.write_bytes(b'original pdf')
        self.doc.file.name="source.pdf"
        first=image_source_digest(self.doc,'page1')
        self.assertIsNotNone(first)
        pdf.write_bytes(b'changed pdf')
        self.assertNotEqual(image_source_digest(self.doc,'page1'),first)
        self.assertIsNone(image_source_digest(self.doc,'../secret.txt'))

    def test_enhanced_verifier_receives_the_actual_selected_image(self):
        selection,evidence,_=self.session();selection.select(['C1'])
        from kb.qa_steps import verify_answer
        llm=SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content='{"verdict":"pass","issues":[]}',response_metadata={})))
        with patch('kb.qa_steps.build_llm',return_value=llm):
            verdict,_=asyncio.run(verify_answer({'vision':True},'Find a diagram','This is a diagram.',evidence))
        self.assertEqual(verdict['verdict'],'pass')
        content=llm.ainvoke.call_args.args[0][0]['content']
        self.assertTrue(any(block['type']=='image_url' for block in content))
        self.assertIn('未经视觉确认',content[0]['text'])


class BoundedSelectionRecoveryTests(SimpleTestCase):
    def test_sync_and_async_recovery_are_bounded_and_do_not_add_rejected_draft_to_history(self):
        request=ModelRequest(model=None,messages=[HumanMessage(content='Find an image')],
            tools=[{'name':'kb_select_evidence'},{'name':'kb_read_chunks'},{'name':'kb_search'}],system_message=SystemMessage(content='original rules'))
        selection=SimpleNamespace(state={'used':True,'finalized':False},pending=True,loaded={'C2'},presented={'C1','C2'})
        for asynchronous in (False,True):
            middleware=RetrievalBudget(selection=selection)
            draft=ModelResponse(result=[AIMessage(content='unselected draft')])
            handler=AsyncMock(return_value=draft) if asynchronous else Mock(return_value=draft)
            if asynchronous:asyncio.run(middleware.awrap_model_call(request,handler))
            else:middleware.wrap_model_call(request,handler)
            self.assertEqual(handler.call_count,2)
            recovery=handler.call_args.args[0]
            self.assertEqual(recovery.tools,[{'name':'kb_select_evidence'}])
            self.assertEqual(recovery.messages,request.messages)
            self.assertNotIn('unselected draft',str(recovery.messages))
            self.assertIsNone(middleware.recovery_request(request,draft))

