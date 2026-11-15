"""Single-value chat answers use the actual permission-checked query or passage."""
import copy
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from kb.agent import _build_agent
from kb.models import Document, KbTracker, KnowledgeBase, TrackerRow
from kb.publication import validate_sources, visible_citations
from kb.tracker_query import TrackerQuery, validate_snapshot


class ValueCardFixture:
    def setUp(self):
        self.user = get_user_model().objects.create_user('value-card-reader', is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='Weather', slug='value-card-weather',
            category='business', is_folder=True, active_fields=['Temperature', 'Date', 'Location'])
        self.tracker = KbTracker.objects.create(kb=self.kb, fields=[{'label': name} for name in
            ['Temperature', 'Date', 'Location', 'Hidden']])
        self.row = TrackerRow.objects.create(tracker=self.tracker, status='done',
            values={'Temperature': 0, 'Date': '2026-11-13', 'Location': 'Shanghai', 'Hidden': 123})

    def tools(self, enhanced=False):
        citations, evidence = [], []
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'):
            _build_agent('', 'value-card-test', {'qa_enhance': enhanced}, 5, None,
                citations=citations, evidence=evidence, relationship_user_id=self.user.pk)
        return ({tool.__name__: tool for tool in create.call_args.kwargs['tools']},
                citations, evidence, create.call_args.kwargs['system_prompt'])


class ValueCardTests(ValueCardFixture, TestCase):
    def test_lookup_defaults_to_a_verified_card_in_both_modes(self):
        for enhanced in (False, True):
            tools, citations, evidence, prompt = self.tools(enhanced)
            result = json.loads(tools['tracker_lookup'](kb_slug=self.kb.slug,
                columns=['Temperature'], card_title='Shanghai temperature', unit='°C'))
            self.assertEqual(result['chart'], dict(type='metric', title='Shanghai temperature', value=0, unit='°C'))
            self.assertEqual(citations[0]['tracker_snapshot']['chart'], result['chart'])
            self.assertTrue(validate_sources(evidence, self.user.pk))
            self.assertTrue(visible_citations(citations, self.user))
            self.assertIn('卡片和真实引用就是完整回答', prompt)

    def test_latest_value_requires_deliberate_order_and_never_sums_current_readings(self):
        TrackerRow.objects.create(tracker=self.tracker, status='done',
            values={'Temperature': '25.5', 'Date': '2026-11-12', 'Location': 'Shanghai'})
        tools, _, _, _ = self.tools()
        params = dict(kb_slug=self.kb.slug, columns=['Temperature'], limit=1)
        self.assertNotIn('chart', json.loads(tools['tracker_lookup'](**params)))
        result = json.loads(tools['tracker_lookup'](**params, order_by=[{'field': 'Date', 'direction': 'desc'}]))
        self.assertEqual(result['chart']['value'], 0)
        self.assertEqual(result['total_matched'], 2)

    def test_ungrouped_metric_and_true_zero_count_are_cards(self):
        tools, _, _, _ = self.tools()
        for params in (dict(metrics=[{'op': 'sum', 'field': 'Temperature', 'as': 'Total'}]),
                       dict(query='NO_MATCH', metrics=[{'op': 'count', 'as': 'Matches'}])):
            self.assertEqual(json.loads(tools['tracker_lookup'](kb_slug=self.kb.slug, **params))['chart']['value'], 0)

    def test_explicit_text_non_numeric_and_grouped_queries_do_not_get_an_automatic_card(self):
        tools, _, _, _ = self.tools()
        for params in (dict(columns=['Temperature'], presentation='text'),
                       dict(columns=['Location']),
                       dict(metrics=[{'op': 'count', 'as': 'Count'}], group_by=[{'field': 'Location'}])):
            self.assertNotIn('chart', json.loads(tools['tracker_lookup'](kb_slug=self.kb.slug, **params)))

    def test_context_columns_do_not_prevent_an_exact_numeric_answer_card(self):
        for enhanced in (False, True):
            tools, citations, evidence, _ = self.tools(enhanced)
            result = json.loads(tools['tracker_lookup'](kb_slug=self.kb.slug,
                columns=['Temperature', 'Date', 'Location']))
            self.assertEqual(result['chart']['value'], 0)
            self.assertEqual(result['chart']['title'], 'Temperature')
            self.assertEqual(result['rows'][0]['values']['Location'], 'Shanghai')
            self.assertEqual(citations[0]['tracker_snapshot']['chart'], result['chart'])
            self.assertTrue(validate_sources(evidence, self.user.pk))

    def test_missing_or_boolean_values_are_not_zero(self):
        for value in (None, False, 'N/A', 'NaN', 'Infinity', {'value': 42}):
            self.row.values['Temperature'] = value
            self.row.save(update_fields=['values'])
            tools, _, _, _ = self.tools()
            self.assertNotIn('chart', json.loads(tools['tracker_lookup'](kb_slug=self.kb.slug, columns=['Temperature'])))

    def test_hidden_forged_and_changed_data_fail_validation(self):
        tools, citations, evidence, _ = self.tools()
        self.assertIn('error', json.loads(tools['tracker_lookup'](kb_slug=self.kb.slug, columns=['Hidden'])))
        self.assertEqual(citations, [])
        tools['tracker_lookup'](kb_slug=self.kb.slug, columns=['Temperature'])
        forged = copy.deepcopy(evidence[0]); forged['chart']['value'] = 999
        self.assertFalse(validate_snapshot(forged, self.user))
        self.row.values['Temperature'] = 23; self.row.save(update_fields=['values'])
        self.assertFalse(validate_sources(evidence, self.user.pk))

    def test_internal_legacy_query_result_is_unchanged(self):
        service = TrackerQuery(lambda kb: True)
        self.assertNotIn('chart', json.loads(service.call('lookup', kb_slug=self.kb.slug, columns=['Temperature'])))
        self.assertTrue(validate_snapshot(service.evidence[0], self.user))

    def test_overall_analysis_defaults_to_a_card_without_an_extra_plot_call(self):
        for enhanced in (False, True):
            tools, _, _, _ = self.tools(enhanced)
            with patch('kb.analysis_exports.agent_analysis', return_value='{}') as analyze:
                tools['tracker_analyze'](kb_slug=self.kb.slug)
                self.assertEqual(analyze.call_args.args[-1], 'plot')
                for params in (dict(output='preview'), dict(mode='options')):
                    tools['tracker_analyze'](kb_slug=self.kb.slug, **params)
                    self.assertEqual(analyze.call_args.args[-1], 'preview')


class DocumentValueCardTests(ValueCardFixture, TestCase):
    def setUp(self):
        super().setUp()
        kb = KnowledgeBase.objects.create(name='Ride manual', slug='value-card-manual')
        self.document = Document.objects.create(kb=kb, original_name='Ride manual.pdf', status='completed',
            md_content='Vehicle A rated load is 2,500 kg. Vehicle B rated load is 900 kg.')

    def source(self, tools):
        doc = self.document
        row = dict(doc_id=str(doc.pk), kb_slug=doc.kb.slug, source=doc.original_name,
            chunk_id='rated-load', page_label='第 12 页', text=doc.md_content, type='text')
        with patch('kb.retriever.search', return_value=[row]), patch('kb.provenance.annotate_results'):
            cid = json.loads(tools['kb_search']('rated load', kb_slug=doc.kb.slug))['candidates'][0]['candidate_id']
        return row, cid

    def test_selected_passage_quantity_has_a_card_and_valid_citation_in_both_modes(self):
        for enhanced in (False, True):
            tools, citations, evidence, _ = self.tools(enhanced)
            row, cid = self.source(tools)
            params = dict(candidate_id=cid, title='Vehicle A rated load', value=2500,
                unit='kg', quote='Vehicle A rated load is 2,500 kg.')
            self.assertIn('error', json.loads(tools['kb_show_value'](**params)))
            tools['kb_read_chunks']([cid])
            self.assertIn('error', json.loads(tools['kb_show_value'](**params)))
            tools['kb_select_evidence']([cid])
            result = json.loads(tools['kb_show_value'](**params))
            self.assertEqual(result['chart']['value'], 2500)
            self.assertEqual(citations[0]['value_cards'][0]['chart'], result['chart'])
            with patch('kb.keyword_index.get_chunks', return_value=[row]):
                self.assertTrue(validate_sources(evidence, self.user.pk))
                self.assertTrue(visible_citations(citations, self.user))
                forged = copy.deepcopy(citations); forged[0]['value_cards'][0]['chart']['value'] = 42
                self.assertFalse(visible_citations(forged, self.user))
            tools['kb_select_evidence']([])
            self.assertEqual(citations, [])

    def test_invented_values_units_and_quotes_cannot_be_displayed(self):
        tools, citations, _, _ = self.tools()
        _, cid = self.source(tools)
        tools['kb_read_chunks']([cid]); tools['kb_select_evidence']([cid])
        for params in (dict(value=42, unit='kg', quote='Vehicle A rated load is 2,500 kg.'),
                       dict(value=2500, unit='lb', quote='Vehicle A rated load is 2,500 kg.'),
                       dict(value=2500, unit='kg', quote='Made up rated load: 2500 kg')):
            self.assertIn('error', json.loads(tools['kb_show_value'](candidate_id=cid, title='Load', **params)))
            self.assertNotIn('value_cards', citations[0])

    def test_citation_only_answers_still_verify_the_actual_card_facts(self):
        from kb.qa_steps import _answer_with_value_cards
        text = _answer_with_value_cards('[1]', [{'citation_number': 1,
            'chart': {'type': 'metric', 'title': 'Temperature', 'value': 0, 'unit': '°C'}}])
        self.assertIn('Temperature', text)
        self.assertIn('"value": 0', text)
        self.assertEqual(_answer_with_value_cards('Explain this [1]', []), 'Explain this [1]')
