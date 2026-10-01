from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from django.contrib.auth import get_user_model
from kb.models import KnowledgeBase, Document
from kb.views import _route_kb_by_question
from kb.manual_overview import is_manual_overview, select_overview
from kb.agent import _build_agent


class OverviewSelectionTests(SimpleTestCase):
    def test_summary_intent_does_not_steal_check_followups(self):
        for q in ['漂流手册有啥，给我总结一下', '漂流手册给我总结一下', 'manual overview']:
            self.assertTrue(is_manual_overview(q))
        for q in ['以上检查的频率整理成表格', '漂流手册检查频率总结', '总结以上内容']:
            self.assertFalse(is_manual_overview(q))

    def test_late_chapters_have_priority_over_front_matter(self):
        rows = [{'section':f'附录{i}', 'text':'front', 'chunk_id':str(i)} for i in range(30)]
        rows += [{'section':f'{i}第章主题', 'text':'complete condition and units', 'chunk_id':f'c{i}'} for i in range(1,15)]
        selected, headings = select_overview(rows, limit=15)
        self.assertEqual(len(headings),44)
        self.assertTrue(all(any(r['chunk_id']==f'c{i}' for r in selected) for i in range(1,15)))
        self.assertEqual([r['chunk_id'] for r in selected],['0']+[f'c{i}' for i in range(1,15)])

    def test_budget_never_cuts_a_condition(self):
        rows=[{'section':'第1章', 'text':'x'*20}, {'section':'第2章', 'text':'whole'}]
        selected,_=select_overview(rows,budget=10)
        self.assertEqual(selected,[rows[1]])

    def test_legacy_headings_with_removed_numbers_cover_late_chapters(self):
        rows = [{'section':f'前置项目{i}', 'text':'front'} for i in range(30)]
        rows += [{'section':'目', 'text':'目录'},
                 {'section':'第章维护与保养','text':'maintenance'},
                 {'section':'第章紧急特殊情况应急','text':'emergency'}]
        selected,_=select_overview(rows,limit=3)
        self.assertEqual(selected,rows[-3:])


class ManualOverviewTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('overview',is_staff=True)
        self.kb=KnowledgeBase.objects.create(name='FL-8B漂流 使用说明书 PRINT.pdf',slug='raft')
        self.wrong=KnowledgeBase.objects.create(name='SDLDWARTP0001_CN_L.pdf',slug='wrong')
        self.doc=Document.objects.create(kb=self.kb,original_name=self.kb.name,status='completed',md_content='manual')

    def test_chinese_device_inside_mixed_filename_routes(self):
        for q in ['漂流手册有啥，给我总结一下','漂流手册给我总结一下']:
            self.assertEqual(_route_kb_by_question(q,self.user),self.kb)

    def test_fallback_copies_titles_without_ocr_parameters_and_rejects_changes(self):
        from kb.manual_overview import outline_answer
        from kb.publication import digest
        self.doc.md_content='## 1第章概述\n8 条（OCR）\n## 13第章紧急特殊情况应急预案\n每周（未经核对）'
        self.doc.save()
        evidence=[{'doc_id':str(self.doc.pk),'document_digest':digest(self.doc.md_content)}]
        answer=outline_answer(evidence)
        self.assertIn('13第章紧急特殊情况应急预案',answer)
        self.assertNotIn('8 条',answer)
        self.doc.md_content='changed';self.doc.save()
        self.assertEqual(outline_answer(evidence),'')

    def test_ambiguous_subject_does_not_choose_arbitrarily(self):
        KnowledgeBase.objects.create(name='FL-10漂流说明书.pdf',slug='raft-other')
        self.assertIsNone(_route_kb_by_question('漂流手册有什么',self.user))

    def test_preloaded_summary_uses_selected_library_and_collects_evidence(self):
        citations,evidence=[],[]
        rows=[{'section':'第1章概述','text':'混凝土水道','chunk_id':'c1'},
              {'section':'第13章应急预案','text':'应急流程','chunk_id':'c13'}]
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'), \
             patch('kb.keyword_index.get_chunks',return_value=rows) as fetch, \
             patch('kb.provenance.annotate_results'):
            _build_agent('raft','turn',{},5,None,citations=citations,evidence=evidence,
                         allowed_kb_slugs=['raft'],overview=True)
        fetch.assert_called_once_with('raft',self.doc.original_name)
        prompt=create.call_args.kwargs['system_prompt']
        self.assertEqual(create.call_args.kwargs['tools'],[])
        self.assertIn('第13章应急预案',prompt)
        self.assertIn('不得把分别适用于不同对象的上限',prompt)
        self.assertEqual({e['kb_slug'] for e in evidence},{'raft'})
        self.assertEqual(citations[0]['doc_id'],str(self.doc.pk))
