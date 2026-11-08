from __future__ import annotations

"""追踪表预览/过滤/全量汇总、输出预算与发布复核回归。"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from kb.agent import _build_agent
from kb.models import KbTracker, KnowledgeBase, TrackerRow
from kb.publication import citation_key, validate_sources, visible_citations
from kb.tracker_query import RESPONSE_CHARS, TURN_CHARS, TrackerQuery
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage


class PlannedTrackerModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class TrackerFixture:
    def setUp(self):
        self.user = get_user_model().objects.create_user('tracker-reader')
        self.kb = KnowledgeBase.objects.create(name='运营历史', slug='tracker-history', category='business', is_folder=True)
        self.tracker = KbTracker.objects.create(kb=self.kb, enabled=True,
            fields=[{'label':key} for key in ('项目名称','采集日期','场次数','持续时长','原文','开放')])
        self.service = TrackerQuery(lambda kb:kb.department=='通用')

    def row(self, **values):
        return TrackerRow.objects.create(tracker=self.tracker, status='done', values={
            '项目名称':'海盗演出', '采集日期':'2025-11-06', '场次数':3, '持续时长':'8.5', '开放':False, **values})


class TrackerQueryTests(TrackerFixture, TestCase):
    def lookup(self, **params):
        return json.loads(self.service.call('lookup', kb_slug=self.kb.slug, **params))

    def test_preview_has_actual_schema_small_examples_and_counts(self):
        self.row(原文='文'*7900)
        result = json.loads(self.service.call('preview', kb_slug=self.kb.slug))
        self.assertEqual(result['tables'][0]['row_count'],1)
        self.assertEqual(result['tables'][0]['completed_rows'],1)
        fields = {f['name']:f for f in result['tables'][0]['fields']}
        self.assertEqual(fields['场次数']['sample_types'],['int'])
        self.assertTrue(result['tables'][0]['samples'][0]['values']['原文']['truncated'])
        self.assertLess(len(json.dumps(result,ensure_ascii=False)),3000)
        self.assertEqual(self.service.evidence,[])

    def test_full_history_unicode_search_and_filters_happen_before_pagination(self):
        old = self.row(项目名称='仅历史记录中有的项目',采集日期='2024-11-06')
        TrackerRow.objects.filter(pk=old.pk).update(updated_at=timezone.now()-timedelta(days=365))
        TrackerRow.objects.bulk_create([TrackerRow(tracker=self.tracker,status='done',values={
            '项目名称':'近期项目','采集日期':'2026-11-06','场次数':9}) for _ in range(210)])
        result = self.lookup(query='仅历史',columns=['项目名称','采集日期'])
        self.assertEqual(result['total_matched'],1)
        self.assertEqual(result['rows'][0]['row_id'],old.pk)
        self.assertNotIn('场次数', result['rows'][0]['values'])
        result = self.lookup(filters=[{'field':'采集日期','op':'between','value':['2024-01-01','2024-12-31'],'type':'date'}])
        self.assertEqual(result['total_matched'],1)

    def test_numeric_strings_numeric_sorting_and_zero_false_are_preserved(self):
        self.row(场次数='12')
        self.row(场次数='3')
        self.row(场次数='未营业')
        self.row(场次数=0)
        result = self.lookup(columns=['场次数','开放'],filters=[{'field':'场次数','op':'gte','value':0,'type':'number'}],
            order_by=[{'field':'场次数','type':'number','direction':'asc'}])
        self.assertEqual(result['total_matched'],3)
        self.assertEqual([r['values']['场次数'] for r in result['rows']],[0,'3','12'])
        self.assertIs(result['rows'][0]['values']['开放'],False)

    def test_pagination_has_exact_total_and_deterministic_tie_break(self):
        for i in range(7):self.row(场次数=i)
        first = self.lookup(columns=['场次数'],limit=3)
        second = self.lookup(columns=['场次数'],limit=3,offset=first['next_offset'])
        self.assertEqual(first['total_matched'],7)
        self.assertEqual(first['next_offset'],3)
        self.assertTrue(first['has_more'])
        self.assertFalse({r['row_id'] for r in first['rows']} & {r['row_id'] for r in second['rows']})

    def test_boolean_null_empty_and_literal_json_keys_filter_correctly(self):
        self.row(场次数=None)
        self.row(场次数='null',开放=True)
        self.row(场次数='')
        self.assertEqual(self.lookup(filters=[{'field':'场次数','op':'is_empty','value':True}])['total_matched'],2)
        self.assertEqual(self.lookup(filters=[{'field':'场次数','op':'eq','value':'null'}])['total_matched'],1)
        self.assertEqual(self.lookup(filters=[{'field':'开放','op':'eq','value':False}])['total_matched'],2)
        self.tracker.fields += [{'label':name} for name in ('3','a.b','a__b','a"b')]
        self.tracker.save(update_fields=['fields'])
        self.row(**{name:'literal-key-match' for name in ('3','a.b','a__b','a"b')})
        for name in ('3','a.b','a__b','a"b'):
            result=self.lookup(filters=[{'field':name,'op':'eq','value':'literal-key-match'}])
            self.assertEqual(result['total_matched'],1,result)

    def test_long_cell_can_be_retrieved_by_row_id_with_a_larger_explicit_cell_budget(self):
        row=self.row(原文='内容'*3900)
        result=self.lookup(columns=['原文'],filters=[{'field':'$row_id','op':'eq','value':row.pk}],cell_chars=8000)
        self.assertEqual(result['total_matched'],1)
        self.assertEqual(result['rows'][0]['values']['原文'],'内容'*3900)

    def test_stats_cover_all_rows_and_report_non_numeric_exclusions(self):
        for value in (3,'4','未提供',None):self.row(场次数=value)
        result = self.lookup(limit=1,metrics=[{'op':'count','as':'条数'},{'op':'sum','field':'场次数','as':'总场次'},
            {'op':'avg','field':'场次数','as':'平均场次'},{'op':'min','field':'场次数','as':'最少'},
            {'op':'max','field':'场次数','as':'最多'},{'op':'distinct_count','field':'项目名称','as':'项目数'}])
        self.assertEqual(result['total_matched'],4)
        self.assertEqual(result['groups'][0]['metrics'],{'条数':4,'总场次':7,'平均场次':3.5,'最少':3,'最多':4,'项目数':1})
        self.assertEqual(result['groups'][0]['valid_numeric_values']['总场次'],2)

    def test_year_and_month_groups_support_history_comparison_with_group_pagination(self):
        self.row(采集日期='2024-11-06',场次数=2)
        self.row(采集日期='2025-11-06',场次数=3)
        self.row(采集日期='2025-11-07',场次数=4)
        metrics = [{'op':'count','as':'记录数'},{'op':'sum','field':'场次数','as':'场次'}]
        first = self.lookup(metrics=metrics,group_by=[{'field':'采集日期','period':'year'}],limit=1)
        self.assertEqual(first['total_matched'],3)
        self.assertEqual(first['total_groups'],2)
        self.assertEqual(first['groups'][0]['by'],{'采集日期':'2024'})
        second = self.lookup(metrics=metrics,group_by=[{'field':'采集日期','period':'year'}],offset=1)
        self.assertEqual(second['groups'][0]['metrics'],{'记录数':2,'场次':7})
        monthly = self.lookup(metrics=metrics,group_by=[{'field':'采集日期','period':'month'}])
        self.assertEqual(monthly['groups'][1]['by'],{'采集日期':'2025-11'})

    def test_empty_stats_are_not_fabricated_numbers(self):
        result = self.lookup(metrics=[{'op':'count'},{'op':'avg','field':'场次数'}])
        self.assertEqual(result['total_matched'],0)
        self.assertEqual(result['groups'][0]['metrics'],{'count':0,'avg':None})

    def test_default_does_not_mix_proposals_with_approved_records(self):
        self.row()
        TrackerRow.objects.create(tracker=self.tracker,status='proposed',values={'场次数':2},proposed_values={'场次数':99})
        self.assertEqual(self.lookup()['total_matched'],1)
        self.assertEqual(self.lookup(status='all')['total_matched'],2)

    def test_parent_fallback_scope_and_private_tables_do_not_leak(self):
        child = KnowledgeBase.objects.create(name='手册',slug='tracker-child',parent=self.kb)
        self.row()
        self.assertEqual(json.loads(self.service.call('lookup',kb_slug=child.slug))['total_matched'],1)
        KbTracker.objects.create(kb=child,fields=[])
        self.assertEqual(json.loads(self.service.call('lookup',kb_slug=child.slug))['total_matched'],1)
        private = KnowledgeBase.objects.create(name='secret-name',slug='tracker-private',department='机械')
        tracker = KbTracker.objects.create(kb=private,fields=[{'label':'secret-field'}])
        TrackerRow.objects.create(tracker=tracker,status='done',values={'secret-field':'secret-value'})
        for method in ('preview','lookup'):
            result = self.service.call(method,kb_slug=private.slug)
            self.assertIn('不存在',result)
            self.assertNotIn('secret-name',result)
            self.assertNotIn('secret-value',result)

    def test_response_and_shared_turn_budgets_do_not_depend_on_row_count(self):
        for _ in range(20):self.row(原文='原文'*3900)
        outputs = []; chars = 0
        for _ in range(12):
            text = self.service.call('lookup',kb_slug=self.kb.slug,columns=['原文'],limit=100)
            self.assertLessEqual(len(text),RESPONSE_CHARS)
            chars += len(text)
            outputs.append(json.loads(text))
            if outputs[-1].get('budget_exhausted'):break
        self.assertTrue(outputs[0]['rows'][0]['values']['原文']['truncated'])
        self.assertTrue(outputs[0]['has_more'])
        self.assertTrue(outputs[-1]['budget_exhausted'])
        self.assertLessEqual(chars,TURN_CHARS)
        self.assertLessEqual(sum(len(ev['text']) for ev in self.service.evidence),TURN_CHARS)

    def test_bad_fields_and_operators_cannot_turn_into_sql_or_unbounded_requests(self):
        for params in ({'columns':['values__password']},{'limit':1000},{'offset':-1},
            {'filters':[{'field':'场次数','op':'raw_sql','value':'1 OR 1=1'}]},
            {'filters':[{'field':'场次数','op':'gte','type':'number','value':'NaN'}]},
            {'filters':[{'field':'采集日期','op':'eq','type':'date','value':'2026-99-99'}]}):
            with self.subTest(params=params):self.assertIn('error',self.lookup(**params))

    def test_snapshots_replay_results_and_revoke_changed_data_and_access(self):
        row = self.row()
        self.lookup(metrics=[{'op':'sum','field':'场次数'}])
        evidence = self.service.evidence
        self.assertTrue(validate_sources(evidence,self.user.pk))
        citation = {'kind':'tracker','kb_slug':self.kb.slug,'result_digest':evidence[0]['result_digest'],
                    'tracker_snapshot':evidence[0]}
        self.assertTrue(visible_citations([citation],self.user))
        self.assertEqual(citation_key(citation),('tracker',evidence[0]['result_digest']))
        TrackerRow.objects.filter(pk=row.pk).update(values={**row.values,'场次数':10})
        self.assertFalse(validate_sources(evidence,self.user.pk))
        self.assertFalse(visible_citations([citation],self.user))
        TrackerRow.objects.filter(pk=row.pk).update(values=row.values)
        self.assertTrue(validate_sources(evidence,self.user.pk))
        self.kb.department='机械';self.kb.save(update_fields=['department'])
        self.assertFalse(validate_sources(evidence,self.user.pk))

    def test_agent_exposes_both_tools_with_filtered_schema_and_publishes_tracker_evidence(self):
        self.row()
        evidence=[];citations=[]
        with patch('langchain.agents.create_agent') as create,patch('kb.agent._get_llm'):
            _build_agent(self.kb.slug,'tracker-agent',{},5,None,evidence=evidence,citations=citations)
        tools={tool.__name__:tool for tool in create.call_args.kwargs['tools']}
        self.assertIn('tracker_preview',tools)
        from langchain_core.utils.function_calling import convert_to_openai_tool
        schema=convert_to_openai_tool(tools['tracker_lookup'])['function']['parameters']
        for param in ('columns','filters','metrics','group_by','offset'):self.assertIn(param,schema['properties'])
        filters = schema['properties']['filters']['anyOf'][0]['items']
        self.assertIn('op', filters['required'])
        self.assertIn('between', filters['properties']['op']['enum'])
        metric = schema['properties']['metrics']['anyOf'][0]['items']
        self.assertIn('as', metric['properties'])
        result=json.loads(tools['tracker_lookup'](kb_slug=self.kb.slug,metrics=[{'op':'count'}]))
        self.assertEqual(result['groups'][0]['metrics']['count'],1)
        self.assertEqual(evidence[0]['kind'],'tracker')
        self.assertEqual(citations[0]['kind'],'tracker')


class TrackerAgentExecutionTests(TrackerFixture, TransactionTestCase):
    def test_parallel_tool_calls_share_one_hard_character_budget(self):
        for _ in range(20):self.row(原文='原文'*3900)
        def lookup(_):
            return self.service.call('lookup',kb_slug=self.kb.slug,columns=['原文'],limit=100)
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses=list(executor.map(lookup,range(8)))
        self.assertLessEqual(sum(map(len,responses)),TURN_CHARS)
        self.assertTrue(any(json.loads(text).get('budget_exhausted') for text in responses if text))

    def test_real_agent_executes_preview_then_a_filtered_database_summary(self):
        self.row(采集日期='2024-11-06',场次数=99)
        self.row(采集日期='2025-11-06',场次数=3)
        self.row(采集日期='2025-11-07',场次数=4)
        model=PlannedTrackerModel(responses=[
            AIMessage(content='',tool_calls=[{'id':'preview','name':'tracker_preview','args':{'kb_slug':self.kb.slug}}]),
            AIMessage(content='',tool_calls=[{'id':'lookup','name':'tracker_lookup','args':{
                'kb_slug':self.kb.slug,
                'filters':[{'field':'采集日期','op':'between','type':'date','value':['2025-01-01','2025-12-31']}],
                'metrics':[{'op':'sum','field':'场次数','as':'总场次'}]}}]),
            AIMessage(content='2025 年总场次为 7。')])
        evidence=[]
        with patch('kb.agent._get_llm',return_value=model):
            agent=_build_agent(self.kb.slug,'planned-tracker',{},5,None,evidence=evidence)
            result=agent.invoke({'messages':[{'role':'user','content':'统计2025年场次数'}]})
        tools=[m for m in result['messages'] if isinstance(m,ToolMessage)]
        self.assertEqual(len(tools),2)
        self.assertEqual(json.loads(tools[0].content)['kind'],'tracker_preview')
        lookup=json.loads(tools[1].content)
        self.assertEqual(lookup['total_matched'],2)
        self.assertEqual(lookup['groups'][0]['metrics']['总场次'],7)
        self.assertEqual(evidence[0]['query']['filters'][0]['value'],['2025-01-01','2025-12-31'])
        self.assertTrue(validate_sources(evidence,self.user.pk))
