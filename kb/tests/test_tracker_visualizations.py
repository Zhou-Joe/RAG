"""Typed visualizations must use the complete selected, permission-checked data."""
from __future__ import annotations

from copy import deepcopy
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from kb.models import KbTracker, KnowledgeBase, TrackerRow
from kb.publication import digest, validate_sources, visible_citations
from kb.tracker_query import TrackerQuery, validate_snapshot
from kb.tracker_visualizations import agent_visualization, validate_visualization_snapshot


class TrackerVisualizationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('extended-visual-reader')
        self.kb = KnowledgeBase.objects.create(name='Operations', slug='extended-visual-data',
            category='business', is_folder=True, created_by=self.user,
            active_fields=['Name', 'Category', 'Date', 'Hour', 'Amount', 'Start', 'End', 'Group'])
        self.tracker = KbTracker.objects.create(kb=self.kb, enabled=True,
            fields=[{'label': field} for field in self.kb.active_fields + ['Hidden']])
        self.records = []
        for name, category, day, hour, amount in [
            ('Pump A', 'Mechanical', '2026-11-01', '09', 3),
            ('Pump B', 'Mechanical', '2026-11-02', '10', 5),
            ('Pump C', 'Electrical', '2026-11-02', '09', 2),
        ]:
            self.records.append(TrackerRow.objects.create(tracker=self.tracker, status='done', values={
                'Name': name, 'Category': category, 'Date': day, 'Hour': hour, 'Amount': amount,
                'Start': day + 'T09:00:00+08:00', 'End': day + 'T10:30:00+08:00',
                'Group': 'Team A', 'Hidden': 'HIDDEN_VISUAL_SENTINEL',
            }))

    def visualize(self, kind, *, service=None, kb=None, **params):
        service = service or TrackerQuery(lambda kb: True)
        text = agent_visualization(self.user, lambda kb: True, service, (kb or self.kb).slug,
                                  {'kind': kind, **params})
        self.assertIsInstance(json.loads(text), dict)
        self.assertTrue(service.evidence)
        return service.evidence[-1]['chart'], service.evidence[-1], text

    def test_progress_uses_complete_aggregate_and_real_target_without_sample_counts(self):
        chart, snapshot, text = self.visualize('progress', operation='sum', measure='Amount',
                                               target=20, title='Budget Used', unit='hours')
        self.assertEqual(chart['type'], 'progress')
        self.assertEqual(chart['title'], 'Budget Used')
        self.assertEqual(chart['value'], 10)
        self.assertEqual(chart['target'], 20)
        self.assertEqual(chart['unit'], 'hours')
        self.assertNotIn('HIDDEN_VISUAL_SENTINEL', text)
        self.assertTrue(validate_snapshot(snapshot, self.user))
        self.assertTrue(validate_sources([snapshot], self.user.pk))

    def test_progress_preserves_zero_and_missing_values_and_rejects_nonpositive_targets(self):
        TrackerRow.objects.filter(tracker=self.tracker).update(values={'Name': 'Zero', 'Amount': 0})
        chart, _, _ = self.visualize('progress', operation='sum', measure='Amount', target=10)
        self.assertEqual(chart['value'], 0)
        TrackerRow.objects.filter(tracker=self.tracker).update(values={'Name': 'Missing', 'Amount': None})
        chart, _, _ = self.visualize('progress', operation='avg', measure='Amount', target=10)
        self.assertIsNone(chart['value'])
        for target in (0, -1, float('nan'), float('inf'), True):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.visualize('progress', target=target)

    def test_sparkline_groups_the_actual_business_date_field_not_record_update_time(self):
        chart, _, text = self.visualize('sparkline', operation='sum', measure='Amount',
                                     x_field='Date', period='day')
        self.assertEqual(chart['type'], 'sparkline')
        self.assertEqual(chart['labels'], ['2026-11-01', '2026-11-02'])
        self.assertEqual(chart['series'][0]['values'], [3, 7])
        facts = json.loads(text)
        self.assertEqual((facts['first_value'], facts['latest_value'], facts['min_value'], facts['max_value']), (3, 7, 3, 7))
        self.assertEqual(facts['change'], 4)
        self.assertAlmostEqual(facts['change_pct'], 400 / 3)
        if 'x_values' in chart:
            self.assertEqual(chart['x_values'][1] - chart['x_values'][0], 86400000)

    def test_heatmap_aggregates_both_dimensions_and_keeps_unobserved_cells_missing(self):
        chart, _, text = self.visualize('heatmap', x_field='Hour', y_field='Category', operation='sum', measure='Amount')
        self.assertEqual(chart['type'], 'heatmap')
        cells = {(chart['x_labels'][cell['x']], chart['y_labels'][cell['y']]): cell['value']
                 for cell in chart['cells']}
        self.assertEqual(cells[('09', 'Mechanical')], 3)
        self.assertEqual(cells[('10', 'Mechanical')], 5)
        self.assertEqual(cells[('09', 'Electrical')], 2)
        self.assertIsNone(cells.get(('10', 'Electrical')))
        self.assertEqual(json.loads(text)['highest_cells'][0], {'x': '10', 'y': 'Mechanical', 'value': 5})

    def test_pareto_uses_all_groups_and_descending_nonnegative_values(self):
        chart, _, text = self.visualize('pareto', x_field='Category', operation='sum', measure='Amount')
        self.assertEqual(chart['labels'], ['Mechanical', 'Electrical'])
        self.assertEqual(chart['series'][0]['values'], [8, 2])
        self.assertEqual(sum(chart['series'][0]['values']), 10)
        self.assertEqual(json.loads(text)['top_contributors'][0], {'category': 'Mechanical', 'value': 8, 'share_pct': 80})
        first = self.records[0]
        first.values = {**first.values, 'Amount': -20}; first.save(update_fields=['values'])
        with self.assertRaises(ValueError):
            self.visualize('pareto', x_field='Category', operation='sum', measure='Amount')

    def test_heatmap_keeps_missing_blank_and_literal_placeholder_categories_distinct(self):
        TrackerRow.objects.filter(tracker=self.tracker).delete()
        categories = [None, '', '(Missing)', '(Blank)', '(Missing) [2]']
        values = [(category, None, 2 ** index) for index, category in enumerate(categories)]
        values += [(None, category, 2 ** (index + 4)) for index, category in enumerate(categories[1:], 1)]
        TrackerRow.objects.bulk_create([TrackerRow(tracker=self.tracker, status='done',
            values={'Category': x, 'Hour': y, 'Amount': value, 'Hidden': 'DO_NOT_EXPOSE'})
            for x, y, value in values])
        chart, snapshot, _ = self.visualize('heatmap', x_field='Category', y_field='Hour',
                                          operation='sum', measure='Amount')
        self.assertEqual(len(chart['x_labels']), 5)
        self.assertEqual(len(chart['y_labels']), 5)
        self.assertEqual(len(chart['cells']), len(values))
        self.assertEqual(len({(cell['x'], cell['y']) for cell in chart['cells']}), len(values))
        self.assertEqual(sorted(cell['value'] for cell in chart['cells']), sorted(value for _, _, value in values))
        self.assertEqual(sum(cell['value'] for cell in chart['cells']), sum(value for _, _, value in values))
        self.assertIn('(Missing)', chart['x_labels'])
        self.assertIn('(Blank)', chart['x_labels'])
        self.assertIn('(Missing) [2]', chart['x_labels'])
        self.assertNotIn('DO_NOT_EXPOSE', json.dumps(chart))
        self.assertTrue(validate_visualization_snapshot(snapshot, self.user))

    def test_pareto_keeps_missing_blank_and_literal_category_contributions_separate(self):
        TrackerRow.objects.filter(tracker=self.tracker).delete()
        TrackerRow.objects.bulk_create([TrackerRow(tracker=self.tracker, status='done',
            values={'Category': category, 'Amount': amount})
            for category, amount in [(None, 8), ('', 4), ('(Missing)', 2), ('(Blank)', 1)]])
        chart, _, _ = self.visualize('pareto', x_field='Category', operation='sum', measure='Amount')
        self.assertEqual(len(chart['labels']), len(set(chart['labels'])))
        self.assertEqual(chart['series'][0]['values'], [8, 4, 2, 1])
        self.assertEqual(sum(chart['series'][0]['values']), 15)

    def test_timeline_retains_true_iso_date_positions_and_original_labels(self):
        chart, _, text = self.visualize('timeline', label_field='Name', start_field='Start',
                                     end_field='End', group_field='Group')
        self.assertEqual(chart['type'], 'timeline')
        self.assertEqual(len(chart['items']), 3)
        items = {item['label']: item for item in chart['items']}
        self.assertEqual(items['Pump A']['end'] - items['Pump A']['start'], 90 * 60 * 1000)
        self.assertEqual(items['Pump B']['start'] - items['Pump A']['start'], 24 * 60 * 60 * 1000)
        self.assertEqual(items['Pump A']['group'], 'Team A')
        self.assertEqual(items['Pump A']['start_label'], '2026-11-01T09:00:00+08:00')
        facts = json.loads(text)
        self.assertEqual(facts['range_start'], '2026-11-01T09:00:00+08:00')
        self.assertEqual(facts['range_end'], '2026-11-02T10:30:00+08:00')
        self.assertEqual(facts['longest_task']['duration_ms'], 90 * 60 * 1000)

    def test_timeline_rejects_invalid_or_reversed_dates_instead_of_fabricating_tasks(self):
        row = self.records[0]
        for start, end in [('not-a-date', '2026-11-01'), ('2026-11-03', '2026-11-01')]:
            row.values = {**row.values, 'Start': start, 'End': end}; row.save(update_fields=['values'])
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.visualize('timeline', label_field='Name', start_field='Start', end_field='End')

    def test_table_has_only_selected_columns_with_truthful_loaded_and_total_counts(self):
        chart, _, _ = self.visualize('table', columns=['Name', 'Amount'], limit=2)
        self.assertEqual(chart['type'], 'table')
        self.assertEqual([column['key'] for column in chart['columns']], ['Name', 'Amount'])
        self.assertEqual(len(chart['rows']), 2)
        self.assertEqual(chart['total_rows'], 3)
        self.assertTrue(chart['truncated'])
        self.assertTrue(all(set(row) <= {'Name', 'Amount'} for row in chart['rows']))
        self.assertNotIn('Hidden', json.dumps(chart))
        self.assertNotIn('HIDDEN_VISUAL_SENTINEL', json.dumps(chart))

    def test_hidden_columns_and_filter_inputs_cannot_be_used_by_any_visual_kind(self):
        cases = [
            ('progress', {'operation': 'sum', 'measure': 'Hidden', 'target': 10}),
            ('sparkline', {'x_field': 'Hidden'}),
            ('heatmap', {'x_field': 'Hidden', 'y_field': 'Category'}),
            ('pareto', {'x_field': 'Hidden'}),
            ('timeline', {'label_field': 'Name', 'start_field': 'Hidden', 'end_field': 'End'}),
            ('table', {'columns': ['Name', 'Hidden']}),
            ('table', {'columns': ['Name'], 'filters': [{'field': 'Hidden', 'op': 'contains', 'value': 'HIDDEN'}]}),
        ]
        for kind, params in cases:
            with self.subTest(kind=kind, params=params), self.assertRaises(ValueError):
                self.visualize(kind, **params)

    def test_hidden_free_text_is_not_a_match_and_original_data_remains_unchanged(self):
        original = deepcopy(self.records[0].values)
        chart, _, _ = self.visualize('table', columns=['Name'], query='HIDDEN_VISUAL_SENTINEL')
        self.assertEqual(chart['rows'], [])
        self.assertEqual(chart['total_rows'], 0)
        self.records[0].refresh_from_db()
        self.assertEqual(self.records[0].values, original)

    def test_charts_refuse_overbound_groups_or_tasks_while_tables_mark_truncation(self):
        TrackerRow.objects.bulk_create([TrackerRow(tracker=self.tracker, status='done', values={
            'Name': f'Task {index}', 'Category': f'Cause {index}', 'Amount': 1,
            'Start': '2026-11-01', 'End': '2026-11-02',
        }) for index in range(101)])
        with self.assertRaises(ValueError):
            self.visualize('timeline', label_field='Name', start_field='Start', end_field='End')
        with self.assertRaises(ValueError):
            self.visualize('pareto', x_field='Category', operation='sum', measure='Amount')
        chart, _, _ = self.visualize('table', columns=['Name'], limit=5)
        self.assertEqual(len(chart['rows']), 5)
        self.assertEqual(chart['total_rows'], 104)
        self.assertTrue(chart['truncated'])

    def test_snapshot_replays_actual_values_and_rejects_chart_data_or_visibility_changes(self):
        chart, snapshot, _ = self.visualize('progress', operation='sum', measure='Amount', target=20)
        self.assertTrue(validate_visualization_snapshot(snapshot, self.user))
        tampered = deepcopy(snapshot); tampered['chart']['value'] = chart['value'] + 1
        self.assertFalse(validate_visualization_snapshot(tampered, self.user))
        citation = {'kind': 'tracker', 'kb_slug': self.kb.slug,
                    'result_digest': snapshot['result_digest'], 'tracker_snapshot': snapshot}
        self.assertTrue(visible_citations([citation], self.user))
        self.kb.active_fields = ['Name']; self.kb.save(update_fields=['active_fields'])
        self.assertFalse(validate_snapshot(snapshot, self.user))
        self.kb.active_fields = None; self.kb.save(update_fields=['active_fields'])
        self.records[0].values = {**self.records[0].values, 'Amount': 9}
        self.records[0].save(update_fields=['values'])
        self.assertFalse(validate_visualization_snapshot(snapshot, self.user))

    def test_same_sized_table_queries_have_distinct_full_result_identities(self):
        service = TrackerQuery(lambda kb: True)
        _, first, _ = self.visualize('table', service=service, columns=['Name', 'Amount'],
                                    filters=[{'field': 'Name', 'op': 'eq', 'value': 'Pump A'}])
        _, second, _ = self.visualize('table', service=service, columns=['Name', 'Amount'],
                                     filters=[{'field': 'Name', 'op': 'eq', 'value': 'Pump B'}])
        self.assertEqual(first['text'], second['text'])
        self.assertNotEqual(first['result_digest'], second['result_digest'])
        self.assertEqual(first['text_digest'], digest(first['text']))
        self.assertEqual(len(service.evidence), 2)
        citations = [{'kind': 'tracker', 'kb_slug': self.kb.slug, 'result_digest': item['result_digest'],
                      'tracker_snapshot': item} for item in service.evidence]
        self.assertEqual(len({citation['result_digest'] for citation in citations}), 2)
        self.assertTrue(visible_citations(citations, self.user))
        self.assertTrue(validate_sources(service.evidence, self.user.pk))

    def test_result_identity_detects_changed_text_query_and_chart_even_with_the_same_summary(self):
        _, snapshot, _ = self.visualize('table', columns=['Name', 'Amount'],
                                       filters=[{'field': 'Name', 'op': 'eq', 'value': 'Pump A'}])
        text_changed = deepcopy(snapshot); text_changed['text'] += ' tampered'
        self.assertFalse(validate_visualization_snapshot(text_changed, self.user))
        query_changed = deepcopy(snapshot); query_changed['visualization_query']['filters'][0]['value'] = 'Pump B'
        self.assertFalse(validate_visualization_snapshot(query_changed, self.user))
        chart_changed = deepcopy(snapshot); chart_changed['chart']['rows'][0]['Amount'] = 99
        self.assertFalse(validate_visualization_snapshot(chart_changed, self.user))
        # The identity includes query settings even when they match the same rows.
        same_rows = deepcopy(snapshot); same_rows['visualization_query']['filters'][0]['op'] = 'contains'
        self.assertFalse(validate_visualization_snapshot(same_rows, self.user))

    def test_snapshot_rechecks_permissions_and_fails_for_another_users_private_library(self):
        _, snapshot, _ = self.visualize('progress', target=20)
        self.assertTrue(validate_visualization_snapshot(snapshot, self.user))
        other = get_user_model().objects.create_user('other-visual-owner')
        self.kb.visibility = 'individual'; self.kb.created_by = other
        self.kb.save(update_fields=['visibility', 'created_by'])
        self.assertFalse(validate_visualization_snapshot(snapshot, self.user))

    def test_same_label_in_another_tracker_does_not_bypass_its_hidden_field_policy(self):
        folder = KnowledgeBase.objects.create(name='Mixed Visual Tables', slug='mixed-visuals', category='business', is_folder=True)
        first = KnowledgeBase.objects.create(name='Visible Amount', slug='visible-visual-amount', parent=folder,
            category='business', active_fields=['Name', 'Amount'])
        second = KnowledgeBase.objects.create(name='Hidden Amount', slug='hidden-visual-amount', parent=folder,
            category='business', active_fields=['Name'])
        schema = [{'label': field} for field in ('Name', 'Amount')]
        one = KbTracker.objects.create(kb=first, fields=schema)
        two = KbTracker.objects.create(kb=second, fields=schema)
        TrackerRow.objects.create(tracker=one, status='done', values={'Name': 'Visible', 'Amount': 7})
        TrackerRow.objects.create(tracker=two, status='done', values={'Name': 'Hidden', 'Amount': 99999})
        chart, _, _ = self.visualize('progress', kb=folder, operation='sum', measure='Amount', target=10)
        self.assertEqual(chart['value'], 7)
        table, _, _ = self.visualize('table', kb=folder, columns=['Name', 'Amount'])
        self.assertNotIn('99999', json.dumps(table))

    def test_budget_exhaustion_is_bounded_and_adds_no_unpublished_chart(self):
        service = TrackerQuery(lambda kb: True); service.remaining = 1000
        text = agent_visualization(self.user, lambda kb: True, service, self.kb.slug,
                                  {'kind': 'progress', 'target': 10})
        self.assertTrue(json.loads(text)['budget_exhausted'])
        self.assertLessEqual(len(text), 1000)
        self.assertGreaterEqual(service.remaining, 0)
        self.assertEqual(service.evidence, [])
