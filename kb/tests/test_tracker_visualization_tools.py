"""The six data views must be actual normal/enhanced chat tools with citations."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from kb.agent import _build_agent
from kb.models import KbTracker, KnowledgeBase, TrackerRow
from kb.publication import validate_sources, visible_citations


class TrackerVisualizationToolTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('visual-tool-reader', is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='Operations', slug='visual-tool-operations',
            category='business', is_folder=True, active_fields=['Name', 'Count', 'Date', 'Start', 'End'])
        tracker = KbTracker.objects.create(kb=self.kb, fields=[{'label': name} for name in
            ['Name', 'Count', 'Date', 'Start', 'End', 'Hidden']])
        TrackerRow.objects.create(tracker=tracker, status='done', values={'Name': 'Inspection', 'Count': 7,
            'Date': '2026-11-01', 'Start': '2026-11-01T08:00:00+08:00',
            'End': '2026-11-01T10:00:00+08:00', 'Hidden': 'HIDDEN_SENTINEL'})

    def tools(self, enhanced):
        citations, evidence = [], []
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'):
            _build_agent('', 'visual-tool-test', {'qa_enhance': enhanced}, 5, None,
                citations=citations, evidence=evidence, relationship_user_id=self.user.pk)
        tools = {tool.__name__: tool for tool in create.call_args.kwargs['tools']}
        return tools, citations, evidence

    def test_all_six_visuals_publish_real_permission_checked_citations_in_both_chat_modes(self):
        cases = [dict(kind='progress', operation='sum', measure='Count', target=10, unit='min'),
            dict(kind='sparkline', operation='sum', measure='Count', x_field='Date'),
            dict(kind='heatmap', x_field='Name', y_field='Date'),
            dict(kind='pareto', operation='sum', measure='Count', x_field='Name'),
            dict(kind='timeline', label_field='Name', start_field='Start', end_field='End'),
            dict(kind='table', columns=['Name', 'Count'])]
        for enhanced in (False, True):
            for params in cases:
                with self.subTest(enhanced=enhanced, kind=params['kind']):
                    tools, citations, evidence = self.tools(enhanced)
                    self.assertIn('tracker_visualize', tools)
                    self.assertEqual('write_analysis' in tools, not enhanced)
                    output = json.loads(tools['tracker_visualize'](kb_slug=self.kb.slug, **params))
                    self.assertNotIn('error', output)
                    self.assertEqual(output['visualization'], params['kind'])
                    self.assertEqual(citations[0]['tracker_snapshot']['chart']['type'], params['kind'])
                    self.assertTrue(validate_sources(evidence, self.user.pk))
                    self.assertTrue(visible_citations(citations, self.user))
                    self.assertNotIn('HIDDEN_SENTINEL', repr(output))

    def test_invalid_or_hidden_inputs_return_an_error_without_publishing_a_visual(self):
        for params in (dict(kind='progress', target=0), dict(kind='table', columns=['Hidden']),
                       dict(kind='timeline', label_field='Name', start_field='Hidden', end_field='End')):
            tools, citations, evidence = self.tools(False)
            result = json.loads(tools['tracker_visualize'](kb_slug=self.kb.slug, **params))
            self.assertIn('error', result)
            self.assertEqual(citations, [])
            self.assertEqual(evidence, [])

    def test_visualization_function_exposes_a_valid_llm_tool_schema_and_executes_typed_arguments(self):
        from langchain_core.tools import StructuredTool
        tools, citations, _ = self.tools(True)
        tool = StructuredTool.from_function(tools['tracker_visualize'])
        schema = tool.args_schema.model_json_schema()
        self.assertEqual(set(schema['properties']['kind']['enum']),
            {'progress', 'sparkline', 'heatmap', 'pareto', 'timeline', 'table'})
        output = json.loads(tool.invoke({'kb_slug': self.kb.slug, 'kind': 'progress',
            'operation': 'sum', 'measure': 'Count', 'target': 10, 'unit': 'min'}))
        self.assertEqual(output['value'], 7)
        self.assertEqual(citations[0]['tracker_snapshot']['chart']['target'], 10)

    def test_same_sized_table_queries_keep_distinct_visuals_and_evidence(self):
        TrackerRow.objects.create(tracker=self.kb.tracker, status='done', values={'Name': 'Other', 'Count': 3})
        tools, citations, evidence = self.tools(False)
        for name in ('Inspection', 'Other'):
            result = json.loads(tools['tracker_visualize'](kb_slug=self.kb.slug, kind='table',
                columns=['Name'], filters=[{'field': 'Name', 'op': 'eq', 'value': name}]))
            self.assertNotIn('error', result)
        self.assertEqual(len(citations), 2)
        self.assertNotEqual(citations[0]['result_digest'], citations[1]['result_digest'])
        self.assertEqual({citation['tracker_snapshot']['chart']['rows'][0]['Name'] for citation in citations},
                         {'Inspection', 'Other'})
        self.assertTrue(validate_sources(evidence, self.user.pk))
