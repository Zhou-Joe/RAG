from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from kb.models import FeedRun, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow


class FeedKnowledgeBaseDetailTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("feed-page-manager", is_staff=True)
        self.client.force_login(self.user)
        self.kb = KnowledgeBase.objects.create(
            name="运营数据", slug="feed-page", is_folder=True,
            category="business", created_by=self.user,
        )
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name=self.kb.name, mode="push", created_by=self.user)
        self.tracker = KbTracker.objects.create(
            kb=self.kb, enabled=True,
            fields=[{"label": "采集日期"}, {"label": "景点"}, {"label": "运行时长"}],
        )
        self.url = reverse("kb:feed_kb_data", args=[self.kb.slug])

    def run_and_row(self, day, attraction, hours, *, status="completed", feed_run=True):
        run = FeedRun.objects.create(
            feed=self.feed, status=status, lease_token="test",
            finished_at=timezone.now(), counts={"created": 1},
        )
        row = TrackerRow.objects.create(
            tracker=self.tracker, feed=self.feed, feed_run=run if feed_run else None,
            external_id=f"{day}-{attraction}", status="done",
            values={"采集日期": day, "景点": attraction, "运行时长": hours},
        )
        return run, row

    def test_auto_kb_home_shows_data_workspace_instead_of_file_list(self):
        page = self.client.get(reverse("kb:manage_detail", args=[self.kb.slug]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="feedKbData"')
        self.assertContains(page, 'id="feedKbChart"')
        self.assertContains(page, 'id="feedKbAggregate"')
        self.assertContains(page, 'id="feedKbSummaryBody"', count=1)
        self.assertNotContains(page, 'id="feedKbTrendBody"')
        self.assertNotContains(page, 'id="feedKbTrend"')
        self.assertContains(page, '>追踪表</a>')
        self.assertNotContains(page, '追踪表与趋势分析')
        self.assertNotContains(page, 'id="docList"')
        self.assertNotContains(page, 'id="uploadForm"')

    def test_latest_result_and_history_search_filter_and_pagination(self):
        first, _ = self.run_and_row("2026-11-09", "加勒比海盗", "10")
        latest, _ = self.run_and_row("2026-11-10", "飞越地平线", "11")
        current = self.client.get(self.url).json()
        self.assertEqual(current["latest_success"]["id"], str(latest.pk))
        self.assertEqual(current["total_matched"], 1)
        self.assertEqual(current["rows"][0]["values"]["景点"], "飞越地平线")
        history = self.client.get(self.url, {"scope": "history", "q": "加勒比"}).json()
        self.assertEqual(history["total_matched"], 1)
        self.assertEqual(history["rows"][0]["source_id"], "2026-11-09-加勒比海盗")
        filtered = self.client.get(self.url, {"scope": "history", "field": "采集日期", "value": "2026-11-10"}).json()
        self.assertEqual(filtered["total_matched"], 1)
        self.assertEqual(filtered["rows"][0]["values"]["景点"], "飞越地平线")
        self.assertEqual(self.client.get(self.url, {"scope": "history", "offset": 100}).json()["rows"], [])

    def test_summary_aggregates_all_matching_rows_and_numeric_strings(self):
        self.run_and_row("2026-11-09", "加勒比海盗", "10")
        self.run_and_row("2026-11-10", "加勒比海盗", "12")
        self.run_and_row("2026-11-10", "飞越地平线", "bad data")
        response = self.client.get(self.url, {
            "kind": "summary", "scope": "history", "group": "景点",
            "op": "sum", "measure": "运行时长",
        })
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        groups = {row["by"]["景点"]: row for row in data["groups"]}
        self.assertEqual(data["total_matched"], 3)
        self.assertEqual(groups["加勒比海盗"]["metrics"]["value"], 22)
        self.assertEqual(groups["加勒比海盗"]["valid_numeric_values"]["value"], 2)
        self.assertIsNone(groups["飞越地平线"]["metrics"]["value"])
        self.assertEqual(groups["飞越地平线"]["valid_numeric_values"]["value"], 0)

    def test_trend_compares_one_object_in_ten_minute_buckets(self):
        def at(minute, value, attraction="加勒比海盗"):
            _, row = self.run_and_row("2026-11-10", attraction, value)
            moment = datetime(2026, 11, 10, 10, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
            TrackerRow.objects.filter(pk=row.pk).update(updated_at=moment)

        at(3, "0")
        at(14, "12")
        at(24, "not a number")
        at(33, "15")
        at(14, "99", attraction="飞越地平线")
        response = self.client.get(self.url, {
            "kind": "trend", "object_field": "景点", "object_value": "加勒比海盗",
            "period": "10min", "op": "sum", "measure": "运行时长",
            "from": "2026-11-10", "to": "2026-11-10",
        })
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["timezone"], "Asia/Shanghai")
        points = data["points"]
        self.assertEqual(len(points), 144)
        self.assertIsNone(points[0]["value"])
        self.assertIsNone(points[-1]["value"])
        by_time = {item["label"][-5:]: item for item in points}
        self.assertEqual([by_time[key]["value"] for key in ("10:00", "10:10", "10:20", "10:30")],
                         [0, 12, None, 15])
        self.assertEqual(by_time["10:10"]["change"], 12)
        self.assertEqual(by_time["10:10"]["comparison"], "zero_baseline")
        self.assertIsNone(by_time["10:10"]["change_pct"])
        self.assertEqual(by_time["10:20"]["valid_numeric_values"], 0)
        self.assertEqual(by_time["10:30"]["comparison"], "no_previous_value")
        hourly = self.client.get(self.url, {
            "kind": "trend", "object_field": "景点", "object_value": "加勒比海盗",
            "period": "hour", "op": "avg", "measure": "运行时长",
            "from": "2026-11-10", "to": "2026-11-10",
        }).json()["points"]
        ten_oclock = next(point for point in hourly if point["label"].endswith("10:00"))
        self.assertEqual(ten_oclock["value"], 9)
        self.assertEqual(ten_oclock["valid_numeric_values"], 3)

    def test_trend_keeps_missing_period_separate_from_zero_and_checks_bounds(self):
        _, first = self.run_and_row("2026-11-10", "A", "2")
        _, second = self.run_and_row("2026-11-10", "A", "4")
        TrackerRow.objects.filter(pk=first.pk).update(
            updated_at=datetime(2026, 11, 10, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai")))
        TrackerRow.objects.filter(pk=second.pk).update(
            updated_at=datetime(2026, 11, 10, 10, 21, tzinfo=ZoneInfo("Asia/Shanghai")))
        params = {
            "kind": "trend", "object_field": "景点", "object_value": "A",
            "period": "10min", "op": "count", "from": "2026-11-10", "to": "2026-11-10",
        }
        result = self.client.get(self.url, params).json()
        by_time = {point["label"][-5:]: point for point in result["points"]}
        self.assertEqual([by_time[key]["value"] for key in ("10:00", "10:10", "10:20")], [1, None, 1])
        self.assertEqual(by_time["10:20"]["comparison"], "no_previous_value")
        self.assertEqual(self.client.get(self.url, {**params, "to": "2026-11-20"}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {**params, "object_field": "unknown"}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {**params, "measure": "unknown", "op": "avg"}).status_code, 400)

    def test_upsert_latest_reads_current_rows_and_failed_run_keeps_last_success(self):
        self.feed.write_mode = "upsert"
        self.feed.save(update_fields=["write_mode"])
        success, row = self.run_and_row("2026-11-10", "飞越地平线", "11", feed_run=False)
        failed = FeedRun.objects.create(feed=self.feed, status="failed", lease_token="test", error="network error")
        data = self.client.get(self.url).json()
        self.assertTrue(data["latest_is_current"])
        self.assertEqual(data["latest_run"]["id"], str(failed.pk))
        self.assertEqual(data["latest_success"]["id"], str(success.pk))
        self.assertEqual(data["rows"][0]["row_id"], row.pk)

    def test_saved_run_mode_survives_later_feed_mode_change(self):
        run, row = self.run_and_row("2026-11-10", "飞越地平线", "11")
        run.counts["write_mode"] = "append"
        run.save(update_fields=["counts"])
        self.feed.write_mode = "upsert"
        self.feed.save(update_fields=["write_mode"])
        data = self.client.get(self.url).json()
        self.assertFalse(data["latest_is_current"])
        self.assertEqual(data["rows"][0]["row_id"], row.pk)

    def test_wide_feed_fields_are_browsable_in_groups_of_twelve(self):
        self.tracker.fields += [{"label": f"字段{i}"} for i in range(13)]
        self.tracker.save(update_fields=["fields"])
        _, row = self.run_and_row("2026-11-10", "飞越地平线", "11")
        row.values["字段12"] = "展开后可见"
        row.save(update_fields=["values"])
        page = self.client.get(self.url, {"column_offset": 12}).json()
        self.assertEqual(page["columns"][0], "字段9")
        self.assertNotIn("景点", page["columns"])
        expanded = self.client.get(self.url, {"kind": "record", "row": row.pk}).json()
        self.assertEqual(expanded["values"]["字段12"], "展开后可见")
        self.assertEqual(self.client.get(self.url, {"column_offset": 1}).status_code, 400)

    def test_feed_selector_is_scoped_to_this_knowledge_base(self):
        other = KnowledgeBase.objects.create(name="别的库", slug="other-feed-page", category="business")
        foreign = KnowledgeFeed.objects.create(kb=other, name=other.name, mode="push")
        self.assertEqual(self.client.get(self.url, {"feed": str(foreign.pk)}).status_code, 404)
        other_tracker = KbTracker.objects.create(kb=other, fields=[{"label": "私有列"}])
        foreign_row = TrackerRow.objects.create(tracker=other_tracker, feed=foreign, status="done", values={"私有列": "secret"})
        self.assertEqual(self.client.get(self.url, {"kind": "record", "row": foreign_row.pk}).status_code, 404)
        self.assertEqual(self.client.get(self.url, {"field": "unknown", "value": "x"}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {"from": "2026-11-11", "to": "2026-11-10"}).status_code, 400)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_reader_permission_matches_knowledge_base_visibility(self):
        from accounts.models import UserProfile
        reader = get_user_model().objects.create_user("feed-page-reader")
        UserProfile.objects.update_or_create(user=reader, defaults={"department": "电气"})
        self.client.force_login(reader)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.kb.department = "机械"
        self.kb.save(update_fields=["department"])
        self.assertEqual(self.client.get(self.url).status_code, 404)
