"""展示字段跨页面与接口一致，隐藏后原值及后续采集仍完整。"""
from __future__ import annotations

import io
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from kb.active_fields import field_options, visible_fields
from kb.feeds import _ensure_tracker
from kb.models import FeedRun, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow
from kb.tracker_analysis import parameters


class ActiveFieldsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('field-owner', is_staff=True)
        self.reader = get_user_model().objects.create_user('field-reader')
        self.client.force_login(self.owner)
        self.kb = KnowledgeBase.objects.create(
            name='运营数据', slug='active-fields', is_folder=True, category='business', created_by=self.owner)
        self.tracker = KbTracker.objects.create(kb=self.kb, enabled=True,
            fields=[{'label': label} for label in ('景点', '等待时间', '内部备注')])
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name=self.kb.name, mode='push', created_by=self.owner)
        run = FeedRun.objects.create(feed=self.feed, status='completed', lease_token='test')
        self.row = TrackerRow.objects.create(tracker=self.tracker, feed=self.feed, feed_run=run,
            status='done', external_id='ride-1', values={'景点': 'Ride A', '等待时间': '35', '内部备注': 'keep me'})
        self.rename = reverse('kb:kb_rename', args=[self.kb.slug])
        self.data = reverse('kb:feed_kb_data', args=[self.kb.slug])

    def save_fields(self, selected, **extra):
        return self.client.post(self.rename, {'name': self.kb.name,
            'active_fields': json.dumps(selected), **extra}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def select(self, selected):
        response = self.save_fields(selected)
        self.assertEqual(response.status_code, 200, response.content)
        self.kb.refresh_from_db()

    def test_selection_is_persisted_without_changing_schema_or_original_values(self):
        original = dict(self.row.values)
        self.select(['景点', '等待时间'])
        self.assertEqual(self.kb.active_fields, ['景点', '等待时间'])
        self.tracker.refresh_from_db()
        self.row.refresh_from_db()
        self.assertEqual(self.tracker.fields[-1]['label'], '内部备注')
        self.assertEqual(self.row.values, original)
        self.assertEqual(field_options(self.kb, self.tracker)[-1], {'label': '内部备注', 'selected': False})

    def test_rows_details_and_analysis_options_only_use_active_fields(self):
        self.select(['景点', '等待时间'])
        page = self.client.get(self.data, {'scope': 'history'}).json()
        self.assertEqual(page['fields'], ['景点', '等待时间'])
        self.assertEqual(page['columns'], ['景点', '等待时间'])
        self.assertEqual(page['rows'][0]['values'], {'景点': 'Ride A', '等待时间': '35'})
        detail = self.client.get(self.data, {'kind': 'record', 'row': self.row.pk}).json()
        self.assertNotIn('内部备注', detail['values'])
        options = self.client.get(self.data, {'kind': 'options', 'object_field': '景点'}).json()
        self.assertEqual(options['fields'], ['景点', '等待时间'])
        self.assertEqual(options['numeric_fields'], ['等待时间'])
        self.assertEqual(parameters(self.tracker), ['景点', '等待时间'])
        for query in ({'kind': 'summary', 'group': '内部备注'},
                      {'kind': 'summary', 'op': 'sum', 'measure': '内部备注'},
                      {'kind': 'options', 'object_field': '内部备注'},
                      {'kind': 'trend', 'object_field': '内部备注', 'object_value': 'keep me'},
                      {'kind': 'rows', 'field': '内部备注', 'value': 'keep'},
                      {'kind': 'summary', 'analysis_filters': json.dumps([
                          {'field': '内部备注', 'op': 'contains', 'value': 'keep'}])}):
            self.assertEqual(self.client.get(self.data, query).status_code, 400, query)

    def test_tracker_page_and_export_project_same_fields(self):
        self.select(['等待时间'])
        url = reverse('kb:tracker', args=[self.kb.slug])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['displayed_fields'], ['等待时间'])
        self.assertEqual(page.context['rows'][0]['pairs'], [{'label': '等待时间', 'value': '35', 'changed': False}])
        self.assertNotContains(page, '<th class="c-field" translate="no">内部备注</th>', html=True)
        export = self.client.get(url, {'xlsx': '1'})
        book = load_workbook(io.BytesIO(export.content), read_only=True)
        self.assertEqual(next(book.active.values)[1:-4], ('等待时间',))

    def test_cron_schema_extension_keeps_selection_and_all_data(self):
        self.select(['等待时间'])
        _ensure_tracker(self.feed, [{'label': label} for label in ('景点', '等待时间', '内部备注', '新字段')])
        self.kb.refresh_from_db()
        self.tracker.refresh_from_db()
        self.assertEqual(visible_fields(self.kb, self.tracker), ['等待时间'])
        self.assertIn({'label': '新字段'}, self.tracker.fields)
        self.row.refresh_from_db()
        self.assertEqual(self.row.values['内部备注'], 'keep me')
        self.select(None)
        self.assertEqual(visible_fields(self.kb, self.tracker), ['景点', '等待时间', '内部备注', '新字段'])
        detail = self.client.get(self.data, {'kind': 'record', 'row': self.row.pk}).json()
        self.assertEqual(detail['values']['内部备注'], 'keep me')

    def test_default_all_includes_new_fields(self):
        _ensure_tracker(self.feed, [{'label': '新增数值'}])
        self.tracker.refresh_from_db()
        self.assertIn('新增数值', visible_fields(self.kb, self.tracker))

    def test_invalid_selection_cannot_partially_rename_kb(self):
        for selected in ([], ['不存在'], ['景点', '景点'], '景点', [['景点']]):
            response = self.save_fields(selected, name='must not save')
            self.assertEqual(response.status_code, 400, response.content)
            self.kb.refresh_from_db()
            self.assertEqual(self.kb.name, '运营数据')
            self.assertIsNone(self.kb.active_fields)

    def test_reader_cannot_change_selection(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.save_fields(['等待时间']).status_code, 404)
        self.kb.refresh_from_db()
        self.assertIsNone(self.kb.active_fields)

    def test_native_editor_fields_and_page_layout(self):
        response = self.client.post(self.rename, {'name': self.kb.name,
            'active_fields_mode': 'selected', 'active_field': ['景点', '等待时间']},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200, response.content)
        page = self.client.get(reverse('kb:manage_detail', args=[self.kb.slug]))
        self.assertEqual(page.context['tracker_fields'], ['景点', '等待时间'])
        self.assertContains(page, 'value="内部备注"')
        self.assertContains(page, 'id="feedKbName" translate="no"')
        text = page.content.decode()
        self.assertLess(text.index('id="feedKbAggregate"'), text.index('id="feedKbData"'))
