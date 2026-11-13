from copy import deepcopy
from io import BytesIO
from pathlib import Path
import json
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from openpyxl import load_workbook

from kb import feed_reports as reports
from kb.models import ConnectorDraft, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow
from kb.report_conditions import normalize_trigger
from kb.report_messages import normalize_teams_card, report_title, teams_card_summary


WORKFLOW = 'https://tenant.environment.api.powerplatform.com/workflows/fixture/triggers/manual/paths/invoke?sig=fixture'


class CompactMessageTests(SimpleTestCase):
    def test_all_items_are_joined_and_aligned_in_three_chosen_fields(self):
        rows = [{'Name': f'Item {i}', 'Wait': i, 'State': None if i == 0 else 'Open', 'Unused': 'private'} for i in range(25)]
        spec = {'teams_card': {'fields': ['Name', 'Wait', 'State']}}
        result = teams_card_summary(spec, ['Unused', 'Name', 'Wait', 'State'], rows)
        self.assertEqual(len(result['facts']), 3)
        self.assertEqual(result['facts'][0]['value'], ', '.join(row['Name'] for row in rows))
        self.assertEqual(result['facts'][1]['value'], ', '.join(str(i) for i in range(25)))
        self.assertTrue(result['facts'][2]['value'].startswith('—, Open'))
        self.assertEqual(result['omitted_rows'], 0)
        self.assertNotIn('private', str(result))

    def test_only_rows_satisfying_the_full_nested_condition_are_presented(self):
        trigger = normalize_trigger({'condition': {'any': [
            {'all': [{'field': 'Wait', 'op': 'gte', 'value': 30}, {'field': 'State', 'op': 'eq', 'value': 'Open'}]},
            {'field': 'State', 'op': 'eq', 'value': 'Fault'},
        ]}}, ['Name', 'Wait', 'State'])
        rows = [{'Name': 'A', 'Wait': 35, 'State': 'Closed'}, {'Name': 'B', 'Wait': 40, 'State': 'Open'},
                {'Name': 'C', 'Wait': 5, 'State': 'Fault'}, {'Name': 'D', 'Wait': 3, 'State': 'Open'}]
        result = teams_card_summary({'trigger': trigger}, ['Name', 'Wait', 'State'], rows)
        self.assertEqual([fact['value'] for fact in result['facts']], ['B, C', '40, 5', 'Open, Fault'])
        self.assertEqual(len(rows), 4)

    def test_whole_result_conditions_and_all_row_mode_keep_their_semantics(self):
        rows = [{'Name': 'A'}, {'Name': 'B'}]
        trigger = normalize_trigger({'condition': {'metric': 'row_count', 'op': 'gte', 'value': 2}}, ['Name'])
        self.assertEqual(teams_card_summary({'trigger': trigger}, ['Name'], rows)['row_count'], 2)
        trigger = normalize_trigger({'condition': {'field': 'Name', 'op': 'eq', 'value': 'A'}, 'match': 'all'}, ['Name'])
        self.assertEqual(teams_card_summary({'trigger': trigger}, ['Name'], rows)['facts'], [])

    def test_overflow_uses_complete_rows_and_reports_the_omitted_count(self):
        rows = [{'Name': '设备' + str(i), 'Wait': i} for i in range(30)]
        result = teams_card_summary({}, ['Name', 'Wait'], rows, max_bytes=170)
        self.assertGreater(result['shown_rows'], 0)
        self.assertGreater(result['omitted_rows'], 0)
        self.assertEqual(result['shown_rows'] + result['omitted_rows'], 30)
        self.assertEqual(len(result['facts'][0]['value'].split(', ')), result['shown_rows'])
        self.assertLessEqual(len(json.dumps(result['facts'], ensure_ascii=False).encode('utf-8')), 170)
        self.assertEqual(teams_card_summary({}, ['Name'], [{'Name': 'x' * 200}], max_bytes=80)['shown_rows'], 0)

    def test_commas_quotes_and_missing_values_are_unambiguous(self):
        result = teams_card_summary({}, ['Name'], [{'Name': 'A, B'}, {'Name': 'C"D'}, {'Name': ''}, {'Name': 0}])
        self.assertEqual(result['facts'][0]['value'], '"A, B", "C""D", —, 0')

    def test_card_fields_are_final_output_columns_and_never_exceed_three(self):
        for value in ({'fields': []}, {'fields': ['A', 'A']}, {'fields': ['Missing']},
                      {'fields': ['A', 'B', 'C', 'D']}, {'fields': ['A'], 'data': [123]}):
            with self.assertRaises(ValueError):
                normalize_teams_card(value, ['A', 'B', 'C', 'D'])

    def test_titles_are_independent_single_lines_with_a_legacy_fallback(self):
        self.assertEqual(report_title('  Daily downtime  ', 'KB'), 'Daily downtime')
        self.assertEqual(report_title(None, 'KB'), 'KB')
        for title in ('', 'x' * 121, 'Alert\r\nBcc: injected', 123):
            with self.assertRaises(ValueError):
                report_title(title, 'KB')


@override_settings(FEED_REPORT_PUBLIC_BASE='https://reports.example.test')
class DeliveryPresentationTests(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = override_settings(DATA_DIR=Path(folder.name))
        config.enable()
        self.addCleanup(config.disable)
        self.user = get_user_model().objects.create_user('compact-delivery', is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='Operations KB', slug='compact-delivery', is_folder=True, created_by=self.user)
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name='Operations KB', mode='push', created_by=self.user)
        tracker = KbTracker.objects.create(kb=self.kb, fields=[{'label': field} for field in ['Name', 'Wait', 'State', 'Other']])
        for i in range(15):
            TrackerRow.objects.create(tracker=tracker, feed=self.feed, external_id=str(i), status='done',
                                      values={'Name': f'Ride {i}', 'Wait': str(i * 5), 'State': 'Open', 'Other': 'Extra'})
        self.plan = reports.configure(self.user, self.feed, {'kind': 'teams', 'url': WORKFLOW})
        self.spec = {'title': 'Long wait alerts', 'columns': [{'label': f, 'field': f} for f in ['Name', 'Wait', 'State', 'Other']],
                     'teams_card': {'fields': ['Name', 'Wait', 'State']}, 'range': {'mode': 'all'},
                     'schedule': {'mode': 'cron', 'cron': '* * * * *', 'timezone': 'Asia/Shanghai'},
                     'trigger': {'condition': {'field': 'Wait', 'op': 'gte', 'value': 30}}}

    def test_preview_and_approval_preserve_title_card_fields_and_separate_source_name(self):
        job = ConnectorDraft.objects.create(user=self.user, kb=self.kb, feed=self.feed, status='ready')
        normalized, preview = reports.preview(job, self.spec)
        self.assertEqual(preview['source_feed'], 'Operations KB')
        self.assertEqual(preview['delivery']['title'], 'Long wait alerts')
        self.assertEqual(preview['teams_card_preview']['row_count'], 9)
        self.assertIn('Ride 14', preview['teams_card_preview']['facts'][0]['value'])
        job.spec = normalized
        job.preview = preview
        job.save()
        reports.approve(job, self.user)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.spec['title'], 'Long wait alerts')
        self.assertEqual(self.plan.spec['teams_card']['fields'], ['Name', 'Wait', 'State'])
        self.kb.name = 'Renamed business KB'
        self.kb.save(update_fields=['name'])
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.spec['title'], 'Long wait alerts')
        self.assertEqual(reports.summary(self.feed)['title'], 'Long wait alerts')

    def test_scheduled_trigger_card_contains_all_matches_without_digest_or_collection(self):
        self.plan.spec = reports.normalize(self.spec, self.feed)
        self.plan.enabled = True
        self.plan.schedule_mode = 'cron'
        self.plan.cron = '* * * * *'
        self.plan.next_run_at = timezone.now().replace(second=0, microsecond=0)
        self.plan.save()
        with patch('kb.report_digest.generate_digest') as digest, patch('kb.feed_reports.post_workflow', return_value=202) as post:
            delivery = reports.run_due(self.plan.next_run_at)[0]
        self.assertEqual(delivery.status, 'accepted', delivery.error)
        digest.assert_not_called()
        card = post.call_args.args[1]['attachments'][0]['content']
        self.assertEqual(card['body'][0]['text'], 'Long wait alerts')
        facts = [fact for block in card['body'] for fact in block.get('facts', [])]
        self.assertEqual(len(facts), 3)
        self.assertEqual(facts[0]['value'], ', '.join('Ride ' + str(i) for i in range(6, 15)))
        self.assertNotIn('Ride 0,', facts[0]['value'])
        book = load_workbook(BytesIO(bytes(delivery.content)))
        self.assertEqual(book.properties.title, 'Long wait alerts')
        self.assertEqual(book.active.max_row, 16)
        book.close()

    def test_scheduled_statistics_aggregate_full_scope_and_can_change_title(self):
        spec = deepcopy(self.spec)
        spec.pop('trigger')
        spec.update(title='Daily total wait', group_by=[], aggregates=[{'field': 'Wait', 'op': 'sum', 'label': 'Total Wait'}],
                    teams_card={'fields': ['Total Wait']})
        spec = reports.normalize(spec, self.feed)
        with patch('kb.report_digest.generate_digest') as digest, patch('kb.feed_reports.post_workflow', return_value=202) as post:
            delivery = reports.send(self.plan, spec=spec)
        self.assertEqual(delivery.status, 'accepted', delivery.error)
        digest.assert_not_called()
        card = post.call_args.args[1]['attachments'][0]['content']
        self.assertEqual(card['body'][0]['text'], 'Daily total wait')
        self.assertEqual(card['body'][1]['facts'], [{'title': 'Total Wait', 'value': '525'}])

    def test_email_subject_is_independent_and_html_escapes_the_title(self):
        spec = {**self.spec, 'title': '设备超温 <提醒>', 'schedule': {'mode': 'manual'}}
        plan = reports.configure(self.user, self.feed, {'kind': 'email', 'url': WORKFLOW, 'recipient': 'owner@example.test'})
        with patch('kb.report_digest.generate_digest') as digest, patch('kb.feed_reports.post_workflow', return_value=202) as post:
            delivery = reports.send(plan, spec=spec)
        self.assertEqual(delivery.status, 'accepted', delivery.error)
        digest.assert_not_called()
        self.assertEqual(post.call_args.args[1]['s'], spec['title'])
        self.assertIn('设备超温 &lt;提醒&gt;', post.call_args.args[1]['b'])
        self.assertEqual(post.call_args.args[1]['b'].count('data-engenie-signature='), 1)
        self.assertIn('Engineering the Magic!', post.call_args.args[1]['b'])
