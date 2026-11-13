"""Hidden business fields must never enter EnGenie's structured query context."""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from kb.models import KbTracker, KnowledgeBase, TrackerRow
from kb.tracker_query import TrackerQuery, validate_snapshot


class HiddenFieldQueryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('hidden-query-reader')
        self.kb = KnowledgeBase.objects.create(
            name='Selected Business Data', slug='selected-business-data', category='business',
            active_fields=['Name', 'Count'])
        self.tracker = KbTracker.objects.create(kb=self.kb, enabled=True,
            fields=[{'label': label} for label in ('Name', 'Count', 'InternalNote', 'UnusedNumber')])
        self.original = {'Name': 'Visible Ride', 'Count': 7,
                         'InternalNote': 'HIDDEN_NOTE_SENTINEL', 'UnusedNumber': 91234}
        self.row = TrackerRow.objects.create(tracker=self.tracker, status='done', values=self.original)

    def service(self):
        return TrackerQuery(lambda kb: True)

    def query(self, method='lookup', *, kb_slug=None, **params):
        return json.loads(self.service().call(method, kb_slug=kb_slug or self.kb.slug, **params))

    def test_preview_schema_counts_pagination_and_samples_use_only_selected_fields(self):
        result = self.query('preview')
        table = result['tables'][0]
        self.assertEqual([field['name'] for field in table['fields']], ['Name', 'Count'])
        self.assertEqual(table['field_count'], 2)
        self.assertEqual(table['samples'][0]['values'], {'Name': 'Visible Ride', 'Count': 7})
        wire = json.dumps(result)
        for hidden in ('InternalNote', 'UnusedNumber', 'HIDDEN_NOTE_SENTINEL', '91234'):
            self.assertNotIn(hidden, wire)
        later = self.query('preview', field_offset=1)['tables'][0]
        self.assertEqual([field['name'] for field in later['fields']], ['Count'])
        self.assertIsNone(later['next_field_offset'])

    def test_default_rows_and_full_export_preserve_only_selected_values(self):
        result = self.query()
        self.assertEqual(result['columns'], ['Name', 'Count'])
        self.assertEqual(result['rows'][0]['values'], {'Name': 'Visible Ride', 'Count': 7})
        self.assertEqual(result['unselected_columns'], 0)
        exported = self.service().lookup(kb_slug=self.kb.slug, _export=True)
        self.assertEqual(exported['rows'][0]['values'], {'Name': 'Visible Ride', 'Count': 7})
        self.row.refresh_from_db()
        self.assertEqual(self.row.values, self.original)

    def test_free_text_search_cannot_match_hidden_values(self):
        self.assertEqual(self.query(query='HIDDEN_NOTE_SENTINEL')['total_matched'], 0)
        self.assertEqual(self.query(query='91234')['total_matched'], 0)
        visible = self.query(query='Visible Ride')
        self.assertEqual(visible['total_matched'], 1)
        self.assertEqual(visible['rows'][0]['row_id'], self.row.pk)

    def test_hidden_fields_cannot_be_explicitly_requested_filtered_sorted_or_aggregated(self):
        requests = [
            {'columns': ['InternalNote']},
            {'filters': [{'field': 'InternalNote', 'op': 'contains', 'value': 'HIDDEN_NOTE_SENTINEL'}]},
            {'order_by': [{'field': 'UnusedNumber', 'type': 'number'}]},
            {'metrics': [{'op': 'sum', 'field': 'UnusedNumber'}]},
            {'metrics': [{'op': 'distinct_count', 'field': 'InternalNote'}]},
            {'group_by': [{'field': 'InternalNote'}]},
        ]
        for params in requests:
            with self.subTest(params=params):
                result = self.query(**params)
                self.assertIn('error', result)
                self.assertNotIn('rows', result)
                self.assertNotIn('HIDDEN_NOTE_SENTINEL', json.dumps(result))
        result = self.query(metrics=[{'op': 'sum', 'field': 'Count'}])
        self.assertEqual(result['groups'][0]['metrics']['sum'], 7)

    def test_hiding_invalidates_old_snapshot_and_unhiding_restores_saved_originals(self):
        self.kb.active_fields = None
        self.kb.save(update_fields=['active_fields'])
        service = self.service()
        result = json.loads(service.call('lookup', kb_slug=self.kb.slug, columns=['InternalNote', 'UnusedNumber']))
        self.assertEqual(result['rows'][0]['values']['InternalNote'], 'HIDDEN_NOTE_SENTINEL')
        snapshot = service.evidence[0]
        self.assertTrue(validate_snapshot(snapshot, self.user))
        self.kb.active_fields = ['Name', 'Count']
        self.kb.save(update_fields=['active_fields'])
        self.assertFalse(validate_snapshot(snapshot, self.user))
        self.kb.active_fields = None
        self.kb.save(update_fields=['active_fields'])
        restored = self.query(columns=['InternalNote', 'UnusedNumber'])
        self.assertEqual(restored['rows'][0]['values'],
                         {'InternalNote': 'HIDDEN_NOTE_SENTINEL', 'UnusedNumber': 91234})
        self.row.refresh_from_db()
        self.assertEqual(self.row.values, self.original)

    def test_manual_document_trackers_ignore_inapplicable_selection_but_table_query_mode_honors_it(self):
        self.kb.category = 'manual'
        self.kb.active_fields = ['Name']
        self.kb.save(update_fields=['category', 'active_fields'])
        manual = self.query('preview')['tables'][0]
        self.assertEqual(manual['field_count'], 4)
        self.assertIn('InternalNote', [field['name'] for field in manual['fields']])
        self.kb.table_query = True
        self.kb.save(update_fields=['table_query'])
        table = self.query('preview')['tables'][0]
        self.assertEqual([field['name'] for field in table['fields']], ['Name'])
        self.assertIn('error', self.query(columns=['InternalNote']))


class MultiTrackerHiddenFieldTests(TestCase):
    def setUp(self):
        self.folder = KnowledgeBase.objects.create(name='Mixed Tables', slug='mixed-tables',
            category='business', is_folder=True)
        first = KnowledgeBase.objects.create(name='Visible Shared Field', slug='visible-shared-field',
            parent=self.folder, category='business', active_fields=['Name', 'Shared'])
        second = KnowledgeBase.objects.create(name='Hidden Shared Field', slug='hidden-shared-field',
            parent=self.folder, category='business', active_fields=['Name'])
        schema = [{'label': label} for label in ('Name', 'Shared')]
        self.visible_tracker = KbTracker.objects.create(kb=first, fields=schema)
        self.hidden_tracker = KbTracker.objects.create(kb=second, fields=schema)
        self.visible_row = TrackerRow.objects.create(tracker=self.visible_tracker, status='done',
            values={'Name': 'Visible Table Row', 'Shared': 5})
        self.hidden_row = TrackerRow.objects.create(tracker=self.hidden_tracker, status='done',
            values={'Name': 'Hidden Table Row', 'Shared': 987654321})

    def query(self, **params):
        return json.loads(TrackerQuery(lambda kb: True).call('lookup', kb_slug=self.folder.slug, **params))

    def test_union_of_schema_labels_never_projects_or_searches_hidden_values_from_another_tracker(self):
        result = self.query(columns=['Name', 'Shared'])
        rows = {row['row_id']: row for row in result['rows']}
        self.assertEqual(rows[self.visible_row.pk]['values']['Shared'], 5)
        self.assertIsNone(rows[self.hidden_row.pk]['values'].get('Shared'))
        self.assertEqual(self.query(query='987654321')['total_matched'], 0)
        self.assertEqual(self.query(query='Visible Table Row')['total_matched'], 1)
        self.assertNotIn('987654321', json.dumps(result))
        self.hidden_row.refresh_from_db()
        self.assertEqual(self.hidden_row.values['Shared'], 987654321)

    def test_filters_aggregates_and_groups_use_per_tracker_visibility(self):
        filtered = self.query(columns=['Name', 'Shared'],
            filters=[{'field': 'Shared', 'op': 'gte', 'value': 0, 'type': 'number'}])
        self.assertEqual(filtered['total_matched'], 1)
        self.assertEqual(filtered['rows'][0]['row_id'], self.visible_row.pk)
        total = self.query(metrics=[{'op': 'sum', 'field': 'Shared', 'as': 'total'}])
        self.assertEqual(total['groups'][0]['metrics']['total'], 5)
        self.assertEqual(total['groups'][0]['valid_numeric_values']['total'], 1)
        grouped = self.query(group_by=[{'field': 'Shared'}])
        self.assertNotIn('987654321', json.dumps(grouped))
        self.assertTrue(any(str(group['by'].get('Shared')) == '5' for group in grouped['groups']))
