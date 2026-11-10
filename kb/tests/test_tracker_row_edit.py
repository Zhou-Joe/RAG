"""Tracksheet cell edits and record deletion are scoped to one managed KB."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from kb.models import Document, KbTracker, KnowledgeBase, KnowledgeFeed, TrackerRow


class TrackerRowEditTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.manager = users.objects.create_user("row-editor", is_staff=True)
        self.reader = users.objects.create_user("row-reader")
        self.kb = KnowledgeBase.objects.create(
            name="记录库", slug="row-edit-kb", is_folder=True, created_by=self.manager)
        self.child = KnowledgeBase.objects.create(
            name="文档", slug="row-edit-doc", parent=self.kb, created_by=self.manager)
        self.tracker = KbTracker.objects.create(
            kb=self.kb, enabled=True, fields=[{"label": "景点"}, {"label": "时长"}])
        self.document = Document.objects.create(
            kb=self.child, original_name="report.txt", status=Document.Status.COMPLETED)
        self.manual = TrackerRow.objects.create(
            tracker=self.tracker, document=self.document, status=TrackerRow.Status.DONE,
            values={"景点": "加勒比海盗", "时长": "12"})
        self.feed = KnowledgeFeed.objects.create(
            kb=self.kb, name=self.kb.name, mode="push", created_by=self.manager)
        self.collected = TrackerRow.objects.create(
            tracker=self.tracker, feed=self.feed, external_id="ride-1",
            status=TrackerRow.Status.DONE, values={"景点": "飞越地平线", "时长": "10"})
        self.url = reverse("kb:tracker", args=[self.kb.slug])
        self.ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        self.client.force_login(self.manager)

    def test_edits_one_manual_or_feed_cell_without_changing_other_fields(self):
        for row, new_value in ((self.manual, "15.5"), (self.collected, "16")):
            with self.subTest(row=row.pk):
                response = self.client.post(self.url, {
                    "action": "cell_edit", "row_id": row.pk,
                    "field": "时长", "value": new_value,
                }, **self.ajax)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertTrue(response.json()["ok"])
                self.assertEqual(response.json()["row_id"], row.pk)
                row.refresh_from_db()
                self.assertEqual(row.values["时长"], new_value)
                self.assertIn("景点", row.values)
                self.assertEqual(row.status, TrackerRow.Status.DONE)

    def test_cell_edit_validates_field_length_and_row_state(self):
        original = dict(self.manual.values)
        for payload in (
            {"row_id": self.manual.pk, "field": "不存在", "value": "x"},
            {"row_id": self.manual.pk, "field": "时长", "value": "x" * 2001},
            {"row_id": "bad", "field": "时长", "value": "x"},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(self.url, {"action": "cell_edit", **payload}, **self.ajax)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.values, original)
        self.manual.status = TrackerRow.Status.RUNNING
        self.manual.save(update_fields=["status"])
        response = self.client.post(self.url, {
            "action": "cell_edit", "row_id": self.manual.pk,
            "field": "时长", "value": "17",
        }, **self.ajax)
        self.assertEqual(response.status_code, 409)
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.values, original)

    def test_exactly_2000_characters_and_stale_version(self):
        original_version = self.manual.updated_at.isoformat()
        response = self.client.post(self.url, {
            "action": "cell_edit", "row_id": self.manual.pk,
            "field": "时长", "value": "x" * 2000,
            "expected_updated_at": original_version,
            "expected_value": json.dumps("12"),
        }, **self.ajax)
        self.assertEqual(response.status_code, 200)
        self.manual.refresh_from_db()
        self.assertEqual(len(self.manual.values["时长"]), 2000)
        self.assertEqual(self.manual.updated_at.isoformat(), original_version)
        stale = self.client.post(self.url, {
            "action": "cell_edit", "row_id": self.manual.pk,
            "field": "时长", "value": "wrong",
            "expected_updated_at": original_version,
            "expected_value": json.dumps("12"),
        }, **self.ajax)
        self.assertEqual(stale.status_code, 409)
        self.manual.refresh_from_db()
        self.assertEqual(len(self.manual.values["时长"]), 2000)

    def test_bulk_delete_is_atomic_and_leaves_source_documents_and_feeds(self):
        response = self.client.post(self.url, {
            "action": "rows_delete", "row_ids": json.dumps([self.manual.pk, self.collected.pk]),
        }, **self.ajax)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["deleted"], 2)
        self.assertFalse(TrackerRow.objects.filter(pk__in=[self.manual.pk, self.collected.pk]).exists())
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())
        self.assertTrue(KnowledgeFeed.objects.filter(pk=self.feed.pk).exists())

    def test_in_flight_row_blocks_both_single_and_bulk_delete(self):
        self.manual.status = TrackerRow.Status.RUNNING
        self.manual.save(update_fields=["status"])
        single = self.client.post(self.url, {
            "action": "row_delete", "row_id": self.manual.pk,
        }, **self.ajax)
        self.assertEqual(single.status_code, 409)
        bulk = self.client.post(self.url, {
            "action": "rows_delete", "row_ids": json.dumps([self.manual.pk, self.collected.pk]),
        }, **self.ajax)
        self.assertEqual(bulk.status_code, 409)
        self.assertTrue(TrackerRow.objects.filter(pk=self.manual.pk).exists())
        self.assertTrue(TrackerRow.objects.filter(pk=self.collected.pk).exists())

    def test_object_cell_accepts_read_api_representation_for_stale_guard(self):
        self.collected.values = {"景点": {"名称": "飞越地平线"}, "时长": 10}
        self.collected.save(update_fields=["values"])
        shown = json.dumps({"名称": "飞越地平线"}, ensure_ascii=False, separators=(",", ":"))
        response = self.client.post(self.url, {
            "action": "cell_edit", "row_id": self.collected.pk, "field": "景点",
            "value": "新名称", "expected_value": json.dumps(shown, ensure_ascii=False),
        }, **self.ajax)
        self.assertEqual(response.status_code, 200, response.content)
        self.collected.refresh_from_db()
        self.assertEqual(self.collected.values["景点"], "新名称")

    def test_invalid_or_foreign_id_never_partially_deletes(self):
        other_kb = KnowledgeBase.objects.create(
            name="其他库", slug="other-row-kb", created_by=self.manager)
        other_feed = KnowledgeFeed.objects.create(
            kb=other_kb, name="其他采集", mode="push", created_by=self.manager)
        foreign = TrackerRow.objects.create(
            tracker=self.tracker, feed=other_feed, external_id="secret",
            status=TrackerRow.Status.DONE, values={"时长": "99"})
        response = self.client.post(self.url, {
            "action": "rows_delete", "row_ids": json.dumps([self.manual.pk, foreign.pk]),
        }, **self.ajax)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(TrackerRow.objects.filter(pk=self.manual.pk).exists())
        self.assertTrue(TrackerRow.objects.filter(pk=foreign.pk).exists())
        response = self.client.post(self.url, {
            "action": "cell_edit", "row_id": foreign.pk, "field": "时长", "value": "1",
        }, **self.ajax)
        self.assertEqual(response.status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.values["时长"], "99")
        for invalid in ("[]", "[true]", "[1,1]", json.dumps(list(range(1, 502)))):
            response = self.client.post(self.url, {
                "action": "rows_delete", "row_ids": invalid,
            }, **self.ajax)
            self.assertEqual(response.status_code, 400)

    def test_read_only_account_cannot_edit_or_delete(self):
        self.client.force_login(self.reader)
        for payload in (
            {"action": "cell_edit", "row_id": self.manual.pk,
             "field": "时长", "value": "0"},
            {"action": "rows_delete", "row_ids": json.dumps([self.manual.pk])},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(self.url, payload, **self.ajax)
                self.assertEqual(response.status_code, 403)
                self.assertFalse(response.json()["ok"])
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.values["时长"], "12")
        self.assertTrue(TrackerRow.objects.filter(pk=self.manual.pk).exists())
