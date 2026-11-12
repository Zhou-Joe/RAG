"""Condition checks use fresh saved data, with independent, duplicate-safe delivery."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from kb import feed_reports as reports
from kb.feeds import claim, execute
from kb.models import ConnectorDraft, FeedReportDelivery, KnowledgeBase, KnowledgeFeed
from kb.report_conditions import evaluate_trigger, normalize_trigger


URL = 'https://tenant.environment.api.powerplatform.com/powerautomate/workflows/fixture/triggers/manual/paths/invoke?sig=fixture'


class TriggerConditionTests(SimpleTestCase):
    def test_conditions_cover_numeric_text_and_nested_logic(self):
        trigger = normalize_trigger({'condition': {'all': [
            {'field': 'Wait', 'op': 'gt', 'value': 30},
            {'any': [{'field': 'State', 'op': 'contains', 'value': 'open'},
                     {'metric': 'row_count', 'op': 'gte', 'value': 2}]},
        ]}}, ['Wait', 'State'])
        result = evaluate_trigger(trigger, [{'Wait': '45', 'State': 'OPEN'}, {'Wait': '5', 'State': 'closed'}])
        self.assertTrue(result['matched'])
        self.assertEqual(result['matched_rows'], 1)
        self.assertFalse(evaluate_trigger({**trigger, 'match': 'all'}, [{'Wait': '45'}, {'Wait': '5'}])['matched'])

    def test_missing_and_nonfinite_values_never_act_as_zero(self):
        trigger = normalize_trigger({'condition': {'field': 'Wait', 'op': 'lte', 'value': 0}}, ['Wait'])
        for value in (None, '', 'not a number', 'NaN', float('nan'), float('inf'), True):
            with self.subTest(value=value):
                self.assertFalse(evaluate_trigger(trigger, [{'Wait': value}])['matched'])
        self.assertTrue(evaluate_trigger(trigger, [{'Wait': '0'}])['matched'])
        missing = normalize_trigger({'condition': {'field': 'Wait', 'op': 'is_missing'}}, ['Wait'])
        self.assertTrue(evaluate_trigger(missing, [{'Wait': None}])['matched'])

    def test_empty_all_does_not_match_but_explicit_zero_record_alert_does(self):
        trigger = normalize_trigger({'condition': {'field': 'Wait', 'op': 'is_missing'}, 'match': 'all'}, ['Wait'])
        self.assertFalse(evaluate_trigger(trigger, [])['matched'])
        trigger = normalize_trigger({'condition': {'metric': 'row_count', 'op': 'eq', 'value': 0}}, ['Wait'])
        self.assertTrue(evaluate_trigger(trigger, [])['matched'])

    def test_unbounded_unknown_or_invalid_conditions_are_rejected(self):
        invalid = [None, {}, {'condition': {'field': 'secret', 'op': 'eq', 'value': 'x'}},
                   {'condition': {'metric': 'row_count', 'op': 'gt', 'value': float('nan')}},
                   {'condition': {'field': 'Wait', 'op': 'gt', 'value': '30'}},
                   {'condition': {'any': []}},
                   {'condition': {'metric': 'row_count', 'op': 'gte', 'value': 1}, 'cooldown_minutes': True},
                   {'condition': {'field': 'Wait', 'op': 'is_missing', 'value': 0}},
                   {'condition': {'field': 'Wait', 'metric': 'row_count', 'op': 'eq', 'value': 1}}]
        nested = {'field': 'Wait', 'op': 'gt', 'value': 1}
        for _ in range(6):
            nested = {'all': [nested]}
        invalid.append({'condition': nested})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_trigger(value, ['Wait'])


@override_settings(FEED_REPORT_PUBLIC_BASE='')
class ReportTriggerTests(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        override = override_settings(DATA_DIR=Path(folder.name))
        override.enable()
        self.addCleanup(override.disable)
        self.user = get_user_model().objects.create_user('alert-owner', is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='Wait alerts', slug='wait-alerts', created_by=self.user, is_folder=True)
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name='Wait alerts', mode='push', write_mode='upsert', created_by=self.user)
        self.publish('45')
        self.plan = reports.configure(self.user, self.feed, {
            'kind': 'email', 'url': URL, 'recipient': 'fixture@example.com',
            'public_base': 'https://reports.example.com',
        })
        self.at = timezone.now().replace(second=0, microsecond=0)
        self.spec = {'kind': 'report', 'range': {'mode': 'all'},
                     'schedule': {'mode': 'cron', 'cron': '* * * * *', 'timezone': 'Asia/Shanghai'},
                     'columns': [{'label': 'Name', 'field': '标题'}, {'label': 'Wait', 'field': 'Wait'}],
                     'trigger': {'condition': {'field': 'Wait', 'op': 'gt', 'value': 30}}}

    def publish(self, wait):
        run, _ = claim(self.feed.pk, trigger='push', payload={'schema': 'fos-rag-feed/v1', 'items': [
            {'id': 'ride-1', 'title': 'Ride A', 'content': 'Wait time', 'fields': {'Wait': wait}},
        ]})
        return execute(run.pk)

    def enable(self, spec=None):
        self.plan.spec = reports.normalize(spec or self.spec, self.feed)
        self.plan.schedule_mode = 'cron'
        self.plan.cron = '* * * * *'
        self.plan.timezone = 'Asia/Shanghai'
        self.plan.enabled = True
        self.plan.next_run_at = self.at
        self.plan.save()

    def tick(self, minutes=0):
        at = self.at + timedelta(minutes=minutes)
        with patch('kb.feed_reports.timezone.now', return_value=at):
            return reports.run_due(at)

    def test_threshold_false_skips_without_workbook_or_workflow(self):
        self.spec['trigger']['condition']['value'] = 100
        self.enable()
        with patch('kb.feed_reports.post_workflow') as post, patch('kb.feed_reports.workbook') as workbook:
            result = self.tick()[0]
        self.assertEqual((result.status, result.error), ('skipped', 'condition_not_met'))
        post.assert_not_called()
        workbook.assert_not_called()
        self.assertFalse(result.content)

    def test_first_match_sends_once_across_repeated_and_later_ticks(self):
        self.enable()
        with patch('kb.feed_reports.post_workflow', return_value=202) as post:
            first = self.tick()[0]
            self.assertEqual(self.tick(), [])
            second = self.tick(1)[0]
        self.assertEqual(first.status, 'accepted')
        self.assertEqual((second.status, second.error), ('skipped', 'already_notified'))
        self.assertEqual(post.call_count, 1)
        self.plan.refresh_from_db()
        self.assertTrue(self.plan.trigger_state['matched'])
        self.assertIn('last_notified_at', self.plan.trigger_state)

    def test_false_then_true_rearms_with_fresh_data(self):
        self.feed.write_mode = 'upsert'
        self.feed.save(update_fields=['write_mode'])
        self.enable()
        with patch('kb.feed_reports.post_workflow', return_value=202) as post:
            self.tick()
            self.publish('5')
            self.assertEqual(self.tick(1)[0].error, 'condition_not_met')
            self.publish('60')
            self.assertEqual(self.tick(2)[0].status, 'accepted')
        self.assertEqual(post.call_count, 2)

    def test_every_match_obeys_cooldown(self):
        self.spec['trigger'].update(notify='every_match', cooldown_minutes=5)
        self.enable()
        with patch('kb.feed_reports.post_workflow', return_value=202) as post:
            self.tick()
            self.assertEqual(self.tick(1)[0].error, 'cooldown')
            self.assertEqual(self.tick(5)[0].status, 'accepted')
        self.assertEqual(post.call_count, 2)

    def test_new_enter_during_cooldown_sends_when_cooldown_expires(self):
        self.feed.write_mode = 'upsert'
        self.feed.save(update_fields=['write_mode'])
        self.spec['trigger']['cooldown_minutes'] = 5
        self.enable()
        with patch('kb.feed_reports.post_workflow', return_value=202) as post:
            self.tick()
            self.publish('5')
            self.tick(1)
            self.publish('60')
            self.assertEqual(self.tick(2)[0].error, 'cooldown')
            self.assertEqual(self.tick(5)[0].status, 'accepted')
        self.assertEqual(post.call_count, 2)

    def test_report_window_and_derived_aggregate_are_recomputed(self):
        spec = deepcopy(self.spec)
        spec['columns'] = [{'label': 'Minutes', 'computed': {'op': 'parse_number', 'args': [{'field': 'Wait'}]}}]
        spec['aggregates'] = [{'field': 'Minutes', 'op': 'avg', 'label': 'Average'}]
        spec['trigger']['condition'] = {'field': 'Average', 'op': 'gt', 'value': 30}
        self.enable(spec)
        with patch('kb.feed_reports.post_workflow', return_value=202) as post:
            self.assertEqual(self.tick()[0].status, 'accepted')
        self.assertEqual(post.call_count, 1)
        spec['range'] = {'mode': 'today'}
        self.plan.trigger_state = {}
        self.enable(spec)
        future = self.at + timedelta(days=2)
        self.plan.next_run_at = future
        self.plan.save(update_fields=['next_run_at'])
        with patch('kb.feed_reports.post_workflow') as post:
            self.assertEqual(reports.run_due(future)[0].error, 'condition_not_met')
        post.assert_not_called()

    def test_preview_never_sends_or_changes_automatic_state(self):
        self.enable()
        job = ConnectorDraft.objects.create(user=self.user, kb=self.kb, feed=self.feed)
        with patch('kb.feed_reports.post_workflow') as post:
            spec, preview = reports.preview(job, self.spec)
        post.assert_not_called()
        self.assertTrue(preview['trigger_evaluation']['matched'])
        self.assertIsNotNone(preview['next_check_at'])
        self.assertIsNone(preview['next_send_at'])
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.trigger_state, {})

    def test_manual_test_honors_condition_without_consuming_automatic_alert(self):
        self.enable()
        with patch('kb.feed_reports.post_workflow', return_value=202) as post:
            first = reports.send(self.plan, request_key='test-matching')
            self.assertEqual(self.tick()[0].status, 'accepted')
            spec = deepcopy(self.spec)
            spec['trigger']['condition']['value'] = 100
            skipped = reports.send(self.plan, spec=spec, request_key='test-unmatched')
        self.assertEqual(first.status, 'accepted')
        self.assertEqual((skipped.status, skipped.error), ('skipped', 'condition_not_met'))
        self.assertEqual(post.call_count, 2)

    def test_ambiguous_send_is_latched_and_never_automatically_retried(self):
        self.enable()
        with patch('kb.feed_reports.post_workflow', side_effect=TimeoutError) as post:
            self.assertEqual(self.tick()[0].status, 'unknown')
            self.assertEqual(self.tick(1)[0].error, 'already_notified')
        self.assertEqual(post.call_count, 1)

    def test_preparation_failure_releases_reservation_for_next_check(self):
        self.enable()
        with patch('kb.feed_reports.workbook', side_effect=ValueError('fixture invalid workbook')):
            self.assertEqual(self.tick()[0].status, 'failed')
        with patch('kb.feed_reports.post_workflow', return_value=202) as post:
            self.assertEqual(self.tick(1)[0].status, 'accepted')
        self.assertEqual(post.call_count, 1)

    def test_cancelled_preparation_cannot_resurrect_itself_when_artifact_is_saved(self):
        self.enable()

        def cancelled_workbook(*args):
            FeedReportDelivery.objects.filter(plan=self.plan, status='preparing').update(status='failed')
            return b'fixture workbook'

        with patch('kb.feed_reports.workbook', side_effect=cancelled_workbook), \
                patch('kb.feed_reports.post_workflow') as post:
            self.assertEqual(self.tick()[0].status, 'failed')
        post.assert_not_called()
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.trigger_state.get('notified_for_match'))

    def test_configuration_paused_during_preparation_prevents_post(self):
        self.enable()

        def paused_workbook(*args):
            type(self.plan).objects.filter(pk=self.plan.pk).update(enabled=False)
            return b'fixture workbook'

        with patch('kb.feed_reports.workbook', side_effect=paused_workbook), \
                patch('kb.feed_reports.post_workflow') as post:
            self.assertEqual(self.tick()[0].status, 'failed')
        post.assert_not_called()

    def test_in_progress_and_stale_occurrences_do_not_post(self):
        self.enable()
        busy = FeedReportDelivery.objects.create(plan=self.plan, request_key='older-check',
                                                scheduled_for=self.at - timedelta(seconds=30), status='preparing')
        with patch('kb.feed_reports.post_workflow') as post:
            self.assertEqual(self.tick()[0].error, 'check_in_progress')
        post.assert_not_called()
        busy.status = 'failed'
        busy.save(update_fields=['status'])
        self.plan.trigger_state = {'last_evaluated_for': (self.at + timedelta(minutes=3)).isoformat()}
        self.plan.save(update_fields=['trigger_state'])
        with patch('kb.feed_reports.post_workflow') as post:
            self.assertEqual(self.tick(1)[0].error, 'stale_check')
        post.assert_not_called()

    def test_revoked_access_and_configuration_change_prevent_delivery(self):
        self.enable()
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        with patch('kb.feed_reports.post_workflow') as post:
            self.assertEqual(self.tick()[0].status, 'failed')
        post.assert_not_called()

    def test_approval_resets_old_trigger_state_and_does_not_collect(self):
        self.enable()
        self.plan.trigger_state = {'matched': True, 'notified_for_match': True}
        self.plan.save(update_fields=['trigger_state'])
        job = ConnectorDraft.objects.create(user=self.user, kb=self.kb, feed=self.feed,
                                            status='ready', spec=self.spec, preview={'valid': True})
        with patch('kb.feed_reports.post_workflow') as post:
            reports.approve(job, self.user)
        post.assert_not_called()
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.trigger_state, {})

    def test_agent_previews_triggered_report_without_collection_or_sending(self):
        from kb.connector_agent import run
        from kb.tests.test_connector import FakeModel, call

        job = ConnectorDraft.objects.create(user=self.user, kb=self.kb, feed=self.feed,
                                            status='running', prompt='Alert when wait exceeds 30 minutes')
        model = FakeModel([call('preview_triggered_report', {'report': self.spec})])
        with patch('kb.agent._get_llm', return_value=model), patch('kb.config.llm_settings', return_value={}), \
                patch('kb.feed_reports.post_workflow') as post, patch('kb.feeds.execute') as collect:
            result = run(job.pk)
        self.assertEqual(result.status, 'ready', result.error)
        self.assertIn('trigger', result.spec)
        self.assertTrue(result.preview['trigger_evaluation']['matched'])
        post.assert_not_called()
        collect.assert_not_called()

    def test_skipped_test_send_is_a_successful_check_not_a_delivery_error(self):
        import json

        self.client.force_login(self.user)
        spec = deepcopy(self.spec)
        spec['trigger']['condition']['value'] = 100
        job = ConnectorDraft.objects.create(user=self.user, kb=self.kb, feed=self.feed,
                                            status='ready', spec=spec, preview={'valid': True})
        with patch('kb.feed_reports.post_workflow') as post:
            response = self.client.post(reverse('kb:report_test', args=[job.pk]),
                                        json.dumps({'request_key': 'fixture-trigger-test'}),
                                        content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['status'], 'skipped')
        self.assertTrue(response.json()['ok'])
        post.assert_not_called()

    def test_unconditional_existing_reports_keep_their_original_behavior(self):
        spec = deepcopy(self.spec)
        spec.pop('trigger')
        self.enable(spec)
        with patch('kb.feed_reports.post_workflow', return_value=202) as post:
            self.assertEqual(self.tick()[0].status, 'accepted')
            self.assertEqual(self.tick(1)[0].status, 'accepted')
        self.assertEqual(post.call_count, 2)
