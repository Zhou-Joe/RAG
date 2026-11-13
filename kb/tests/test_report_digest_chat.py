"""Digest-only cron chat edits preserve the report and its confirmation boundary."""
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from langchain_core.messages import AIMessageChunk

from kb.connector_agent import _digest_report, run
from kb.connector_context import normalize_arguments
from kb.models import ConnectorDraft, FeedReportPlan, KnowledgeBase, KnowledgeFeed
from kb.tests.test_connector import FakeModel, call


class ReportDigestChatTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('digest-chat-owner', is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='Ride Operations', slug='digest-chat',
                                               is_folder=True, created_by=self.user)
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name=self.kb.name,
            created_by=self.user, mode='push', cron='*/30 * * * *',
            adapter_spec={'url': 'https://example.test/operations'})
        self.report = {'kind': 'report', 'title': self.kb.name,
            'columns': [{'label': 'Ride', 'field': 'Ride'}, {'label': 'Wait', 'field': 'Wait'}],
            'range': {'mode': 'days', 'days': 7, 'field': 'Date'},
            'filters': [{'field': 'Wait', 'op': 'gte', 'value': 0}],
            'group_by': [], 'aggregates': [],
            'schedule': {'mode': 'cron', 'cron': '0 9 * * mon', 'timezone': 'Asia/Shanghai'},
            'trigger': {'condition': {'field': 'Wait', 'op': 'gt', 'value': 30},
                        'match': 'any', 'notify': 'on_enter', 'cooldown_minutes': 60},
            'delivery': {'kind': 'teams'}}
        self.plan = FeedReportPlan.objects.create(feed=self.feed, created_by=self.user,
            spec=self.report, enabled=True, schedule_mode='cron', cron='0 9 * * mon')
        self.parent = ConnectorDraft.objects.create(user=self.user, kb=self.kb, feed=self.feed,
            status='enabled', prompt='Collect ride operations', spec={'url': 'https://example.test/operations'})
        self.job = ConnectorDraft.objects.create(user=self.user, kb=self.kb, parent=self.parent,
            status='running', prompt='Collect ride operations', feedback='Add an English digest.',
            response_language='en')

    def digest(self, **extra):
        return {'enabled': True, 'instructions': 'Highlight average wait and number of rides.',
            'metrics': [{'op': 'avg', 'field': 'Wait', 'label': 'Average wait'},
                        {'op': 'distinct_count', 'field': 'Ride', 'label': 'Ride count'}],
            'group_by': ['Ride'], **extra}

    def test_digest_only_update_uses_approved_report_and_preserves_every_other_setting(self):
        candidate = _digest_report(self.job, self.digest())
        self.assertEqual({key: value for key, value in candidate.items() if key != 'digest'}, self.report)
        self.assertEqual(candidate['digest']['language'], 'en')
        self.assertEqual(candidate['digest']['metrics'][0]['field'], 'Wait')
        self.plan.refresh_from_db()
        self.assertNotIn('digest', self.plan.spec)

    def test_latest_report_draft_takes_precedence_over_an_older_approved_plan(self):
        latest = deepcopy(self.report)
        latest['range'] = {'mode': 'dates', 'start': '2026-10-01', 'end': '2026-10-31', 'field': 'Date'}
        self.parent.spec = latest
        self.parent.status = 'ready'
        self.parent.save(update_fields=['spec', 'status'])
        candidate = _digest_report(self.job, self.digest())
        self.assertEqual(candidate['range'], latest['range'])
        self.assertEqual(candidate['trigger'], self.report['trigger'])
        self.assertEqual(candidate['delivery'], self.report['delivery'])

    def test_live_plan_changes_are_not_reverted_by_an_enabled_ancestor_snapshot(self):
        from kb.connector_context import conversation_context
        self.parent.spec = deepcopy(self.report)
        self.parent.status = 'enabled'
        self.parent.save(update_fields=['spec', 'status'])
        current = deepcopy(self.report)
        current['schedule']['cron'] = '15 10 * * *'
        current['digest'] = self.digest(instructions='This was changed in the schedule editor.')
        self.plan.spec = current
        self.plan.save(update_fields=['spec'])
        candidate = _digest_report(self.job, {'language': 'zh-hans'})
        self.assertEqual(candidate['schedule'], current['schedule'])
        self.assertEqual(candidate['digest']['instructions'], current['digest']['instructions'])
        self.assertEqual(conversation_context(self.job)['previous_report'], current)

    def test_partial_digest_edit_or_disable_preserves_existing_digest_options(self):
        initial = self.digest(language='zh-hans')
        self.plan.spec = {**self.report, 'digest': initial}
        self.plan.save(update_fields=['spec'])
        candidate = _digest_report(self.job, {'instructions': 'Focus on exceptions.'})
        self.assertEqual(candidate['digest'], {**initial, 'instructions': 'Focus on exceptions.'})
        disabled = _digest_report(self.job, {'enabled': False})
        self.assertFalse(disabled['digest']['enabled'])
        self.assertEqual(disabled['trigger'], self.report['trigger'])
        self.assertEqual(disabled['schedule'], self.report['schedule'])

    def test_prompt_and_link_edits_preserve_email_attachment_presentation(self):
        original = self.digest(language='en', include_source_links=True, email_format='html_attachment')
        self.plan.spec = {**deepcopy(self.report), 'delivery': {'kind': 'email', 'recipient': 'owner@example.test'},
                          'digest': original}
        self.plan.save(update_fields=['spec'])
        candidate = _digest_report(self.job, {'instructions': 'Explain only exceptional waits.'})
        self.assertEqual(candidate['digest'], {**original, 'instructions': 'Explain only exceptional waits.'})
        self.assertEqual(candidate['delivery'], self.plan.spec['delivery'])
        without_links = _digest_report(self.job, {'include_source_links': False})
        self.assertFalse(without_links['digest']['include_source_links'])
        self.assertEqual(without_links['digest']['email_format'], 'html_attachment')
        self.assertEqual(without_links['trigger'], self.report['trigger'])

    def test_model_schema_describes_links_and_email_only_html_attachment_without_raw_html(self):
        from kb.connector_schema import digest_schema
        properties = digest_schema()['properties']
        self.assertEqual(properties['include_source_links']['type'], 'boolean')
        self.assertFalse(properties['include_source_links']['default'])
        self.assertEqual(properties['email_format']['enum'], ['body', 'html_link', 'html_attachment'])
        self.assertEqual(properties['email_format']['default'], 'body')
        self.assertIn('html_attachment is email only', properties['email_format']['description'])
        self.assertNotIn('html', properties)
        self.assertNotIn('urls', properties)

    def test_generated_html_report_link_is_distinct_from_original_source_links_and_survives_edits(self):
        original = self.digest(email_format='html_link', include_source_links=False)
        self.plan.spec = {**deepcopy(self.report), 'delivery': {'kind': 'teams'}, 'digest': original}
        self.plan.save(update_fields=['spec'])
        candidate = _digest_report(self.job, {'instructions': 'Send a concise report link message.'})
        self.assertEqual(candidate['digest']['email_format'], 'html_link')
        self.assertFalse(candidate['digest']['include_source_links'])
        self.assertEqual(candidate['delivery'], {'kind': 'teams'})
        self.assertEqual(candidate['trigger'], self.report['trigger'])

    def test_chart_settings_survive_unrelated_digest_edits_and_can_be_explicitly_removed(self):
        charts = [{'title': 'Average Wait by Ride', 'type': 'combo', 'x': 'Ride',
            'series': [{'field': 'Wait', 'aggregate': 'avg', 'label': 'Average Wait', 'type': 'line', 'axis': 'left'},
                       {'aggregate': 'count', 'label': 'Records', 'type': 'bar', 'axis': 'right'}],
            'limit': 20, 'sort': 'value_desc', 'options': {'legend': True, 'smooth': True}}]
        original = self.digest(charts=charts, include_source_links=True, email_format='html_attachment')
        self.plan.spec = {**deepcopy(self.report), 'delivery': {'kind': 'email'}, 'digest': original}
        self.plan.save(update_fields=['spec'])
        edited = _digest_report(self.job, {'instructions': 'Explain changes briefly.'})
        self.assertEqual(edited['digest']['charts'], charts)
        self.assertEqual(edited['digest']['email_format'], 'html_attachment')
        disabled = _digest_report(self.job, {'charts': []})
        self.assertEqual(disabled['digest']['charts'], [])
        self.assertEqual(disabled['digest']['instructions'], original['instructions'])
        self.assertEqual(disabled['trigger'], self.report['trigger'])

    def test_chart_schema_only_exposes_supported_declarative_bindings(self):
        from kb.connector_schema import digest_schema
        chart_array = digest_schema()['properties']['charts']
        self.assertEqual(chart_array['maxItems'], 8)
        chart = chart_array['items']
        self.assertEqual(chart['properties']['type']['enum'], ['metric', 'line', 'bar', 'pie', 'combo', 'area', 'scatter', 'doughnut'])
        self.assertFalse(chart['additionalProperties'])
        self.assertNotIn('data', chart['properties'])
        self.assertNotIn('html', chart['properties'])
        self.assertNotIn('url', chart['properties'])
        series = chart['properties']['series']
        self.assertEqual(series['maxItems'], 6)
        self.assertEqual(series['items']['properties']['aggregate']['enum'], ['count', 'sum', 'avg', 'min', 'max'])

    def test_html_attachment_preview_waits_for_user_workflow_mapping_confirmation(self):
        proposed = self.digest(include_source_links=True, email_format='html_attachment')
        self.plan.spec = {**deepcopy(self.report), 'delivery': {'kind': 'email', 'recipient': 'owner@example.test'}}
        self.plan.save(update_fields=['spec'])
        model = FakeModel([call('preview_engenie_digest', {'digest': proposed})])

        def missing_mapping(job, spec):
            clean, preview = self.fake_preview(job, spec)
            preview['delivery'] = {'missing': ['html_attachment_mapping'], 'kind': 'email',
                'workflow_saved': True, 'html_attachments_supported': False,
                'settings_url': '/kb/feeds/?edit=current'}
            return clean, preview

        with patch('kb.agent._get_llm', return_value=model), \
                patch('kb.config.llm_settings', return_value={}), \
                patch('kb.feed_reports.preview', side_effect=missing_mapping), \
                patch('kb.feed_reports.post_workflow') as send:
            result = run(self.job.pk)
        self.assertEqual(result.status, 'needs_info', result.error)
        self.assertEqual(result.spec['digest']['email_format'], 'html_attachment')
        self.assertTrue(result.preview['setup_required'])
        self.assertIn('Name', result.preview['setup_hint'])
        self.assertIn('ContentBytes', result.preview['setup_hint'])
        self.assertEqual(result.preview['settings_url'], '/kb/feeds/?edit=current')
        send.assert_not_called()

    def test_new_report_can_be_supplied_without_replacing_collection_configuration(self):
        self.plan.delete()
        candidate = _digest_report(self.job, self.digest(), report=self.report)
        self.assertEqual(candidate['columns'], self.report['columns'])
        self.assertEqual(candidate['digest']['group_by'], ['Ride'])
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.cron, '*/30 * * * *')
        self.assertEqual(self.feed.adapter_spec['url'], 'https://example.test/operations')
        self.assertFalse(FeedReportPlan.objects.exists())

    def test_missing_report_and_foreign_ancestry_are_rejected(self):
        self.plan.delete()
        with self.assertRaisesRegex(ValueError, '报告字段'):
            _digest_report(self.job, self.digest())
        other = get_user_model().objects.create_user('digest-chat-other', is_staff=True)
        self.parent.user = other
        self.parent.save(update_fields=['user'])
        with self.assertRaisesRegex(ValueError, '不属于当前账号'):
            _digest_report(self.job, self.digest(), report=self.report)

    def fake_preview(self, job, spec):
        return {**deepcopy(spec), 'kind': 'report', 'title': self.kb.name}, {
            'valid': True, 'report': True, 'headers': ['Ride', 'Wait'], 'preview': [], 'items': 0,
            'delivery': {'missing': [], 'workflow_saved': True, 'kind': 'teams'},
            'digest_preview': {'text': 'Average waits are stable.'},
        }

    def test_real_chat_tool_registers_merges_and_stops_at_preview_without_sending(self):
        model = FakeModel([call('preview_engenie_digest', {'digest': self.digest()})])
        original_feed = (deepcopy(self.feed.adapter_spec), self.feed.cron)
        with patch('kb.agent._get_llm', return_value=model), \
                patch('kb.config.llm_settings', return_value={}), \
                patch('kb.feed_reports.preview', side_effect=self.fake_preview) as preview, \
                patch('kb.feed_reports.post_workflow') as post, \
                patch('kb.connector_agent.read_url') as recollect:
            result = run(self.job.pk)
        self.assertEqual(result.status, 'ready', result.error)
        self.assertEqual(result.spec['digest']['metrics'], self.digest()['metrics'])
        self.assertEqual(result.spec['trigger'], self.report['trigger'])
        self.assertEqual(result.preview['digest_preview']['text'], 'Average waits are stable.')
        preview.assert_called_once()
        post.assert_not_called()
        recollect.assert_not_called()
        self.feed.refresh_from_db()
        self.assertEqual((self.feed.adapter_spec, self.feed.cron), original_feed)
        self.plan.refresh_from_db()
        self.assertNotIn('digest', self.plan.spec)
        schema = model.tools['preview_engenie_digest']['function']['parameters']['properties']['digest']
        self.assertEqual(schema['properties']['group_by']['maxItems'], 2)
        self.assertIn('distinct_count', schema['properties']['metrics']['items']['properties']['op']['enum'])

    def test_scheduled_and_triggered_preview_tools_preserve_a_supplied_digest(self):
        for name in ('preview_scheduled_report', 'preview_triggered_report'):
            with self.subTest(tool=name):
                job = ConnectorDraft.objects.create(user=self.user, kb=self.kb, parent=self.parent,
                    status='running', prompt='Report operations', feedback='Add digest.')
                proposed = {**deepcopy(self.report), 'digest': self.digest()}
                model = FakeModel([call(name, {'report': proposed})])
                with patch('kb.agent._get_llm', return_value=model), \
                        patch('kb.config.llm_settings', return_value={}), \
                        patch('kb.feed_reports.preview', side_effect=self.fake_preview):
                    result = run(job.pk)
                self.assertEqual(result.status, 'ready', result.error)
                self.assertEqual(result.spec['digest'], proposed['digest'])

    def test_plain_digest_question_answers_without_creating_a_preview(self):
        self.job.feedback = 'What is EnGenie Digest?'
        self.job.save(update_fields=['feedback'])
        model = FakeModel([AIMessageChunk(content='It adds a summary to the report message.')])
        with patch('kb.agent._get_llm', return_value=model), \
                patch('kb.config.llm_settings', return_value={}), \
                patch('kb.feed_reports.preview') as preview:
            result = run(self.job.pk)
        self.assertEqual(result.status, 'answered', result.error)
        preview.assert_not_called()
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.spec, self.report)

    def test_double_encoded_digest_arguments_are_normalized_as_an_object(self):
        normalized = normalize_arguments({'digest': '{"enabled":false}', 'report': '{"columns":[]}'})
        self.assertEqual(normalized['digest'], {'enabled': False})
        self.assertEqual(normalized['report'], {'columns': []})

