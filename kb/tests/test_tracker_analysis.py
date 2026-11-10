from __future__ import annotations

"""参数分析的采集时间边界、聚合口径、来源隔离与页面权限回归。"""

from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from kb.models import FeedRun, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow
from kb.tracker_analysis import analyze, analysis_options


class TrackerAnalysisTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('analysis-manager', is_staff=True)
        self.client.force_login(self.user)
        self.kb = KnowledgeBase.objects.create(name='运营数据', slug='parameter-analysis', category='business', is_folder=True)
        self.tracker = KbTracker.objects.create(kb=self.kb, enabled=True, fields=[{'label': label} for label in
            ('标题', '场次数', '开场时间', '开放状态', '采集时间', '采集日期', '来源ID')])
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name='官网采集', timezone='Asia/Shanghai', created_by=self.user)
        self.url = reverse('kb:tracker_analysis', args=[self.kb.slug])

    def row(self, stamp='2026-01-01T08:00:00+08:00', value='3', external_id='show-1', feed=None, status='done', **values):
        feed = feed or self.feed
        run = FeedRun.objects.create(feed=feed, lease_token='test', status='completed')
        FeedRun.objects.filter(pk=run.pk).update(started_at=datetime.fromisoformat(stamp))
        return TrackerRow.objects.create(tracker=self.tracker, feed=feed, feed_run=run, external_id=external_id,
            status=status, values={'标题': '海盗演出', '场次数': value, '采集时间': stamp,
                                   '采集日期': '2020-01-01', **values})

    def test_full_range_numeric_metrics_exclude_missing_and_non_numeric_instead_of_zero(self):
        row = self.row(value=0)
        self.row('2026-01-01T09:00:00+08:00', value=' 3 ')
        self.row('2026-01-02T08:00:00+08:00', value='未营业')
        self.row('2026-01-02T09:00:00+08:00', value='9')
        self.row('2026-01-02T10:00:00+08:00', value=None)
        data = analyze(row, '场次数')
        summary = data['summary']
        self.assertEqual(data['kind'], 'number')
        self.assertEqual(summary['sample_count'], 5)
        self.assertEqual(summary['valid_count'], 3)
        self.assertEqual((summary['avg'], summary['min'], summary['max'], summary['latest']), (4, 0, 9, 9))
        self.assertEqual(summary['delta'], 6)
        self.assertEqual(summary['delta_percent'], 200)
        self.assertEqual(data['periods'][0]['avg'], 1.5)
        self.assertEqual(data['periods'][1]['comparison']['avg']['delta'], 7.5)
        self.assertEqual(data['periods'][1]['comparison']['sum']['delta'], 6)

    def test_date_time_bounds_are_inclusive_and_use_feed_timezone_and_batch_time(self):
        row = self.row('2026-01-01T00:30:00Z', value=1, **{'采集时间': '2001-01-01T08:00:00+08:00'})
        self.row('2026-01-01T00:31:00Z', value=8)
        result = self.client.get(self.url, {'row': row.pk, 'parameter': '场次数',
            'start': '2026-01-01T08:30:00', 'end': '2026-01-01T08:30:00'})
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['summary']['sample_count'], 1)
        self.assertEqual(result.json()['summary']['avg'], 1)
        self.assertEqual(result.json()['timezone'], 'Asia/Shanghai')
        self.assertEqual(result.json()['periods'][0]['start'], '2026-01-01T00:00:00+08:00')

    def test_different_ids_sources_pending_and_manual_rows_never_enter_item_metrics(self):
        row = self.row(value=3)
        self.row(value=999, external_id='other')
        other_feed = KnowledgeFeed.objects.create(kb=self.kb, name='其他来源')
        self.row(value=999, feed=other_feed)
        self.row(value=999, status='proposed')
        TrackerRow.objects.create(tracker=self.tracker, status='done', values={'场次数': 999})
        self.assertEqual(analyze(row, '场次数')['summary']['avg'], 3)
        self.assertEqual(len(analysis_options(self.tracker)['items']), 3)

    def test_titles_can_change_without_splitting_stable_item(self):
        row = self.row(value=2)
        self.row('2026-01-02T08:00:00+08:00', value=4, **{'标题': '新演出名称'})
        self.assertEqual(analyze(row, '场次数')['summary']['sample_count'], 2)
        options = analysis_options(self.tracker)
        self.assertEqual(len(options['items']), 1)
        self.assertEqual(options['items'][0]['title'], '新演出名称')
        self.assertEqual(options['items'][0]['sample_count'], 2)
        self.assertEqual(options['parameters'], ['场次数', '开场时间', '开放状态'])
        self.assertEqual(options['default_parameter'], '场次数')

    def test_clock_metrics_are_minutes_but_not_number_like_strings(self):
        row = self.row(**{'开场时间': '09:30'})
        self.row('2026-01-01T09:00:00+08:00', **{'开场时间': '10:00'})
        self.row('2026-01-02T09:00:00+08:00', **{'开场时间': '今日关闭'})
        data = analyze(row, '开场时间')
        self.assertEqual(data['kind'], 'clock')
        self.assertEqual(data['summary']['valid_count'], 2)
        self.assertEqual(data['summary']['avg'], 585)
        self.assertEqual(data['summary']['delta'], 30)
        self.assertIsNone(data['summary']['delta_percent'])
        self.assertIsNone(data['periods'][1]['avg'])

    def test_categories_analyze_distribution_and_changes_not_artificial_numeric_values(self):
        row = self.row(**{'开放状态': '开放'})
        self.row('2026-01-01T09:00:00+08:00', **{'开放状态': ''})
        self.row('2026-01-01T10:00:00+08:00', **{'开放状态': '关闭'})
        self.row('2026-01-02T09:00:00+08:00', **{'开放状态': '关闭'})
        self.row('2026-01-02T10:00:00+08:00', **{'开放状态': '开放'})
        data = analyze(row, '开放状态')
        self.assertEqual(data['kind'], 'category')
        self.assertEqual(data['summary']['distinct_count'], 2)
        self.assertEqual(data['summary']['changes'], 2)
        self.assertEqual(data['summary']['present_count'], 4)
        self.assertEqual({r['value']: r['count'] for r in data['distribution']}, {'开放': 2, '关闭': 2})
        self.assertEqual([p['changes'] for p in data['periods']], [1, 1])

    def test_no_matches_returns_real_zero_samples_not_zero_parameter_values(self):
        row = self.row()
        data = analyze(row, '场次数', start='2027-01-01T00:00')
        self.assertEqual(data['summary']['sample_count'], 0)
        self.assertEqual(data['periods'], [])

    def test_missing_periods_not_filled_as_zero_and_previous_zero_has_no_percent(self):
        row = self.row(value=0)
        self.row('2026-01-05T08:00:00+08:00', value=4)
        data = analyze(row, '场次数')
        self.assertEqual(data['period_count'], 2)
        self.assertEqual(data['periods'][-1]['comparison']['avg']['delta'], 4)
        self.assertIsNone(data['periods'][-1]['comparison']['avg']['percent'])

    def test_subsecond_bounds_preserve_precision(self):
        row = self.row('2026-01-01T08:00:00.000001+08:00', value=9)
        self.assertEqual(analyze(row, '场次数', end='2026-01-01T08:00:00')['summary']['sample_count'], 0)
        self.assertEqual(analyze(row, '场次数', start='2026-01-01T08:00:00.000001',
                                 end='2026-01-01T08:00:00.000001')['summary']['sample_count'], 1)

    def test_recent_period_limit_never_limits_overall_metrics(self):
        stamp = datetime.fromisoformat('2026-01-01T08:00:00+08:00')
        row = None
        for index in range(125):
            row = self.row((stamp + timedelta(days=index)).isoformat(), value=index)
        data = analyze(row, '场次数')
        self.assertEqual(data['summary']['sample_count'], 125)
        self.assertEqual(data['summary']['avg'], 62)
        self.assertEqual(data['period_count'], 125)
        self.assertEqual(len(data['periods']), 120)
        self.assertTrue(data['truncated_periods'])
        self.assertEqual(data['periods'][0]['comparison']['avg']['delta'], 1)

    def test_hour_week_month_year_buckets(self):
        row = self.row('2026-01-04T08:00:00+08:00', value=2)
        self.row('2026-01-05T08:00:00+08:00', value=4)
        self.assertEqual(analyze(row, '场次数', grain='hour')['period_count'], 2)
        weekly = analyze(row, '场次数', grain='week')
        self.assertEqual(weekly['periods'][0]['start'][:10], '2025-12-29')
        self.assertEqual(weekly['periods'][1]['start'][:10], '2026-01-05')
        self.assertEqual(analyze(row, '场次数', grain='month')['period_count'], 1)
        self.assertEqual(analyze(row, '场次数', grain='year')['period_count'], 1)

    def test_upsert_legacy_timestamp_is_normalized_to_utc_before_filtering(self):
        self.feed.write_mode = 'upsert'
        self.feed.save(update_fields=['write_mode'])
        row = TrackerRow.objects.create(tracker=self.tracker, feed=self.feed, external_id='current', status='done',
            values={'标题': '旧数据', '采集时间': '2026-01-01T09:00:00+08:00', '场次数': 7})
        data = analyze(row, '场次数', start='2026-01-01T01:00:00Z', end='2026-01-01T01:00:00Z')
        self.assertEqual(data['summary']['sample_count'], 1)
        self.assertEqual(data['item']['write_mode'], 'upsert')
        row.values['采集时间'] = 'bad timestamp'
        row.save(update_fields=['values'])
        self.assertEqual(analyze(row, '场次数')['summary']['sample_count'], 1)

    def test_invalid_inputs_are_errors_not_unfiltered_fallbacks(self):
        row = self.row()
        for extra in ({'start': 'nonsense'}, {'end': '2026-88-01'},
                      {'start': '2026-02-02T00:00', 'end': '2026-01-01T00:00'},
                      {'grain': 'decade'}, {'parameter': '不存在的列'}, {'row': ''}):
            with self.subTest(extra=extra):
                response = self.client.get(self.url, {'row': row.pk, 'parameter': '场次数', **extra})
                self.assertEqual(response.status_code, 400, response.content)

    def test_literal_json_keys_zero_and_boolean_never_silently_cast_to_numeric_zero(self):
        self.tracker.fields += [{'label': 'a.b"[]'}]
        self.tracker.save(update_fields=['fields'])
        row = self.row(**{'a.b"[]': 0, '开放状态': False})
        self.assertEqual(analyze(row, 'a.b"[]')['summary']['latest'], 0)
        self.assertEqual(analyze(row, '开放状态')['kind'], 'category')

    def test_item_options_are_bounded_and_searchable(self):
        run = FeedRun.objects.create(feed=self.feed, lease_token='test', status='completed')
        TrackerRow.objects.bulk_create([TrackerRow(tracker=self.tracker, feed=self.feed, feed_run=run,
            external_id=f'item-{index:03}', status='done', values={'标题': f'项目{index}', '场次数': index}) for index in range(202)])
        data = analysis_options(self.tracker)
        self.assertTrue(data['has_more'])
        self.assertEqual(len(data['items']), 200)
        response = self.client.get(self.url, {'q': 'item-201'})
        self.assertEqual(response.json()['items'][0]['title'], '项目201')
        self.assertEqual(len(response.json()['items']), 1)

    def test_auto_feed_tracker_keeps_records_without_duplicate_analysis_panel(self):
        row = self.row()
        response = self.client.get(reverse('kb:tracker', args=[self.kb.slug]))
        self.assertNotContains(response, 'id="trackerAnalysis"')
        self.assertNotContains(response, 'id="trkRawRecords"')
        self.assertNotContains(response, '<button type="button" class="trk-analyze"')
        self.assertContains(response, '海盗演出')
        self.assertContains(response, '?xlsx=1')

    def test_cross_kb_row_and_department_access_is_denied(self):
        row = self.row()
        other_kb = KnowledgeBase.objects.create(name='其他库', slug='other-analysis')
        other_tracker = KbTracker.objects.create(kb=other_kb)
        foreign = TrackerRow.objects.create(tracker=other_tracker, feed=self.feed, external_id='foreign', status='done')
        self.assertEqual(self.client.get(self.url, {'row': foreign.pk, 'parameter': '场次数'}).status_code, 404)
        manager = get_user_model().objects.create_user('department-analysis')
        UserProfile.objects.update_or_create(user=manager, defaults={'department': '机械', 'role': UserProfile.Role.DEPT_ADMIN})
        self.kb.department = '电气'
        self.kb.save(update_fields=['department'])
        self.client.force_login(manager)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(self.url, {'row': row.pk, 'parameter': '场次数'}).status_code, 404)
        self.kb.department = '机械'
        self.kb.save(update_fields=['department'])
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_anonymous_blocked_and_reader_follows_kb_read_access(self):
        """分析权限跟“能不能读这个库”走，不再卡管理员。

        原期望是「普通账号 404」（分析仅管理可用）；现在「通用」库对所有部门
        开放，所以同库可读 = 可分析；但库改成部门专属后必须立刻 404。
        """
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        reader = get_user_model().objects.create_user('analysis-reader')
        self.client.force_login(reader)
        self.assertEqual(self.client.get(self.url).status_code, 200)   # 共享库：可读即可分析
        self.kb.department = '机械'
        self.kb.save(update_fields=['department'])
        self.assertEqual(self.client.get(self.url).status_code, 404)    # 换部门 -> 马上关掉
