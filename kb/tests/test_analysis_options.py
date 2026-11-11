"""Analysis controls discover complete scoped data and apply exact local time ranges."""

from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from kb.models import Document, FeedRecord, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow


class AnalysisOptionsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('analysis-options-owner', is_staff=True)
        self.client.force_login(self.owner)
        self.kb = KnowledgeBase.objects.create(name='Analysis', slug='analysis-options',
                                               category='business', is_folder=True, created_by=self.owner)
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name='Source', mode='push',
                                                 timezone='America/New_York', created_by=self.owner)
        self.fields = ['Object', 'Number', 'Mixed', 'Boolean', 'Nested', 'Overflow', '123']
        self.tracker = KbTracker.objects.create(kb=self.kb, enabled=True,
                                               fields=[{'label': key} for key in self.fields])
        self.url = reverse('kb:feed_kb_data', args=[self.kb.slug])

    def row(self, values, moment=None, feed=None, status='done'):
        row = TrackerRow.objects.create(tracker=self.tracker, feed=feed or self.feed,
                                         status=status, values=values, external_id=uuid4().hex)
        if moment:
            TrackerRow.objects.filter(pk=row.pk).update(updated_at=moment)
        return row

    def get(self, **params):
        return self.client.get(self.url, {'scope': 'history', **params})

    def test_numeric_fields_use_all_saved_rows_and_keep_mixed_numeric_strings(self):
        self.row({'Object': 'Old', 'Number': 4, 'Mixed': '2.5e1', '123': '-7.5'})
        for index in range(30):
            self.row({'Object': f'Ride {index:02}', 'Mixed': 'unavailable', 'Boolean': True,
                      'Nested': {'value': 4}, 'Overflow': '1e999'})
        self.row({'Boolean': 2, 'Nested': 9}, status='proposed')
        response = self.get(kind='options', object_field='Object')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['numeric_fields'], ['Number', 'Mixed', '123'])
        self.assertEqual(response.json()['fields'], self.fields)

    def test_object_search_pages_history_and_roundtrips_exact_values(self):
        for index in range(56):
            self.row({'Object': f'Ride {index:02}'})
        exact = '  上海, Disney  '
        self.row({'Object': exact})
        self.row({'Object': 'x' * 201})
        self.row({'Object': '\t\n\u00a0'})
        first = self.get(kind='options', object_field='Object').json()
        second = self.get(kind='options', object_field='Object', object_offset=50).json()
        self.assertEqual(len(first['object_values']), 50)
        self.assertTrue(first['object_has_more'])
        self.assertEqual(first['object_next_offset'], 50)
        self.assertEqual(len(second['object_values']), 7)
        self.assertFalse(second['object_has_more'])
        self.assertEqual(len(set(first['object_values'] + second['object_values'])), 57)
        found = self.get(kind='options', object_field='Object', object_query='Disney').json()
        self.assertEqual(found['object_values'], [exact])
        response = self.get(kind='trend', object_field='Object', object_value=exact,
                            period='day', op='count')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['object_values'], [exact])
        self.assertEqual(response.json()['series'][0]['records'], 1)

    def test_options_are_scoped_to_selected_feed_and_accessible_kb(self):
        other = KnowledgeFeed.objects.create(kb=self.kb, name='Other', mode='push', created_by=self.owner)
        self.row({'Object': 'Visible', 'Number': '5'})
        self.row({'Object': 'Other source', 'Nested': 20}, feed=other)
        data = self.get(kind='options', feed=str(self.feed.pk), object_field='Object').json()
        self.assertEqual(data['object_values'], ['Visible'])
        self.assertEqual(data['numeric_fields'], ['Number'])
        self.client.force_login(get_user_model().objects.create_user('analysis-reader'))
        self.kb.visibility = 'individual'
        self.kb.save(update_fields=['visibility'])
        self.assertEqual(self.get(kind='options').status_code, 404)

    def test_precise_range_matches_records_summary_options_and_trend_in_feed_timezone(self):
        zone = ZoneInfo('America/New_York')
        self.row({'Object': 'A', 'Number': 3}, datetime(2026, 11, 10, 9, 0, tzinfo=zone))
        self.row({'Object': 'A', 'Number': 7, 'Mixed': '8'}, datetime(2026, 11, 10, 10, 0, tzinfo=zone))
        self.row({'Object': 'A', 'Number': 9}, datetime(2026, 11, 10, 23, 30, tzinfo=zone))
        params = {'feed': str(self.feed.pk), 'from': '2026-11-10T09:00', 'to': '2026-11-10T10:00'}
        rows = self.get(kind='rows', **params).json()
        self.assertEqual(rows['total_matched'], 1)
        summary = self.get(kind='summary', op='sum', measure='Number', **params).json()
        self.assertEqual(summary['groups'][0]['metrics']['value'], 3)
        options = self.get(kind='options', **params).json()
        self.assertEqual(options['numeric_fields'], ['Number'])
        self.assertEqual(options['timezone'], 'America/New_York')
        self.assertEqual(options['range_from'], '2026-11-10T09:00:00-05:00')
        self.assertEqual(options['data_from'], '2026-11-10T09:00:00-05:00')
        self.assertEqual(options['data_to'], '2026-11-10T23:30:00-05:00')
        trend = self.get(kind='trend', object_field='Object', object_value='A',
                         period='hour', op='sum', measure='Number', **params).json()
        self.assertEqual([point['value'] for point in trend['points']], [3])
        # Legacy end dates include the full selected-feed local calendar day.
        legacy = self.get(kind='rows', feed=str(self.feed.pk),
                          **{'from': '2026-11-10', 'to': '2026-11-10'}).json()
        self.assertEqual(legacy['total_matched'], 3)
        aware = self.get(kind='rows', feed=str(self.feed.pk),
                         **{'from': '2026-11-10T14:00:00Z', 'to': '2026-11-10T15:00:00Z'}).json()
        self.assertEqual(aware['total_matched'], 1)

    def test_invalid_ranges_and_options_inputs_fail_cleanly(self):
        for extra in ({'from': 'bad'}, {'from': '2026-11-10T10:00', 'to': '2026-11-10T09:00'},
                      {'from': '2026-11-10T10:00', 'to': '2026-11-10T10:00'},
                      {'object_field': 'missing'}, {'object_offset': '-1'}, {'object_query': 'x' * 201}):
            with self.subTest(extra=extra):
                self.assertEqual(self.get(kind='options', **extra).status_code, 400)
        response = self.get(kind='trend', object_field='Object', object_value='A', period='year',
                            **{'from': '9998-01-01T00:00', 'to': '9999-12-31T00:00'})
        self.assertEqual(response.status_code, 400)


class ManualAnalysisHistoryTests(TestCase):
    def test_options_and_history_summary_include_saved_document_snapshots_only_when_visible(self):
        user = get_user_model().objects.create_user('manual-analysis-owner', is_staff=True)
        self.client.force_login(user)
        kb = KnowledgeBase.objects.create(name='Manual', slug='manual-analysis-history', is_folder=True,
                                         created_by=user)
        child = KnowledgeBase.objects.create(name='Source', slug='manual-analysis-source', parent=kb,
                                            created_by=user)
        tracker = KbTracker.objects.create(kb=kb, enabled=True, fields=[{'label': 'Object'}, {'label': 'Value'}])
        feed = KnowledgeFeed.objects.create(kb=kb, name='News', mode='push', write_mode='append',
                                            adapter_spec={'ingest_mode': 'documents'}, created_by=user)
        old = Document.objects.create(kb=child, original_name='old.md', status='superseded')
        current = Document.objects.create(kb=child, original_name='new.md', status='completed')
        FeedRecord.objects.create(feed=feed, kb=child, document=current, external_id='one')
        TrackerRow.objects.create(tracker=tracker, document=old, status='done', values={'Object': 'Old', 'Value': 4})
        TrackerRow.objects.create(tracker=tracker, document=current, status='done', values={'Object': 'Current'})
        url = reverse('kb:tracker_kb_data', args=[kb.slug])
        data = self.client.get(url, {'kind': 'options', 'object_field': 'Object'}).json()
        self.assertEqual(data['object_values'], ['Current', 'Old'])
        self.assertEqual(data['numeric_fields'], ['Value'])
        data = self.client.get(url, {'kind': 'summary', 'scope': 'history', 'op': 'sum', 'measure': 'Value'}).json()
        self.assertEqual(data['groups'][0]['metrics']['value'], 4)
        feed.write_mode = 'upsert'
        feed.save(update_fields=['write_mode'])
        data = self.client.get(url, {'kind': 'options', 'object_field': 'Object'}).json()
        self.assertEqual(data['object_values'], ['Current'])
        self.assertEqual(data['numeric_fields'], [])
