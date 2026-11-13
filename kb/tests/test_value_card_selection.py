"""A broad retrieval can be presented directly, without copying invented values."""
import copy
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from kb.access import kb_accessible
from kb.models import TrackerRow
from kb.publication import digest, validate_sources, visible_citations
from kb.tracker_query import TrackerQuery, encode, validate_snapshot
from kb.tests.test_value_cards import ValueCardFixture


class FlexibleValueCardTests(ValueCardFixture, TestCase):
    def setUp(self):
        super().setUp()
        names = ['Temperature', 'Pressure', 'Date', 'Location', 'Equipment ID', 'Year',
                 'Hours', 'Duration', 'Description', 'Hidden']
        self.kb.active_fields = names[:-1]
        self.kb.visibility = 'individual'
        self.kb.created_by = self.user
        self.kb.save(update_fields=['active_fields', 'visibility', 'created_by'])
        self.tracker.fields = [{'label': field} for field in names]
        self.tracker.save(update_fields=['fields'])
        self.row.values.update(Pressure=101.25, **{'Equipment ID': 42, 'Year': 2026,
            'Hours': '09:00–18:00', 'Duration': '45 min', 'Description': 'Long source text. ' * 40})
        self.row.save(update_fields=['values'])

    def service(self):
        return TrackerQuery(lambda kb: kb_accessible(self.user, kb))

    def lookup(self, service, **extra):
        params = {'kb_slug': self.kb.slug, 'columns': ['Temperature', 'Pressure', 'Date', 'Location'],
                  'presentation': 'text', 'presentation_version': 2, **extra}
        return json.loads(service.call('lookup', **params))

    def show(self, service, field='Temperature', **extra):
        return json.loads(service.show_value(field=field, **extra))

    def test_first_four_column_lookup_auto_selects_the_business_value_in_both_modes(self):
        for enhanced in (False, True):
            with self.subTest(enhanced=enhanced):
                tools, citations, evidence, _ = self.tools(enhanced)
                result = json.loads(tools['tracker_lookup'](kb_slug=self.kb.slug,
                    columns=['Equipment ID', 'Date', 'Location', 'Temperature'],
                    card_title='Shanghai temperature', unit='°C'))
                self.assertEqual(result['chart'], {'type': 'metric', 'title': 'Shanghai temperature', 'value': 0, 'unit': '°C'})
                self.assertEqual(result['rows'][0]['values']['Equipment ID'], 42)
                self.assertTrue(validate_sources(evidence, self.user.pk))
                self.assertTrue(visible_citations(citations, self.user))

    def test_numeric_year_and_id_metadata_do_not_become_an_unrequested_card(self):
        service = self.service()
        result = self.lookup(service, presentation='auto', columns=['Equipment ID', 'Year', 'Date', 'Location'])
        self.assertNotIn('chart', result)
        relevant = self.lookup(service, presentation='auto', columns=['Equipment ID', 'Year', 'Temperature', 'Location'])
        self.assertEqual(relevant['chart']['value'], 0)

    def test_explicit_card_field_selects_among_multiple_values_without_losing_context(self):
        service = self.service()
        broad = self.lookup(service, presentation='auto')
        self.assertNotIn('chart', broad)
        selected = self.lookup(service, presentation='auto', card_field='Pressure',
                               card_title='Shanghai pressure', unit='kPa')
        self.assertEqual(selected['chart'], {'type': 'metric', 'title': 'Shanghai pressure', 'value': 101.25, 'unit': 'kPa'})
        self.assertEqual(selected['rows'][0]['values']['Location'], 'Shanghai')
        self.assertTrue(validate_snapshot(service.evidence[-1], self.user))

    def test_requested_numeric_identifier_can_be_presented_explicitly(self):
        service = self.service()
        selected = self.lookup(service, presentation='auto', columns=['Equipment ID', 'Location'],
                               card_field='Equipment ID', card_title='Equipment number')
        self.assertEqual(selected['chart']['value'], 42)

    def test_a_returned_row_can_be_selected_but_a_first_page_is_not_automatically_an_answer(self):
        second = TrackerRow.objects.create(tracker=self.tracker, status='done',
            values={'Temperature': 19.5, 'Pressure': 100, 'Date': '2026-11-12', 'Location': 'Beijing'})
        service = self.service()
        sampled = self.lookup(service, presentation='auto', limit=1, card_field='Temperature')
        self.assertNotIn('chart', sampled)
        selected = self.lookup(service, presentation='auto', card_field='Temperature', card_row_id=second.pk)
        self.assertEqual(selected['chart']['value'], 19.5)
        self.assertTrue(validate_snapshot(service.evidence[-1], self.user))

    def test_deliberately_latest_query_can_keep_all_context_columns(self):
        TrackerRow.objects.create(tracker=self.tracker, status='done',
            values={'Temperature': 21, 'Date': '2026-11-12', 'Location': 'Shanghai', 'Equipment ID': 42})
        result = self.lookup(self.service(), presentation='auto',
            columns=['Equipment ID', 'Date', 'Location', 'Temperature'], limit=1,
            order_by=[{'field': 'Date', 'direction': 'desc'}])
        self.assertEqual(result['chart']['value'], 0)
        self.assertEqual(result['total_matched'], 2)

    def test_result_id_is_returned_as_metadata_and_old_query_validation_remains_valid(self):
        service = self.service()
        response = self.lookup(service)
        self.assertTrue(response['result_id'])
        snapshot = service.evidence[-1]
        self.assertNotIn('result_id', json.loads(snapshot['text']))
        self.assertTrue(validate_snapshot(snapshot, self.user))
        legacy = self.service()
        old = json.loads(legacy.call('lookup', kb_slug=self.kb.slug,
            columns=['Temperature', 'Location'], presentation='auto'))
        self.assertNotIn('chart', old)
        self.assertTrue(validate_snapshot(legacy.evidence[-1], self.user))

    def test_legacy_numeric_aggregate_keeps_the_original_saved_presentation_payload(self):
        legacy_notice = ('This value is already displayed as a compact card above the answer. '
                         'For a simple exact-answer question, return only the assigned [N] citation; '
                         'the card is the answer. Do not add prose, insights, caveats, a table, or repeat the value. '
                         'Explain further only when the user asks for explanation or the answer needs clarification.')
        for op, field in [('count', ''), ('sum', 'Temperature'), ('avg', 'Pressure'), ('max', 'Pressure')]:
            with self.subTest(op=op, field=field):
                service = self.service()
                metric = {'op': op, 'as': 'Legacy metric'}
                if field:metric['field'] = field
                result = json.loads(service.call('lookup', kb_slug=self.kb.slug,
                    metrics=[metric], presentation='auto'))
                self.assertEqual(result['presentation_notice'], legacy_notice)
                # Construct the historical persisted payload independently of
                # the current presentation helper and replay its exact digest.
                snapshot = copy.deepcopy(service.evidence[-1])
                original = json.loads(snapshot['text'])
                original['presentation_notice'] = legacy_notice
                snapshot['text'] = encode(original)
                snapshot['result_digest'] = digest(snapshot['text'])
                self.assertTrue(validate_snapshot(snapshot, self.user))

    def test_show_value_selects_the_actual_saved_result_and_defaults_to_the_latest_lookup(self):
        service = self.service()
        original = self.lookup(service)
        first = self.show(service, result_id=original['result_id'], title='Pressure', field='Pressure', unit='kPa')
        self.assertEqual(first['chart']['value'], 101.25)
        latest = self.show(service, title='Temperature')
        self.assertEqual(latest['chart']['value'], 0)
        self.assertTrue(validate_sources(service.evidence, self.user.pk))

    def test_saved_value_can_be_presented_after_the_raw_query_output_budget_is_spent(self):
        service = self.service()
        original = self.lookup(service)
        original_snapshot = copy.deepcopy(service.evidence[-1])
        service.remaining = 0
        before = service.presentation_remaining
        result = self.show(service, result_id=original['result_id'], title='Pressure', field='Pressure', unit='kPa')
        self.assertEqual(result['chart']['value'], 101.25)
        self.assertEqual(service.remaining, 0)
        self.assertLess(service.presentation_remaining, before)
        self.assertGreaterEqual(service.presentation_remaining, 0)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 2000)
        self.assertNotIn('rows', result)
        self.assertNotIn('groups', result)
        self.assertNotIn('Shanghai', json.dumps(result, ensure_ascii=False))
        self.assertEqual(service.evidence[0], original_snapshot)
        self.assertTrue(validate_snapshot(service.evidence[-1], self.user))
        service.presentation_remaining = 0
        exhausted = self.show(service, field='Temperature')
        self.assertIn('error', exhausted)
        self.assertNotIn('chart', exhausted)
        self.assertEqual(service.remaining, 0)

    def test_answer_verification_uses_selected_card_while_preserving_original_source_evidence(self):
        from kb.qa_steps import _answer_with_value_cards
        service = self.service()
        original = self.lookup(service, presentation='auto', card_field='Temperature',
                               card_title='Original automatic temperature')
        self.assertEqual(original['chart']['value'], 0)
        chosen = self.show(service, result_id=original['result_id'], field='Pressure',
                           title='Selected Shanghai pressure', unit='kPa')
        self.assertEqual(chosen['chart']['value'], 101.25)
        self.assertEqual(len(service.evidence), 2)
        self.assertEqual(service.evidence[0]['chart']['title'], 'Original automatic temperature')
        self.assertEqual(service.evidence[0]['result_digest'], original['result_id'])
        verification = _answer_with_value_cards('[1]', service.evidence)
        self.assertIn('Selected Shanghai pressure', verification)
        self.assertIn('101.25', verification)
        self.assertNotIn('Original automatic temperature', verification)
        self.assertTrue(validate_sources(service.evidence, self.user.pk))

    def test_two_selected_values_publish_distinct_verified_cards_and_citations_in_both_modes(self):
        for enhanced in (False, True):
            with self.subTest(enhanced=enhanced):
                tools, citations, evidence, _ = self.tools(enhanced)
                result = json.loads(tools['tracker_lookup'](kb_slug=self.kb.slug,
                    columns=['Temperature', 'Pressure', 'Date', 'Location'], presentation='text'))
                self.assertNotIn('chart', result)
                temperature = json.loads(tools['tracker_show_value'](field='Temperature',
                    result_id=result['result_id'], title='Temperature', unit='°C'))
                pressure = json.loads(tools['tracker_show_value'](field='Pressure',
                    result_id=result['result_id'], title='Pressure', unit='kPa'))
                self.assertEqual(temperature['chart']['value'], 0)
                self.assertEqual(pressure['chart']['value'], 101.25)
                selected = [item for item in citations if item['tracker_snapshot'].get('chart')]
                self.assertEqual(len(selected), 2)
                self.assertNotEqual(selected[0]['result_digest'], selected[1]['result_digest'])
                self.assertTrue(validate_sources(evidence, self.user.pk))
                self.assertTrue(visible_citations(citations, self.user))

    def test_show_value_rejects_unknown_results_unreturned_fields_and_rows(self):
        second = TrackerRow.objects.create(tracker=self.tracker, status='done',
            values={'Temperature': 25, 'Location': 'Beijing'})
        for selection in (dict(field='Hidden'), dict(field='Pressure'),
                          dict(field='Temperature', row_id=second.pk),
                          dict(field='Temperature', result_id='invented-result-id')):
            with self.subTest(selection=selection):
                service = self.service()
                self.lookup(service, columns=['Temperature', 'Location'],
                    filters=[{'field': 'Location', 'op': 'eq', 'value': 'Shanghai'}])
                self.assertIn('error', self.show(service, **selection))

    def test_multi_row_selection_requires_the_requested_returned_row(self):
        second = TrackerRow.objects.create(tracker=self.tracker, status='done',
            values={'Temperature': 19.5, 'Pressure': 100, 'Date': '2026-11-12', 'Location': 'Beijing'})
        service = self.service()
        self.lookup(service)
        self.assertIn('error', self.show(service))
        result = self.show(service, row_id=second.pk, title='Beijing temperature')
        self.assertEqual(result['chart']['value'], 19.5)

    def test_grouped_metric_selection_uses_exact_returned_by_values(self):
        TrackerRow.objects.create(tracker=self.tracker, status='done',
            values={'Temperature': 19.5, 'Pressure': 100, 'Location': 'Beijing'})
        service = self.service()
        response = self.lookup(service, metrics=[{'op': 'sum', 'field': 'Pressure', 'as': 'Total pressure'}],
            group_by=[{'field': 'Location'}])
        self.assertIn('error', self.show(service, field='Total pressure'))
        result = self.show(service, field='Total pressure', result_id=response['result_id'],
                           group={'Location': 'Shanghai'}, title='Shanghai pressure')
        self.assertEqual(result['chart']['value'], 101.25)
        self.assertIn('error', self.show(service, field='Total pressure', group={'Location': 'Unknown'}))
        self.assertTrue(validate_sources(service.evidence, self.user.pk))

    def test_explicit_short_text_values_keep_their_source_text_and_embedded_unit(self):
        for field, expected in [('Hours', '09:00–18:00'), ('Duration', '45 min')]:
            with self.subTest(field=field):
                service = self.service()
                self.lookup(service, columns=['Location', field])
                result = self.show(service, field=field, title=field)
                self.assertEqual(result['chart']['value'], expected)
                self.assertEqual(result['chart']['value_type'], 'text')
                self.assertNotIn('unit', result['chart'])
                self.assertTrue(validate_snapshot(service.evidence[-1], self.user))

    def test_truncated_missing_boolean_and_nonfinite_cells_are_not_presentable(self):
        service = self.service()
        self.lookup(service, columns=['Description'], cell_chars=80)
        self.assertIn('error', self.show(service, field='Description'))
        for value in (None, False, 'NaN', 'Infinity', '1e10000'):
            with self.subTest(value=value):
                self.row.values['Temperature'] = value
                self.row.save(update_fields=['values'])
                service = self.service()
                self.lookup(service)
                self.assertIn('error', self.show(service))

    def test_pending_source_rows_cannot_be_promoted_to_verified_cards(self):
        self.row.status = 'pending'
        self.row.save(update_fields=['status'])
        service = self.service()
        self.lookup(service, status='pending')
        self.assertIn('error', self.show(service))

    def test_card_source_validation_rejects_changed_forged_hidden_and_unauthorized_results(self):
        service = self.service()
        self.lookup(service)
        self.show(service)
        snapshot = service.evidence[-1]
        self.assertTrue(validate_snapshot(snapshot, self.user))
        forged = copy.deepcopy(snapshot)
        forged['chart']['value'] = 999
        self.assertFalse(validate_snapshot(forged, self.user))
        other = get_user_model().objects.create_user('card-unrelated-reader')
        self.assertFalse(validate_snapshot(snapshot, other))
        self.row.values['Temperature'] = 23
        self.row.save(update_fields=['values'])
        self.assertFalse(validate_snapshot(snapshot, self.user))
        self.row.values['Temperature'] = 0
        self.row.save(update_fields=['values'])
        fresh = self.service()
        self.lookup(fresh)
        self.show(fresh)
        current_snapshot = fresh.evidence[-1]
        self.assertTrue(validate_snapshot(current_snapshot, self.user))
        self.kb.active_fields = ['Pressure', 'Date', 'Location']
        self.kb.save(update_fields=['active_fields'])
        self.assertFalse(validate_snapshot(current_snapshot, self.user))
