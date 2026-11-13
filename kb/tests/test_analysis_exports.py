"""Complete exports, scoped agent analysis, real files and publication evidence."""
import io
import json
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from kb.agent import _build_agent
from kb.analysis_exports import agent_analysis, interactive_chart, query_analysis
from kb.models import Document, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow
from kb.publication import validate_sources, visible_citations
from kb.tracker_query import TrackerQuery


class AnalysisExportTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('export-owner', is_staff=True)
        self.other = get_user_model().objects.create_user('export-other')
        self.client.force_login(self.owner)
        self.kb = KnowledgeBase.objects.create(name='Analysis', slug='exports', is_folder=True,
                                               category='business', created_by=self.owner)
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name='Source', mode='push', created_by=self.owner)
        self.tracker = KbTracker.objects.create(kb=self.kb, enabled=True,
                                               fields=[{'label': key} for key in ('Object', 'Number')])
        self.url = reverse('kb:feed_kb_data', args=[self.kb.slug])
        self.params = {'kind': 'summary', 'scope': 'history', 'group': 'Object', 'op': 'sum', 'measure': 'Number'}
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        override = self.settings(DATA_DIR=self.directory.name)
        override.enable()
        self.addCleanup(override.disable)

    def row(self, name='A', value='3', minute=None, status='done'):
        row = TrackerRow.objects.create(tracker=self.tracker, feed=self.feed, status=status,
                                         external_id=uuid4().hex, values={'Object': name, 'Number': value})
        if minute is not None:
            TrackerRow.objects.filter(pk=row.pk).update(updated_at=datetime(
                2026, 11, 10, 10, minute, tzinfo=ZoneInfo('Asia/Shanghai')))
        return row

    def workbook(self, response):
        self.assertEqual(response.status_code, 200, getattr(response, 'content', b''))
        return load_workbook(io.BytesIO(response.content))

    def test_excel_exports_unloaded_groups_full_labels_and_safe_text(self):
        for index in range(58):
            self.row(f'Group {index:02}', str(index))
        self.row('Long ' + 'x' * 1200, '2.5')
        self.row('=HYPERLINK("https://example.com")', '7')
        self.row('invalid numeric', 'closed')
        self.row('ignored', '200', status='proposed')
        page = self.client.get(self.url, self.params).json()
        self.assertEqual(len(page['groups']), 50)
        self.assertTrue(page['has_more'])
        book = self.workbook(self.client.get(self.url, {**self.params, 'export': 'xlsx', 'offset': '50'}))
        sheet = book['Analysis']
        values = {row[0]: row for row in sheet.iter_rows(min_row=2, values_only=True)}
        self.assertEqual(len(values), 61)
        self.assertEqual(values['Long ' + 'x' * 1200][1:], (2.5, 1))
        self.assertEqual(values['invalid numeric'][1:], (None, 0))
        formula_cell = next(row[0] for row in sheet.iter_rows(min_row=2) if row[0].value.startswith('='))
        self.assertEqual(formula_cell.data_type, 's')
        self.assertIn('Details', book.sheetnames)
        self.assertEqual(sheet.auto_filter.ref, 'A1:C62')

    def test_trend_export_retains_missing_zero_and_changes_for_each_object(self):
        self.row('A', '0', 1)
        self.row('A', '12', 11)
        self.row('A', '18', 21)
        self.row('B', 'bad', 11)
        params = {'kind': 'trend', 'object_field': 'Object', 'object_value': ['A', 'B'],
                  'period': 'minute', 'span': 10, 'op': 'avg', 'measure': 'Number',
                  'from': '2026-11-10T10:00', 'to': '2026-11-10T10:40', 'export': 'xlsx'}
        book = self.workbook(self.client.get(self.url, params))
        rows = list(book['Analysis'].iter_rows(values_only=True))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[1][1], 0)
        self.assertEqual(rows[2][1:4], (12, 12, None))
        self.assertEqual(rows[2][6], 'zero_baseline')
        self.assertEqual(rows[3][1:4], (18, 6, 50))
        self.assertEqual(rows[4][1:4], (None, None, None))
        self.assertEqual(rows[2][7:12], (None, None, None, 1, 0))

    def test_export_respects_range_feed_permissions_and_validates_format(self):
        self.row('Included', '2', 10)
        self.row('Excluded', '9', 20)
        response = self.client.get(self.url, {**self.params, 'export': 'xlsx',
                                   'from': '2026-11-10T10:10', 'to': '2026-11-10T10:20'})
        rows = list(self.workbook(response)['Analysis'].iter_rows(values_only=True))
        self.assertEqual(rows[1:], [('Included', 2, 1)])
        self.assertEqual(self.client.get(self.url, {**self.params, 'export': 'exe'}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {'kind': 'rows', 'export': 'xlsx'}).status_code, 400)
        self.kb.visibility = 'individual'
        self.kb.save(update_fields=['visibility'])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url, {**self.params, 'export': 'xlsx'}).status_code, 404)

    def test_agent_generates_private_excel_and_safe_plot_from_full_data(self):
        self.row('<script>alert(1)</script>', '2')
        self.row('B', '7')
        service = TrackerQuery(lambda kb: True)
        result = json.loads(agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, self.params, 'both'))
        self.assertEqual({file['format'] for file in result['files']}, {'xlsx', 'svg'})
        for file in result['files']:
            response = self.client.get(file['url'])
            self.assertEqual(response.status_code, 200)
            content = b''.join(response.streaming_content)
            if file['format'] == 'xlsx':
                self.assertEqual(load_workbook(io.BytesIO(content))['Analysis'].max_row, 3)
            else:
                svg = ET.fromstring(content)
                self.assertEqual(svg.tag, '{http://www.w3.org/2000/svg}svg')
                self.assertEqual(svg.findall('.//{http://www.w3.org/2000/svg}script'), [])
            self.assertEqual(response['Cache-Control'], 'private, no-store')
        url = result['files'][0]['url']
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.owner)
        self.kb.created_by = self.other
        self.kb.visibility = 'individual'
        self.kb.save(update_fields=['created_by', 'visibility'])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_chat_summary_chart_uses_full_groups_outside_the_model_preview(self):
        for index in range(30):
            self.row(f'Group {index:02}', index)
        self.row('<script>alert(1)</script>', '2')
        service = TrackerQuery(lambda kb: True)
        result = json.loads(agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, self.params, 'plot'))
        snapshot = service.evidence[0]
        chart = snapshot['chart']
        self.assertEqual(chart['type'], 'bar')
        self.assertEqual(chart['meta'], {'system_metric': 'value', 'system_summary': True})
        self.assertEqual(len(chart['labels']), 31)
        self.assertLess(len(result['analysis']['groups']), len(chart['labels']))
        values = dict(zip(chart['labels'], chart['series'][0]['values']))
        self.assertEqual(values['Group 00'], 0)
        self.assertEqual(values['Group 29'], 29)
        self.assertEqual(values['<script>alert(1)</script>'], 2)
        self.assertNotIn('chart', result)
        self.assertNotIn('chart', json.loads(snapshot['text']))
        self.assertTrue(validate_sources(service.evidence, self.owner.pk))

    def test_chat_trend_chart_keeps_all_periods_gaps_zeros_and_selected_metric(self):
        self.row('A', '0', 1)
        self.row('A', '12', 11)
        self.row('A', '18', 21)
        self.row('B', 'bad', 11)
        params = {'kind': 'trend', 'object_field': 'Object', 'object_value': ['A', 'B'],
                  'period': 'minute', 'span': 10, 'op': 'avg', 'measure': 'Number',
                  'from': '2026-11-10T10:00', 'to': '2026-11-10T15:00'}
        service = TrackerQuery(lambda kb: True)
        agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, params, 'both')
        snapshot = service.evidence[0]
        chart = snapshot['chart']
        self.assertEqual(chart['type'], 'line')
        self.assertEqual(chart['meta'], {'system_metric': 'value', 'system_summary': False})
        self.assertEqual(len(chart['labels']), 30)
        self.assertEqual(chart['series'][0]['values'][:4], [0, 12, 18, None])
        self.assertEqual(chart['series'][1]['values'][:4], [None, None, None, None])
        self.assertEqual(chart['series'][1]['quality'][1], {'valid': 0, 'records': 1})
        _, data = query_analysis(self.owner, self.kb.slug, params)
        changes = interactive_chart(data, {**params, 'chart_metric': 'change_pct'}, self.kb.name)
        self.assertEqual(changes['series'][0]['values'][:4], [None, None, 50, None])
        self.assertEqual(changes['options']['y_label'], 'Change (%)')
        self.assertEqual(changes['meta'], {'system_metric': 'change_pct', 'system_summary': False})
        self.assertTrue(validate_sources(service.evidence, self.owner.pk))

    def test_chat_chart_payload_is_verified_and_permission_checked_in_history(self):
        self.row('A', '3')
        service = TrackerQuery(lambda kb: True)
        agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, self.params, 'plot')
        snapshot = service.evidence[0]
        citation = {'kind': 'tracker', 'kb_slug': self.kb.slug, 'result_digest': snapshot['result_digest'],
                    'tracker_snapshot': snapshot}
        restored = json.loads(json.dumps(citation))
        self.assertEqual(restored['tracker_snapshot']['chart'], snapshot['chart'])
        self.assertTrue(visible_citations([restored], self.owner))
        for mutate in (
            lambda chart: chart['series'][0]['values'].__setitem__(0, 9000),
            lambda chart: chart['labels'].__setitem__(0, 'Invented group'),
            lambda chart: chart.update(type='pie'),
            lambda chart: chart.update(options={'callback': 'alert(1)'}),
        ):
            changed = deepcopy(restored)
            mutate(changed['tracker_snapshot']['chart'])
            self.assertFalse(visible_citations([changed], self.owner))
        self.kb.visibility = 'individual'
        self.kb.save(update_fields=['visibility'])
        self.assertFalse(visible_citations([restored], self.other))

    def test_chat_chart_is_only_added_when_plot_is_requested_and_count_omits_numeric_quality(self):
        self.row('A', '3', 1)
        for output in ('preview', 'excel'):
            service = TrackerQuery(lambda kb: True)
            agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, self.params, output)
            self.assertNotIn('chart', service.evidence[0])
        params = {'kind': 'trend', 'object_field': 'Object', 'object_value': ['A'],
                  'period': 'minute', 'span': 10, 'op': 'count',
                  'from': '2026-11-10T10:00', 'to': '2026-11-10T10:20'}
        service = TrackerQuery(lambda kb: True)
        agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, params, 'plot')
        self.assertEqual(service.evidence[0]['chart']['series'][0]['values'], [1, None])
        self.assertNotIn('quality', service.evidence[0]['chart']['series'][0])

    def test_chat_chart_requested_types_use_actual_data_and_valid_combo_series(self):
        self.row('A', '3', 1)
        self.row('B', '7', 1)
        _, data = query_analysis(self.owner, self.kb.slug, self.params)
        for kind in ('line', 'bar', 'area', 'pie', 'doughnut'):
            with self.subTest(kind=kind):
                chart = interactive_chart(data, {**self.params, 'chart_type': kind}, self.kb.name)
                self.assertEqual(chart['type'], kind)
                self.assertEqual(chart['labels'], ['A', 'B'])
                self.assertEqual(chart['series'][0]['values'], [3, 7])
        trend_params = {'kind': 'trend', 'object_field': 'Object', 'object_value': ['A', 'B'],
                        'period': 'minute', 'span': 10, 'op': 'sum', 'measure': 'Number',
                        'from': '2026-11-10T10:00', 'to': '2026-11-10T10:20', 'chart_type': 'combo'}
        service = TrackerQuery(lambda kb: True)
        agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, trend_params, 'plot')
        chart = service.evidence[0]['chart']
        self.assertEqual(chart['type'], 'combo')
        self.assertEqual([part['type'] for part in chart['series']], ['bar', 'line'])
        self.assertEqual([part['values'] for part in chart['series']], [[3, None], [7, None]])
        self.assertTrue(validate_sources(service.evidence, self.owner.pk))

    def test_chat_scatter_uses_numeric_categories_rather_than_invented_indices(self):
        self.row('0', '3')
        self.row('12.5', '7')
        service = TrackerQuery(lambda kb: True)
        params = {**self.params, 'chart_type': 'scatter'}
        agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, params, 'plot')
        chart = service.evidence[0]['chart']
        self.assertEqual(chart['type'], 'scatter')
        self.assertEqual(chart['x_values'], [0, 12.5])
        self.assertEqual(chart['series'][0]['values'], [3, 7])
        self.assertTrue(validate_sources(service.evidence, self.owner.pk))

    def test_chat_invalid_chart_semantics_are_rejected_before_files_are_written(self):
        self.row('A', '-3')
        self.row('B', '7')
        for options in (
            {'chart_type': 'pie'}, {'chart_type': 'doughnut'}, {'chart_type': 'combo'},
            {'chart_type': 'scatter'}, {'chart_type': 'script'},
            {'chart_metric': 'change'},
            {'chart_type': 'pie', 'op': 'avg'}, {'chart_type': 'pie', 'chart_metric': 'change'},
        ):
            with self.subTest(options=options):
                service = TrackerQuery(lambda kb: True)
                with self.assertRaises(ValueError):
                    agent_analysis(self.owner, lambda kb: True, service, self.kb.slug,
                                   {**self.params, **options}, 'both')
                self.assertEqual(service.evidence, [])
                self.assertFalse((Path(self.directory.name) / 'analysis_exports').exists())

    def test_chat_chart_size_limits_reject_data_instead_of_silently_dropping_points(self):
        group = {'by': {'Object': 'A'}, 'metrics': {'value': 1}}
        with self.assertRaises(ValueError):
            interactive_chart({'kind': 'tracker_summary', 'groups': [group] * 51}, self.params, self.kb.name)
        point = {'bucket': '2026-11-10', 'label': '2026-11-10', 'value': 1}
        for series in ([{'object_value': 'A', 'points': [point] * 501}],
                       [{'object_value': 'A', 'points': [point]}] * 9):
            with self.assertRaises(ValueError):
                interactive_chart({'kind': 'trend', 'series': series}, {'op': 'count'}, self.kb.name)

    def test_ungrouped_summary_automatically_publishes_a_verified_value_card(self):
        self.row('A', '0')
        self.row('B', '7.5')
        self.row('C', 'invalid')
        params = {**self.params, 'group': ''}
        service = TrackerQuery(lambda kb: True)
        result = json.loads(agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, params, 'plot'))
        snapshot = service.evidence[0]
        chart = snapshot['chart']
        self.assertEqual(chart['type'], 'metric')
        self.assertEqual(chart['value'], 7.5)
        self.assertIn('Number', chart['title'])
        self.assertEqual(chart['subtitle'], self.kb.name)
        self.assertEqual(chart['quality'], {'valid': 2, 'records': 3})
        self.assertNotIn('chart', result)
        self.assertNotIn('chart', json.loads(snapshot['text']))
        self.assertTrue(validate_sources(service.evidence, self.owner.pk))
        citation = {'kind': 'tracker', 'kb_slug': self.kb.slug,
                    'result_digest': snapshot['result_digest'], 'tracker_snapshot': snapshot}
        restored = json.loads(json.dumps(citation))
        self.assertTrue(visible_citations([restored], self.owner))
        for change in ({'value': 9000}, {'title': 'Invented title'}, {'subtitle': 'Another source'}):
            altered = deepcopy(restored)
            altered['tracker_snapshot']['chart'].update(change)
            self.assertFalse(visible_citations([altered], self.owner))

    def test_value_card_retains_real_zero_missing_and_empty_count(self):
        params = {**self.params, 'group': '', 'chart_type': 'metric'}
        self.row('A', '0')
        _, data = query_analysis(self.owner, self.kb.slug, params)
        self.assertEqual(interactive_chart(data, params, self.kb.name)['value'], 0)
        TrackerRow.objects.all().delete()
        self.row('A', 'invalid')
        _, data = query_analysis(self.owner, self.kb.slug, params)
        self.assertIsNone(interactive_chart(data, params, self.kb.name)['value'])
        TrackerRow.objects.all().delete()
        count_params = {**params, 'op': 'count', 'measure': ''}
        _, data = query_analysis(self.owner, self.kb.slug, count_params)
        chart = interactive_chart(data, count_params, self.kb.name)
        self.assertEqual(chart['value'], 0)
        self.assertEqual(chart['title'], 'Records')
        self.assertEqual(chart['quality'], {'valid': 0, 'records': 0})

    def test_legacy_auto_summary_bar_snapshot_remains_valid_after_value_card_default(self):
        self.row('A', '3')
        service = TrackerQuery(lambda kb: True)
        params = {**self.params, 'group': '', 'chart_type': 'bar'}
        agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, params, 'plot')
        snapshot = service.evidence[0]
        snapshot['analysis_query'].pop('chart_type')
        self.assertEqual(snapshot['chart']['type'], 'bar')
        self.assertTrue(validate_sources(service.evidence, self.owner.pk))
        snapshot['chart']['series'][0]['values'][0] = 9000
        self.assertFalse(validate_sources(service.evidence, self.owner.pk))

    def test_explicit_value_card_rejects_grouped_trend_and_multiple_outputs(self):
        self.row('A', '3')
        _, grouped = query_analysis(self.owner, self.kb.slug, self.params)
        self.assertEqual(interactive_chart(grouped, self.params, self.kb.name)['type'], 'bar')
        for data, params in (
            (grouped, {**self.params, 'chart_type': 'metric'}),
            ({'kind': 'trend', 'series': []}, {'chart_type': 'metric'}),
            ({'kind': 'tracker_summary', 'groups': [{'metrics': {'value': 1, 'other': 2}}]}, {'chart_type': 'metric'}),
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                interactive_chart(data, params, self.kb.name)

    def test_agent_period_evidence_freezes_now_and_rejects_changed_data(self):
        row = self.row('A', '3')
        service = TrackerQuery(lambda kb: True)
        params = {'kind': 'trend', 'op': 'sum', 'measure': 'Number', 'object_field': 'Object',
                  'object_value': ['A'], 'period': 'day', 'span': '1'}
        result = json.loads(agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, params, 'preview'))
        self.assertTrue(result['analysis']['series'])
        self.assertTrue(service.evidence[0]['analysis_query']['to'])
        self.assertTrue(validate_sources(service.evidence, self.owner.pk))
        snapshot = service.evidence[0]
        citation = {'kind': 'tracker', 'kb_slug': self.kb.slug, 'result_digest': snapshot['result_digest'],
                    'tracker_snapshot': snapshot}
        self.assertTrue(visible_citations([citation], self.owner))
        TrackerRow.objects.filter(pk=row.pk).update(values={'Object': 'A', 'Number': '8'})
        self.assertFalse(validate_sources(service.evidence, self.owner.pk))

    def test_options_and_export_limits_are_explicit(self):
        self.row()
        _, options = query_analysis(self.owner, self.kb.slug, {'kind': 'options', 'object_field': 'Object'})
        self.assertEqual(options['numeric_fields'], ['Number'])
        self.assertEqual(options['object_values'], ['A'])
        for index in range(51):
            self.row(str(index), index)
        response = self.client.get(self.url, {**self.params, 'export': 'svg'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('50', response.json()['message'])

    def test_keyword_and_business_date_filters_apply_before_all_analysis_and_exports(self):
        self.tracker.fields.append({'label': 'Business date'})
        self.tracker.save(update_fields=['fields'])
        for name, number, business_date in [
            ('Ride A', '10', '2026-11-03'), ('Ride A', '80', '2026-10-31'),
            ('Other', '70', '2026-11-03'), ('Ride A', '2', '2026-11-03'),
            ('Ride B', '90', '2026-11-03'),
        ]:
            row = self.row(name, number, 10)
            TrackerRow.objects.filter(pk=row.pk).update(values={**row.values, 'Business date': business_date})
        filters = [
            {'field': 'Object', 'op': 'eq', 'value': 'Ride A'},
            {'field': 'Number', 'op': 'gte', 'type': 'number', 'value': 5},
            {'field': 'Business date', 'op': 'between', 'type': 'date',
             'value': ['2026-11-01', '2026-11-07']},
        ]
        params = {**self.params, 'analysis_search': 'Ride', 'analysis_filters': json.dumps(filters)}
        _, data = query_analysis(self.owner, self.kb.slug, params)
        self.assertEqual(data['total_matched'], 1)
        self.assertEqual(data['groups'][0]['metrics']['value'], 10)
        self.assertEqual(data['criteria'], {'query': 'Ride', 'filters': filters})
        book = self.workbook(self.client.get(self.url, {**params, 'export': 'xlsx'}))
        self.assertEqual(list(book['Analysis'].values)[1:], [('Ride A', 10, 1)])
        _, options = query_analysis(self.owner, self.kb.slug, {**params, 'kind': 'options', 'object_field': 'Object'})
        self.assertEqual(options['object_values'], ['Ride A'])
        self.assertEqual(options['data_from'], options['data_to'])
        _, trend = query_analysis(self.owner, self.kb.slug, {**params, 'kind': 'trend',
            'object_field': 'Object', 'object_value': ['Ride A'], 'period': 'minute', 'span': '10',
            'from': '2026-11-10T10:00', 'to': '2026-11-10T10:30'})
        self.assertEqual([point['value'] for point in trend['series'][0]['points']], [None, 10, None])
        service = TrackerQuery(lambda kb: True)
        result = json.loads(agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, params, 'excel'))
        self.assertEqual(result['analysis']['total_matched'], 1)
        self.assertTrue(validate_sources(service.evidence, self.owner.pk))

    def test_invalid_analysis_filters_are_rejected(self):
        self.row()
        for filters in ('{}', 'null', 'not json', json.dumps([{'field': 'Unknown', 'op': 'eq', 'value': 'x'}]),
                        json.dumps([{'field': 'Object', 'op': 'eq', 'value': 'A'}] * 13)):
            response = self.client.get(self.url, {**self.params, 'analysis_filters': filters})
            self.assertEqual(response.status_code, 400, response.content)

    def test_repeated_analysis_exhaustion_notices_cannot_exceed_shared_budget(self):
        service = TrackerQuery(lambda kb: True)
        service.remaining = 100
        with patch('kb.analysis_exports.query_analysis') as query:
            results = [agent_analysis(self.owner, lambda kb: True, service, self.kb.slug,
                                      self.params, 'preview') for _ in range(20)]
        query.assert_not_called()
        self.assertLessEqual(sum(map(len, results)), 100)
        self.assertLessEqual(service.remaining, 1)  # One char cannot hold another valid JSON result.
        self.assertGreaterEqual(service.remaining, 0)

    def test_file_fingerprint_is_verified_and_links_expire(self):
        self.row()
        service = TrackerQuery(lambda kb: True)
        result = json.loads(agent_analysis(self.owner, lambda kb: True, service, self.kb.slug, self.params, 'excel'))
        self.assertTrue(validate_sources(service.evidence, self.owner.pk))
        file = next((Path(self.directory.name) / 'analysis_exports').glob('*.xlsx'))
        file.write_bytes(b'changed file')
        self.assertFalse(validate_sources(service.evidence, self.owner.pk))
        import time
        with patch('django.core.signing.time.time', return_value=time.time() + 86401):
            self.assertEqual(self.client.get(result['files'][0]['url']).status_code, 404)

    def test_manual_tracksheet_uses_document_scope_and_exports(self):
        self.feed.delete()
        self.kb.category = 'document'
        self.kb.save(update_fields=['category'])
        child = KnowledgeBase.objects.create(name='File', slug='export-doc', parent=self.kb)
        doc = Document.objects.create(kb=child, original_name='Test.txt', status='completed')
        TrackerRow.objects.create(tracker=self.tracker, document=doc, status='done',
                                  values={'Object': 'Manual', 'Number': '5'})
        url = reverse('kb:tracker_kb_data', args=[self.kb.slug])
        book = self.workbook(self.client.get(url, {**self.params, 'export': 'xlsx'}))
        self.assertEqual(list(book['Analysis'].values)[1], ('Manual', 5, 1))
        _, result = query_analysis(self.owner, self.kb.slug, self.params)
        self.assertEqual(result['total_matched'], 1)

    def test_agent_exposes_tool_in_enhanced_mode_with_real_downloads(self):
        self.row()
        evidence, citations = [], []
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'):
            _build_agent('', 'export-tools', {'qa_enhance': True}, 5, None,
                         relationship_user_id=self.owner.pk, evidence=evidence, citations=citations)
        tools = {tool.__name__: tool for tool in create.call_args.kwargs['tools']}
        self.assertNotIn('write_analysis', tools)
        from langchain_core.utils.function_calling import convert_to_openai_tool
        schema = convert_to_openai_tool(tools['tracker_analyze'])['function']['parameters']
        self.assertIn('both', schema['properties']['output']['enum'])
        self.assertIn('filters', schema['properties'])
        self.assertIn('query', schema['properties'])
        result = json.loads(tools['tracker_analyze'](self.kb.slug, group='Object', operation='sum',
                                                    measure='Number', output='excel'))
        self.assertEqual(result['analysis']['groups'][0]['metrics']['value'], 3)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(citations[0]['kind'], 'tracker')
        self.assertTrue(validate_sources(evidence, self.owner.pk))
        result = json.loads(tools['tracker_analyze'](self.kb.slug, group='Object', query='A',
            filters=[{'field': 'Number', 'op': 'gte', 'type': 'number', 'value': 5}]))
        self.assertEqual(result['analysis']['total_matched'], 0)
