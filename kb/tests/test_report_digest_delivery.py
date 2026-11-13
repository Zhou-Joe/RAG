"""Digest opt-in, fresh-result delivery, and creator-only configuration."""
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import json
import base64
from urllib.parse import urlsplit
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from kb import feed_reports as reports
from kb.models import ConnectorDraft, FeedReportDelivery, FeedReportPlan, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow
from kb.report_digest import build_facts


URL = 'https://tenant.environment.api.powerplatform.com/workflows/fixture/triggers/manual/paths/invoke?sig=fixture'


@override_settings(FEED_REPORT_PUBLIC_BASE='https://reports.example.test')
class ReportDigestDeliveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('digest-owner', is_staff=True)
        self.other = get_user_model().objects.create_user('digest-other', is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='Operations', slug='digest-operations',
                                               category='business', is_folder=True, created_by=self.user)
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name='Operations', mode='push', created_by=self.user)
        tracker = KbTracker.objects.create(kb=self.kb, enabled=True,
                                          fields=[{'label': key} for key in ('Name', 'Wait')])
        for index, wait in enumerate(('0', '5', '7')):
            TrackerRow.objects.create(tracker=tracker, feed=self.feed, external_id=str(index),
                                      status='done', values={'Name': str(index), 'Wait': wait})
        self.plan = reports.configure(self.user, self.feed, {
            'kind': 'email', 'url': URL, 'recipient': 'fixture@example.test',
        })
        self.spec = {'kind': 'report', 'columns': [{'label': key, 'field': key} for key in ('Name', 'Wait')],
                     'range': {'mode': 'all'}, 'schedule': {'mode': 'cron', 'cron': '* * * * *', 'timezone': 'Asia/Shanghai'},
                     'digest': {'enabled': True, 'instructions': 'Explain the total wait.', 'language': 'en',
                                'metrics': [{'op': 'sum', 'field': 'Wait', 'label': 'Total Wait'}]}}
        self.at = timezone.now().replace(second=0, microsecond=0)
        self.client.force_login(self.user)
        self.control_url = reverse('kb:report_schedule_control', args=[self.feed.pk])

    def enable(self, *, conditional=False, digest=True):
        spec = deepcopy(self.spec)
        if conditional:
            spec['trigger'] = {'condition': {'field': 'Wait', 'op': 'gt', 'value': 6}}
        if not digest:
            spec.pop('digest')
        self.plan.spec = reports.normalize(spec, self.feed)
        self.plan.schedule_mode = 'cron'
        self.plan.cron = '* * * * *'
        self.plan.timezone = 'Asia/Shanghai'
        self.plan.enabled = True
        self.plan.next_run_at = self.at
        self.plan.save()

    @staticmethod
    def generated(spec, headers, rows, **kwargs):
        return {'text': 'Summary <script>source</script>', 'facts': build_facts(headers, rows, spec['digest']),
                'model': 'fixture-model', 'generated_at': timezone.now().isoformat()}

    def tick(self):
        return reports.run_due(self.at)[0]

    def test_scheduled_email_contains_digest_totals_and_excel_digest_sheet(self):
        self.enable()
        with patch('kb.report_digest.generate_digest', side_effect=self.generated) as generate, \
                patch('kb.feed_reports.post_workflow', return_value=202) as post:
            delivery = self.tick()
        self.assertEqual(delivery.status, 'accepted', delivery.error)
        generate.assert_called_once()
        self.assertEqual(len(generate.call_args.args[2]), 3)
        self.assertEqual(delivery.digest['facts']['metrics'][0]['value'], '12')
        body = post.call_args.args[1]['b']
        self.assertIn('EnGenie Digest', body)
        self.assertIn('Total Wait', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertNotIn('<script>', body)
        book = load_workbook(BytesIO(bytes(delivery.content)))
        self.assertIn('EnGenie Digest', book.sheetnames)
        self.assertIn('Digest Aggregates', book.sheetnames)
        self.assertIn('12', [row[4] for row in book['Digest Aggregates'].iter_rows(values_only=True)
                            if row[2] == 'Total Wait'])
        self.assertIn('Summary <script>source</script>', book['EnGenie Digest']['A2'].value)
        self.assertEqual(book['EnGenie Digest']['A2'].data_type, 's')
        book.close()

    def test_disabled_digest_never_calls_model(self):
        self.enable(digest=False)
        with patch('kb.report_digest.generate_digest') as generate, patch('kb.feed_reports.post_workflow', return_value=202):
            delivery = self.tick()
        self.assertEqual(delivery.status, 'accepted')
        self.assertEqual(delivery.digest, {})
        generate.assert_not_called()

    def test_unmatched_condition_skips_digest_and_workflow(self):
        self.enable(conditional=True)
        self.plan.spec['trigger']['condition']['value'] = 100
        self.plan.save()
        with patch('kb.report_digest.generate_digest') as generate, patch('kb.feed_reports.post_workflow') as post:
            delivery = self.tick()
        self.assertEqual(delivery.status, 'skipped')
        generate.assert_not_called()
        post.assert_not_called()

    def test_digest_failure_releases_trigger_reservation_without_sending(self):
        self.enable(conditional=True)
        with patch('kb.report_digest.generate_digest', side_effect=ValueError('Digest unavailable')), \
                patch('kb.feed_reports.post_workflow') as post:
            delivery = self.tick()
        self.assertEqual(delivery.status, 'failed')
        post.assert_not_called()
        self.plan.refresh_from_db()
        self.assertNotIn('reserved_delivery', self.plan.trigger_state)
        self.assertFalse(self.plan.trigger_state.get('notified_for_match'))

    def test_plan_paused_during_digest_does_not_send(self):
        self.enable(conditional=True)
        def pause(*args, **kwargs):
            FeedReportPlan.objects.filter(pk=self.plan.pk).update(enabled=False)
            return self.generated(*args, **kwargs)
        with patch('kb.report_digest.generate_digest', side_effect=pause), patch('kb.feed_reports.post_workflow') as post:
            delivery = self.tick()
        self.assertEqual(delivery.status, 'failed')
        post.assert_not_called()

    def test_duplicate_delivery_request_does_not_regenerate_or_post_twice(self):
        self.enable()
        with patch('kb.report_digest.generate_digest', side_effect=self.generated) as generate, \
                patch('kb.feed_reports.post_workflow', return_value=202) as post:
            first = reports.send(self.plan, request_key='same-request')
            second = reports.send(self.plan, request_key='same-request')
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(post.call_count, 1)

    def test_teams_card_contains_narrative_and_deterministic_figures(self):
        reports.configure(self.user, self.feed, {'kind': 'teams', 'url': URL})
        self.plan.refresh_from_db()
        self.enable(conditional=True)
        with patch('kb.report_digest.generate_digest', side_effect=self.generated), \
                patch('kb.feed_reports.post_workflow', return_value=202) as post:
            delivery = self.tick()
        self.assertEqual(delivery.status, 'accepted', delivery.error)
        card = post.call_args.args[1]['attachments'][0]['content']
        self.assertIn('EnGenie Digest', json.dumps(card))
        self.assertIn('Total Wait', json.dumps(card))
        self.assertFalse(any(block['type'] == 'FactSet' for block in card['body']))

    def test_digest_only_edit_preserves_paused_schedule_conditions_and_credentials(self):
        self.enable(conditional=True)
        self.plan.enabled = False
        self.plan.next_run_at = None
        self.plan.save()
        original = deepcopy(self.plan.spec)
        ciphertext = self.plan.destination
        response = self.client.post(self.control_url, json.dumps({'action': 'update_digest',
            'digest': {'enabled': True, 'instructions': 'Highlight exceptions', 'language': 'zh-hans'}}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.enabled)
        self.assertIsNone(self.plan.next_run_at)
        self.assertEqual(self.plan.destination, ciphertext)
        for key in ('columns', 'range', 'schedule', 'trigger'):
            self.assertEqual(self.plan.spec[key], original[key])
        self.assertEqual(response.json()['report']['digest']['instructions'], 'Highlight exceptions')

    def test_invalid_digest_rejects_entire_frequency_update(self):
        self.enable()
        original = deepcopy(self.plan.spec)
        response = self.client.post(self.control_url, json.dumps({'action': 'update_schedule',
            'schedule': {'mode': 'cron', 'cron': '0 9 * * *', 'timezone': 'Asia/Shanghai'},
            'digest': {'enabled': True, 'metrics': [{'op': 'sum', 'field': 'Unknown', 'label': 'Bad'}]}}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.spec, original)

    def test_only_creator_can_update_digest(self):
        self.enable()
        self.client.force_login(self.other)
        response = self.client.post(self.control_url, json.dumps({'action': 'update_digest',
            'digest': {'enabled': False}}), content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_unknown_trigger_delivery_is_not_regenerated_on_next_check(self):
        self.enable(conditional=True)
        with patch('kb.report_digest.generate_digest', side_effect=self.generated) as generate, \
                patch('kb.feed_reports.post_workflow', side_effect=TimeoutError) as post:
            first = self.tick()
            self.at += timedelta(minutes=1)
            second = self.tick()
        self.assertEqual(first.status, 'unknown')
        self.assertEqual(second.status, 'skipped')
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(post.call_count, 1)

    def test_preview_generates_digest_without_sending_and_preserves_destination(self):
        reports.configure(self.user, self.feed, {'kind': 'teams', 'url': URL})
        job = ConnectorDraft.objects.create(user=self.user, kb=self.kb, feed=self.feed, status='running')
        self.spec['delivery'] = {'kind': 'email', 'recipient': 'selected@example.test'}
        with patch('kb.report_digest.generate_digest', side_effect=self.generated) as generate, \
                patch('kb.feed_reports.post_workflow') as post:
            spec, preview = reports.preview(job, self.spec)
        self.assertEqual(spec['delivery'], self.spec['delivery'])
        self.assertEqual(preview['delivery']['kind'], 'email')
        self.assertEqual(preview['digest_preview']['facts']['metrics'][0]['value'], '12')
        generate.assert_called_once()
        post.assert_not_called()

    def test_unmatched_preview_shows_fact_scope_without_requesting_llm(self):
        job = ConnectorDraft.objects.create(user=self.user, kb=self.kb, feed=self.feed, status='running')
        self.spec['trigger'] = {'condition': {'field': 'Wait', 'op': 'gt', 'value': 100}}
        with patch('kb.report_digest.generate_digest') as generate:
            _, preview = reports.preview(job, self.spec)
        self.assertEqual(preview['digest_preview']['reason'], 'condition_not_met')
        self.assertEqual(preview['digest_preview']['facts']['row_count'], 3)
        generate.assert_not_called()

    def test_html_attachment_requires_confirmed_workflow_mapping_before_llm(self):
        self.spec['digest']['email_format']='html_attachment'
        self.enable()
        with patch('kb.report_digest.generate_digest') as generate, patch('kb.feed_reports.post_workflow') as post:
            delivery=self.tick()
        self.assertEqual(delivery.status,'failed')
        self.assertIn('ContentBytes',delivery.error)
        generate.assert_not_called();post.assert_not_called()

    def test_html_attachment_is_a_real_offline_file_and_does_not_require_public_link(self):
        reports.save_workflow_settings(self.user,self.feed,{'html_attachments_supported':True})
        self.plan.refresh_from_db()
        self.spec['digest'].update(email_format='html_attachment',charts=[])
        self.enable()
        unavailable={'available':False,'public_base':'','source':'none','reason':'No public entry'}
        with patch('kb.report_digest.generate_digest',side_effect=self.generated), \
                patch('kb.feed_reports.post_workflow',return_value=202) as post, \
                patch('kb.feed_reports.site_address',return_value=unavailable), \
                patch('kb.feed_reports.download_link') as download:
            delivery=self.tick()
        self.assertEqual(delivery.status,'accepted',delivery.error)
        body=post.call_args.args[1]
        attachment=body['attachments'][0]
        self.assertEqual(attachment['Name'],'EnGenie-report.html')
        html=base64.b64decode(attachment['ContentBytes']).decode('utf-8')
        self.assertIn('<!doctype html>',html.lower())
        self.assertIn('Total Wait',html)
        self.assertEqual(delivery.html_content,html.encode('utf-8'))
        self.assertNotIn('Download complete Excel',body['b'])
        download.assert_not_called()

    def test_html_report_link_opens_saved_dashboard_with_sandbox_headers(self):
        self.spec['digest'].update(email_format='html_link',charts=[])
        self.enable()
        with patch('kb.report_digest.generate_digest',side_effect=self.generated), \
                patch('kb.feed_reports.post_workflow',return_value=202) as post:
            delivery=self.tick()
        self.assertEqual(delivery.status,'accepted',delivery.error)
        self.assertTrue(delivery.html_content)
        self.assertIn('?format=html',post.call_args.args[1]['b'])
        import re
        link=re.search(r'href="([^"]+\?format=html)"',post.call_args.args[1]['b'])[1]
        parsed=urlsplit(link)
        response=self.client.get(parsed.path+'?'+parsed.query)
        self.assertEqual(response.status_code,200)
        self.assertIn('text/html',response['Content-Type'])
        self.assertIn('sandbox',response['Content-Security-Policy'])
        self.assertNotIn('allow-same-origin',response['Content-Security-Policy'])
        self.assertEqual(response.content,bytes(delivery.html_content))

    def test_preview_html_refuses_stale_data_and_remains_private(self):
        self.spec['digest'].update(email_format='html_link',charts=[])
        job=ConnectorDraft.objects.create(user=self.user,kb=self.kb,feed=self.feed,status='running')
        with patch('kb.report_digest.generate_digest',side_effect=self.generated):
            job.spec,job.preview=reports.preview(job,self.spec)
        job.status='ready';job.save()
        url=job.preview['digest_preview']['html']['download_url']
        self.assertEqual(self.client.get(url).status_code,200)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code,404)
        self.client.force_login(self.user)
        row=self.feed.tracker_rows.first();row.values={**row.values,'Wait':'99'};row.save()
        response=self.client.get(url)
        self.assertEqual(response.status_code,400)
        self.assertIn('预览',response.json()['error'])

    def test_workflow_capability_can_change_without_replacing_url_and_resets_on_new_url(self):
        self.assertTrue(reports.save_workflow_settings(self.user,self.feed,{'html_attachments_supported':True}))
        self.plan.refresh_from_db()
        self.assertTrue(reports.destination(self.plan)['html_attachments_supported'])
        self.assertFalse(reports.save_workflow_settings(self.user,self.feed,{'html_attachments_supported':True}))
        reports.save_workflow_settings(self.user,self.feed,{'email_workflow_url':URL.replace('fixture/triggers','replacement/triggers')})
        self.plan.refresh_from_db()
        self.assertFalse(reports.destination(self.plan)['html_attachments_supported'])

    def test_source_links_are_clickable_and_teams_html_attachment_is_rejected(self):
        row=self.feed.tracker_rows.first();row.values={**row.values,'Source':'https://example.test/item?a=1&b=2'};row.save()
        self.spec['columns'].append({'label':'Source','field':'Source'})
        self.spec['digest']['include_source_links']=True
        self.enable()
        with patch('kb.report_digest.generate_digest',side_effect=self.generated),patch('kb.feed_reports.post_workflow',return_value=202) as post:
            delivery=self.tick()
        self.assertEqual(delivery.status,'accepted',delivery.error)
        self.assertIn('https://example.test/item?a=1&amp;b=2',post.call_args.args[1]['b'])
        self.spec['delivery']={'kind':'teams'}
        self.spec['digest']['email_format']='html_attachment'
        with self.assertRaises(ValueError):reports.normalize(self.spec,self.feed)

