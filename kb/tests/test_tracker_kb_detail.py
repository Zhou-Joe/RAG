from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from kb.models import Document, FeedRecord, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow


class TrackerKnowledgeBaseDetailTests(TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user("manual-tracker-manager", is_staff=True)
        self.client.force_login(self.manager)
        self.kb = KnowledgeBase.objects.create(
            name="设备报告", slug="manual-tracker-kb", is_folder=True, created_by=self.manager,
        )
        self.child = KnowledgeBase.objects.create(
            name="子文档", slug="manual-tracker-child", parent=self.kb, created_by=self.manager,
        )
        self.tracker = KbTracker.objects.create(
            kb=self.kb, enabled=True,
            fields=[{"label": "设备"}, {"label": "时长"}],
        )
        self.page_url = reverse("kb:manage_detail", args=[self.kb.slug])
        self.data_url = reverse("kb:tracker_kb_data", args=[self.kb.slug])

    def row(self, name, device, duration, *, status="done", child=None):
        document = Document.objects.create(
            kb=child or self.child, original_name=name, file_type="pdf", status="completed",
        )
        return TrackerRow.objects.create(
            tracker=self.tracker, document=document, status=status,
            values={"设备": device, "时长": duration},
        )

    def test_main_page_shows_tracker_views_and_folds_document_list_below(self):
        self.row("报告A.pdf", "A", "3")
        page = self.client.get(self.page_url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="feedKbData"')
        self.assertContains(page, 'id="feedKbAggregate"')
        self.assertContains(page, 'id="feedKbSummaryBody"', count=1)
        self.assertNotContains(page, 'id="feedKbTrendBody"')
        self.assertNotContains(page, 'id="feedKbTrend"')
        self.assertContains(page, '<details class="fkd-documents" id="kbDocumentDetails">')
        self.assertContains(page, 'id="docList"')
        self.assertContains(page, 'id="uploadForm"')
        body = page.content.decode()
        self.assertLess(body.index('id="feedKbAggregate"'), body.index('id="uploadForm"'))
        self.assertLess(body.index('id="uploadForm"'), body.index('id="kbDocumentDetails"'))

    def test_completed_manual_rows_search_filter_and_aggregate(self):
        first = self.row("报告A.pdf", "A", "3")
        self.row("报告B.pdf", "A", "4")
        self.row("待确认.pdf", "B", "99", status="proposed")
        feed = KnowledgeFeed.objects.create(kb=self.kb, name="别的采集源", mode="push")
        TrackerRow.objects.create(tracker=self.tracker, feed=feed, external_id="feed-1",
                                  status="done", values={"设备": "C", "时长": "50"})
        result = self.client.get(self.data_url).json()
        self.assertEqual(result["total_matched"], 2)
        self.assertEqual({row["document_name"] for row in result["rows"]}, {"报告A.pdf", "报告B.pdf"})
        self.assertEqual(self.client.get(self.data_url, {"q": "报告A"}).json()["total_matched"], 1)
        self.assertEqual(self.client.get(self.data_url, {"field": "设备", "value": "A"}).json()["total_matched"], 2)
        summary = self.client.get(self.data_url, {
            "kind": "summary", "group": "设备", "op": "sum", "measure": "时长",
        }).json()
        self.assertEqual(summary["total_matched"], 2)
        self.assertEqual(summary["groups"][0]["metrics"]["value"], 7)
        expanded = self.client.get(self.data_url, {"kind": "record", "row": first.pk}).json()
        self.assertEqual(expanded["values"], {"设备": "A", "时长": "3"})

    def test_manual_trend_uses_visible_documents_and_numeric_values(self):
        first = self.row("报告A.pdf", "A", "3")
        second = self.row("报告B.pdf", "A", "4")
        self.row("其它设备.pdf", "B", "99")
        TrackerRow.objects.filter(pk=first.pk).update(
            updated_at=datetime(2026, 11, 9, 8, tzinfo=ZoneInfo("Asia/Shanghai")))
        TrackerRow.objects.filter(pk=second.pk).update(
            updated_at=datetime(2026, 11, 10, 8, tzinfo=ZoneInfo("Asia/Shanghai")))
        response = self.client.get(self.data_url, {
            "kind": "trend", "object_field": "设备", "object_value": "A",
            "period": "day", "op": "sum", "measure": "时长",
            "from": "2026-11-09", "to": "2026-11-10",
        })
        self.assertEqual(response.status_code, 200, response.content)
        points = response.json()["points"]
        self.assertEqual([point["value"] for point in points], [3, 4])
        self.assertEqual(points[1]["change"], 1)
        self.assertAlmostEqual(points[1]["change_pct"], 33.33333333)

    def test_trend_includes_superseded_append_feed_documents_only(self):
        feed = KnowledgeFeed.objects.create(
            kb=self.kb, name=self.kb.name, mode="push", write_mode="append",
            adapter_spec={"ingest_mode": "documents"},
        )
        FeedRecord.objects.create(feed=feed, external_id="A", kb=self.child)
        old_doc = Document.objects.create(kb=self.child, original_name="old.md",
                                          status=Document.Status.SUPERSEDED)
        new_doc = Document.objects.create(kb=self.child, original_name="new.md",
                                          status=Document.Status.COMPLETED)
        old_row = TrackerRow.objects.create(tracker=self.tracker, document=old_doc,
                                            status="done", values={"设备": "A", "时长": "3"})
        new_row = TrackerRow.objects.create(tracker=self.tracker, document=new_doc,
                                            status="done", values={"设备": "A", "时长": "5"})
        TrackerRow.objects.filter(pk=old_row.pk).update(
            updated_at=datetime(2026, 11, 9, 8, tzinfo=ZoneInfo("Asia/Shanghai")))
        TrackerRow.objects.filter(pk=new_row.pk).update(
            updated_at=datetime(2026, 11, 10, 8, tzinfo=ZoneInfo("Asia/Shanghai")))
        params = {
            "kind": "trend", "object_field": "设备", "object_value": "A",
            "period": "day", "op": "sum", "measure": "时长",
            "from": "2026-11-09", "to": "2026-11-10",
        }
        self.assertEqual(self.client.get(self.data_url).json()["total_matched"], 1)
        self.assertEqual([point["value"] for point in self.client.get(self.data_url, params).json()["points"]], [3, 5])
        feed.write_mode = "upsert"
        feed.save(update_fields=["write_mode"])
        self.assertEqual([point["value"] for point in self.client.get(self.data_url, params).json()["points"]], [None, 5])
        feed.write_mode = "append"
        feed.save(update_fields=["write_mode"])
        self.child.department = "机械"
        self.child.save(update_fields=["department"])
        from accounts.models import UserProfile
        reader = get_user_model().objects.create_user("history-reader")
        UserProfile.objects.update_or_create(user=reader, defaults={"department": "电气"})
        self.client.force_login(reader)
        self.assertTrue(all(point["value"] is None for point in
                            self.client.get(self.data_url, params).json()["points"]))

    def test_record_scope_and_reader_access(self):
        from accounts.models import UserProfile
        row = self.row("报告A.pdf", "A", "3")
        restricted_child = KnowledgeBase.objects.create(
            name="机械文档", slug="manual-tracker-restricted", parent=self.kb, department="机械",
        )
        hidden_row = self.row("机械报告.pdf", "M", "9", child=restricted_child)
        other = KnowledgeBase.objects.create(name="其它", slug="manual-tracker-other")
        other_tracker = KbTracker.objects.create(kb=other, enabled=True, fields=[{"label": "设备"}])
        foreign_doc = Document.objects.create(kb=other, original_name="私有.pdf", status="completed")
        foreign_row = TrackerRow.objects.create(
            tracker=other_tracker, document=foreign_doc, status="done", values={"设备": "secret"},
        )
        self.assertEqual(self.client.get(self.data_url, {"kind": "record", "row": foreign_row.pk}).status_code, 404)
        reader = get_user_model().objects.create_user("manual-tracker-reader")
        UserProfile.objects.update_or_create(user=reader, defaults={"department": "电气"})
        self.client.force_login(reader)
        self.assertEqual(self.client.get(self.data_url).status_code, 200)
        self.assertEqual(self.client.get(self.data_url).json()["total_matched"], 1)
        self.assertEqual(self.client.get(self.data_url, {"kind": "record", "row": row.pk}).status_code, 200)
        self.assertEqual(self.client.get(self.data_url, {"kind": "record", "row": hidden_row.pk}).status_code, 404)
        self.tracker.enabled = False
        self.tracker.save(update_fields=["enabled"])
        self.assertEqual(self.client.get(self.data_url).status_code, 404)
        page = self.client.get(self.page_url)
        self.assertNotContains(page, 'id="feedKbAggregate"')

    def test_plain_knowledge_base_keeps_document_page(self):
        plain = KnowledgeBase.objects.create(name="普通资料", slug="manual-plain-kb", created_by=self.manager)
        page = self.client.get(reverse("kb:manage_detail", args=[plain.slug]))
        self.assertContains(page, 'id="docList"')
        self.assertNotContains(page, 'id="feedKbData"')
        self.assertNotContains(page, 'id="kbDocumentDetails"')
        self.assertEqual(self.client.get(reverse("kb:tracker_kb_data", args=[plain.slug])).status_code, 404)

    def test_record_pagination_loads_additional_manual_rows(self):
        for number in range(27):
            self.row(f"报告{number}.pdf", "A", str(number))
        first = self.client.get(self.data_url).json()
        self.assertEqual(first["total_matched"], 27)
        self.assertEqual(len(first["rows"]), 25)
        self.assertTrue(first["has_more"])
        second = self.client.get(self.data_url, {"offset": first["next_offset"]}).json()
        self.assertEqual(len(second["rows"]), 2)
        self.assertFalse(second["has_more"])
