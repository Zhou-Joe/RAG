"""Hidden business fields stay out of report-builder and digest model input."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from langchain_core.messages import AIMessageChunk, ToolMessage

from kb import feed_reports as reports
from kb.connector_agent import run
from kb.models import ConnectorDraft, FeedReportPlan, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow
from kb.tests.test_connector import FakeModel, call


@override_settings(FEED_REPORT_PUBLIC_BASE='https://reports.example.test')
class ReportActiveFieldsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('report-field-owner', is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='Operations', slug='report-active-fields',
            category='business', is_folder=True, created_by=self.user,
            active_fields=['Device', 'Wait', 'Items'])
        self.tracker = KbTracker.objects.create(kb=self.kb, enabled=True,
            fields=[{'label': label} for label in ('Hidden', 'Device', 'Wait', 'Items')])
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name=self.kb.name, created_by=self.user, mode='push')
        self.row = TrackerRow.objects.create(tracker=self.tracker, feed=self.feed, status='done',
            external_id='ride-1', values={'Hidden': 'PRIVATE_VALUE', 'Device': 'Ride A', 'Wait': 35,
                'Items': [{'amount': 2}, {'amount': 3}]})
        self.spec = {'kind': 'report', 'columns': [{'label': 'Device', 'field': 'Device'},
            {'label': 'Wait', 'field': 'Wait'}], 'range': {'mode': 'all'},
            'schedule': {'mode': 'manual'}, 'filters': [], 'group_by': [], 'aggregates': []}
        self.parent = ConnectorDraft.objects.create(user=self.user, kb=self.kb, feed=self.feed,
            status='enabled', spec={'body_fields': [{'label': 'Hidden', 'path': 'hidden'}]},
            preview={'records': [{'Hidden': 'PRIVATE_VALUE'}], 'answer': 'PRIVATE_VALUE'})
        self.job = ConnectorDraft.objects.create(user=self.user, kb=self.kb, parent=self.parent,
            status='running', prompt='Summarize the saved business records.', feedback='Show the visible data.',
            sample={'Hidden': 'PRIVATE_VALUE'}, response_language='en')

    def test_report_field_profiles_and_samples_exclude_hidden_names_and_values_without_mutating_rows(self):
        output = reports.available(self.feed)
        self.assertNotIn('Hidden', output['fields'])
        self.assertNotIn('Hidden', output['field_profiles'])
        self.assertNotIn('PRIVATE_VALUE', json.dumps(output))
        self.assertEqual(output['sample'][0]['Device'], 'Ride A')
        self.assertEqual(output['field_profiles']['Wait']['samples'], [35])
        self.row.refresh_from_db()
        self.assertEqual(self.row.values['Hidden'], 'PRIVATE_VALUE')

    def test_unhiding_restores_schema_and_samples_and_manual_libraries_keep_their_previous_behavior(self):
        self.kb.active_fields = None; self.kb.save(update_fields=['active_fields'])
        self.assertIn('Hidden', reports.available(self.feed)['fields'])
        self.kb.active_fields = ['Device']; self.kb.category = 'manual'
        self.kb.save(update_fields=['active_fields', 'category'])
        self.assertIn('Hidden', reports.available(self.feed)['fields'])

    def test_table_query_libraries_and_document_children_use_the_selected_field_policy(self):
        child = KnowledgeBase.objects.create(name='Child Document', slug='report-field-child', parent=self.kb)
        child_feed = KnowledgeFeed.objects.create(kb=child, name=child.name, created_by=self.user, mode='push')
        self.assertEqual(reports.report_visible_fields(child_feed), {'Device', 'Wait', 'Items'})
        self.kb.category = 'manual'; self.kb.table_query = True
        self.kb.save(update_fields=['category', 'table_query'])
        self.assertEqual(reports.report_visible_fields(self.feed), {'Device', 'Wait', 'Items'})

    def test_hidden_direct_columns_filters_and_date_fields_are_rejected_before_digest_or_delivery(self):
        candidates = [{**self.spec, 'columns': [{'label': 'H', 'field': 'Hidden'}]},
            {**self.spec, 'filters': [{'field': 'Hidden', 'op': 'contains', 'value': 'PRIVATE_VALUE'}]},
            {**self.spec, 'range': {'mode': 'all', 'field': 'Hidden'}}]
        for candidate in candidates:
            with self.subTest(candidate=candidate), patch('kb.report_digest.generate_digest') as digest:
                with self.assertRaises(ValueError):
                    reports.preview(self.job, candidate)
                digest.assert_not_called()

    def test_hidden_computed_references_are_rejected_in_root_pointer_and_duration_forms(self):
        for computed in (
            {'op': 'concat', 'args': [{'field': 'Hidden'}, '!']},
            {'op': 'concat', 'args': [{'field': '$root.Hidden'}, '!']},
            {'op': 'concat', 'args': [{'field': '/Hidden'}, '!']},
            {'op': 'duration', 'start': 'Hidden', 'end': 'Wait'},
            {'op': 'map', 'args': [{'field': 'Items'}, {'field': '$root.Hidden'}]},
        ):
            with self.subTest(computed=computed), self.assertRaisesRegex(ValueError, 'unavailable field'):
                reports.validate_report_calculation(computed, self.feed)

    def test_nested_computations_on_a_visible_field_remain_available(self):
        computed = {'op': 'sum', 'args': [{'op': 'map', 'args': [{'field': 'Items'}, {'field': 'amount'}]}]}
        spec = {**self.spec, 'columns': [{'label': 'Total', 'computed': computed}]}
        normalized = reports.normalize(spec, self.feed)
        self.assertEqual(reports.table(self.feed, normalized)[1], [{'Total': 5}])

    def test_stale_specs_fail_after_hiding_and_whole_record_computations_only_receive_visible_values(self):
        old = deepcopy(self.spec)
        old['columns'] = [{'label': 'Hidden', 'field': 'Hidden'}]
        with self.assertRaisesRegex(ValueError, 'unavailable field'):
            reports.table(self.feed, old)
        root = {**self.spec, 'columns': [{'label': 'Visible Record',
            'computed': {'op': 'concat', 'args': [{'field': '$root'}, '!']}}]}
        normalized = reports.normalize(root, self.feed)
        output = reports.table(self.feed, normalized)[1]
        self.assertNotIn('PRIVATE_VALUE', json.dumps(output))
        self.assertNotIn('Hidden', json.dumps(output))

    def test_previous_report_and_context_do_not_replay_hidden_fields_or_saved_examples(self):
        invalid = {**self.spec, 'columns': [{'label': 'Old Secret', 'field': 'Hidden'}]}
        FeedReportPlan.objects.create(feed=self.feed, created_by=self.user,
            spec={**invalid, 'digest': {'enabled': True, 'instructions': 'PRIVATE_VALUE'}})
        output = reports.available(self.feed)
        self.assertEqual(output['previous_report'], {})
        self.assertNotIn('PRIVATE_VALUE', json.dumps(output))
        raw = {'previous_report': invalid, 'previous_examples': {'Hidden': 'PRIVATE_VALUE'},
            'conversation': [{'assistant_answer': 'PRIVATE_VALUE'}], 'previous_connector': {'body_fields': ['Hidden']},
            'report_delivery': {'kind': 'email', 'digest': {'instructions': 'PRIVATE_VALUE'}}}
        projected = reports.engenie_report_context(self.feed, raw)
        self.assertNotIn('PRIVATE_VALUE', json.dumps(projected))
        self.assertNotIn('Hidden', json.dumps(projected))
        self.assertEqual(raw['previous_examples']['Hidden'], 'PRIVATE_VALUE')

    def test_real_cron_chat_tools_use_only_selected_fields_and_calculation_inputs(self):
        model = FakeModel([call('inspect_report_data'), call('preview_report_calculation',
            {'computed': {'op': 'add', 'args': [{'field': 'Wait'}, 1]}}, 'calculation'),
            AIMessageChunk(content='The visible wait is 35.')])
        with patch('kb.agent._get_llm', return_value=model), patch('kb.config.llm_settings', return_value={}):
            result = run(self.job.pk)
        self.assertEqual(result.status, 'answered', result.error)
        self.assertNotIn('PRIVATE_VALUE', repr(model.messages))
        self.assertNotIn('Hidden', repr(model.messages))
        tools = [json.loads(message.content) for message in model.messages[-1] if isinstance(message, ToolMessage)]
        self.assertEqual(tools[1]['examples'][0]['input'], {'Wait': 35})
        self.assertEqual(tools[1]['examples'][0]['value'], 36)
