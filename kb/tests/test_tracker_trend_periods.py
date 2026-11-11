"""自定义周期（每 N 个单位）与多对象对比：`kind=trend` 的契约。

旧接口只有 10min/hour/day/week/month 五个固定周期，想按「每 30 分钟」或「每 2 天」
看趋势做不到；而且一次只能看一个对象，多条曲线没法对比。
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from django.utils import timezone

from kb.models import FeedRun, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow

ZONE = ZoneInfo("Asia/Shanghai")


class TrendPeriodTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("trend-admin", "t@e.test", "pw")
        self.client.force_login(self.admin)
        self.kb = KnowledgeBase.objects.create(
            name="等待时间", slug="trend-kb", is_folder=True, category="business",
            created_by=self.admin)
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name="Attraction", mode="push",
                                                 created_by=self.admin)
        self.tracker = KbTracker.objects.create(
            kb=self.kb, enabled=True,
            fields=[{"label": "景点"}, {"label": "排队时间(分钟)"}])
        self.url = reverse("kb:feed_kb_data", args=[self.kb.slug])

    def row(self, name, value, moment, run=None):
        row = TrackerRow.objects.create(
            tracker=self.tracker, feed=self.feed, feed_run=run, status="done",
            external_id=f"{name}-{moment.isoformat()}",
            values={"景点": name, "排队时间(分钟)": value})
        # updated_at 是 auto_now，建完再改（周期就是按它切的）
        TrackerRow.objects.filter(pk=row.pk).update(updated_at=moment)
        return row

    def get(self, **extra):
        params = {"kind": "trend", "object_field": "景点", "op": "count", "scope": "history",
                  "from": "2026-11-10", "to": "2026-11-10"}
        params.update(extra)
        response = self.client.get(self.url, params)
        return response, response.json() if response["Content-Type"].startswith("application/json") else {}

    def test_custom_span_groups_every_30_minutes(self):
        base = datetime(2026, 11, 10, 10, 0, tzinfo=ZONE)
        for minute in (2, 14, 31, 45, 61):
            self.row("A", "5", base + timedelta(minutes=minute))
        response, data = self.get(period="minute", span="30", object_value="A")
        self.assertEqual(response.status_code, 200, data)
        self.assertEqual(data["period_unit"], "minute")
        self.assertEqual(data["span"], 30)
        self.assertEqual(data["period_label"], "每 30 分钟")
        by_label = {point["label"][-5:]: point for point in data["points"]}
        # 每 30 分钟一桶：10:02/10:14 → 10:00；10:31/10:45 → 10:30；11:01 → 11:00
        self.assertEqual([by_label[key]["value"] for key in ("10:00", "10:30", "11:00")],
                         [2, 2, 1])
        self.assertEqual(by_label["10:30"]["records"], 2)
        # 每 30 分钟一天的桶数是 48（不是 144）
        self.assertEqual(len(data["points"]), 48)

    def test_every_two_days_and_year_unit(self):
        self.row("A", "5", datetime(2026, 11, 10, 9, 0, tzinfo=ZONE))
        self.row("A", "6", datetime(2026, 11, 11, 9, 0, tzinfo=ZONE))
        self.row("A", "7", datetime(2026, 11, 12, 9, 0, tzinfo=ZONE))
        _, data = self.get(period="day", span="2", object_value="A", **{"to": "2026-11-13"})
        # 锚点是窗口起点：11-10 与 11-11 落在同一个「每 2 天」桶里
        self.assertEqual([point["value"] for point in data["points"]], [2, 1])
        self.assertEqual([point["label"] for point in data["points"]], ["2026-11-10", "2026-11-12"])
        response, yearly = self.get(period="year", span="1", object_value="A",
                                    **{"from": "2020-01-01", "to": "2026-12-31"})
        self.assertEqual(response.status_code, 200, yearly)
        self.assertEqual(len(yearly["points"]), 7)        # 2020…2026 一年一桶
        self.assertEqual(yearly["points"][-1]["label"], "2026")
        self.assertEqual(yearly["points"][-1]["value"], 3)

    def test_legacy_period_names_still_work(self):
        self.row("A", "5", datetime(2026, 11, 10, 10, 3, tzinfo=ZONE))
        self.row("A", "6", datetime(2026, 11, 10, 10, 41, tzinfo=ZONE))
        _, legacy = self.get(period="10min", object_value="A")
        _, modern = self.get(period="minute", span="10", object_value="A")
        self.assertEqual([p["value"] for p in legacy["points"]], [p["value"] for p in modern["points"]])

    def test_multiple_objects_return_one_series_each(self):
        run = FeedRun.objects.create(feed=self.feed, status="completed", lease_token="t",
                                     finished_at=timezone.now(), counts={"created": 1})
        self.row("A", "5", datetime(2026, 11, 10, 10, 0, tzinfo=ZONE), run)
        self.row("B", "9", datetime(2026, 11, 10, 10, 20, tzinfo=ZONE), run)
        self.row("B", "4", datetime(2026, 11, 10, 11, 0, tzinfo=ZONE), run)
        response = self.client.get(self.url, {
            "kind": "trend", "object_field": "景点", "period": "hour", "op": "count",
            "scope": "history", "from": "2026-11-10", "to": "2026-11-10",
            "object_value": ["A", "B", "B"]})
        data = response.json()
        self.assertEqual(response.status_code, 200, data)
        self.assertEqual(data["object_values"], ["A", "B"])
        self.assertEqual([item["object_value"] for item in data["series"]], ["A", "B"])
        # 窗口从当天 00:00 起算，所以 10:00 是第 11 个桶
        self.assertEqual(len(data["series"][0]["points"]), 24)
        self.assertEqual([p["value"] for p in data["series"][0]["points"]][10], 1)
        self.assertEqual([p["value"] for p in data["series"][1]["points"]][10], 1)
        self.assertEqual([p["value"] for p in data["series"][1]["points"]][11], 1)
        # 兼容旧调用：points 仍是第一条曲线
        self.assertEqual(data["points"], data["series"][0]["points"])
        self.assertEqual(data["object_value"], "A")

    def test_rejects_too_many_objects_and_bad_spans(self):
        self.row("A", "5", datetime(2026, 11, 10, 10, 0, tzinfo=ZONE))
        too_many = [f"obj-{index}" for index in range(9)]
        response = self.client.get(self.url, {
            "kind": "trend", "object_field": "景点", "period": "hour", "op": "count",
            "scope": "history", "from": "2026-11-10", "to": "2026-11-10",
            "object_value": too_many})
        self.assertEqual(response.status_code, 400)
        self.assertIn("最多同时对比", response.json()["message"])
        for bad in ({"period": "minute", "span": "0"}, {"period": "minute", "span": "181"},
                    {"period": "fortnight", "span": "1"}, {"period": "day", "span": "abc"},
                    {"period": "day", "span": "1", "object_value": []}):
            with self.subTest(**bad):
                self.assertEqual(self.client.get(self.url, {
                    "kind": "trend", "object_field": "景点", "scope": "history",
                    "from": "2026-11-10", "to": "2026-11-10", **bad}).status_code, 400)

    def test_period_over_limit_still_rejected(self):
        self.row("A", "5", datetime(2026, 11, 10, 10, 0, tzinfo=ZONE))
        response, data = self.get(period="minute", span="1", object_value="A",
                                  **{"from": "2026-01-01", "to": "2026-11-10"})
        self.assertEqual(response.status_code, 400, data)
        self.assertIn("周期", data["message"])
