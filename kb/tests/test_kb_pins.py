"""Favorite KBs are private to a user and limited to visible top-level libraries."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from kb.models import KnowledgeBase, KnowledgeBasePin


class KnowledgeBasePinTests(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.reader = users.create_user("pin-reader")
        self.other = users.create_user("pin-other", is_staff=True)
        self.general = KnowledgeBase.objects.create(
            name="General manual", slug="pin-general", is_folder=True,
            created_by=self.other,
        )
        self.business = KnowledgeBase.objects.create(
            name="General feed", slug="pin-business", is_folder=True,
            category="business", created_by=self.other,
        )
        self.private = KnowledgeBase.objects.create(
            name="Private manual", slug="pin-private", is_folder=True,
            visibility="individual", created_by=self.reader,
        )
        self.foreign_private = KnowledgeBase.objects.create(
            name="Other private", slug="pin-other-private", is_folder=True,
            visibility="individual", created_by=self.other,
        )
        self.child = KnowledgeBase.objects.create(
            name="Child", slug="pin-child", parent=self.general,
            created_by=self.other,
        )
        self.client.force_login(self.reader)

    def pin(self, kb, value):
        return self.client.post(reverse("kb:kb_pin", args=[kb.slug]), {"pinned": value})

    def test_favorites_appear_once_above_regular_categories_in_pin_order(self):
        for kb in (self.private, self.general, self.business):
            response = self.pin(kb, "1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ok": True, "slug": kb.slug, "pinned": True})

        page = self.client.get(reverse("kb:manage_list"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual([kb.slug for kb in page.context["pinned_kbs"]],
                         [self.business.slug, self.general.slug, self.private.slug])
        ordinary = [*page.context["kbs"], *page.context["business_kbs"],
                    *page.context["read_kbs"]]
        self.assertFalse(set(kb.slug for kb in ordinary) &
                         {self.business.slug, self.general.slug, self.private.slug})
        self.assertFalse(page.context["pinned_kbs"][1].can_manage)
        self.assertTrue(all(kb.is_pinned for kb in page.context["pinned_kbs"]))

        self.client.force_login(self.other)
        other_page = self.client.get(reverse("kb:manage_list"))
        self.assertFalse(other_page.context["pinned_kbs"])
        self.assertEqual(KnowledgeBasePin.objects.filter(user=self.reader).count(), 3)

    def test_unpin_is_idempotent_and_returns_to_original_category(self):
        self.pin(self.general, "1")
        self.assertEqual(self.pin(self.general, "1").status_code, 200)
        self.assertEqual(KnowledgeBasePin.objects.filter(user=self.reader, kb=self.general).count(), 1)
        response = self.pin(self.general, "0")
        self.assertEqual(response.json()["pinned"], False)
        self.assertEqual(self.pin(self.general, "0").status_code, 200)
        page = self.client.get(reverse("kb:manage_list"))
        self.assertFalse(page.context["pinned_kbs"])
        self.assertIn(self.general.slug, [kb.slug for kb in page.context["read_kbs"]])

    def test_hidden_and_child_kbs_cannot_be_pinned_or_disclosed(self):
        for kb in (self.foreign_private, self.child):
            self.assertEqual(self.pin(kb, "1").status_code, 404)
        self.assertFalse(KnowledgeBasePin.objects.filter(user=self.reader).exists())
        self.assertEqual(self.pin(self.general, "invalid").status_code, 400)
        self.assertFalse(KnowledgeBasePin.objects.filter(user=self.reader).exists())
        self.assertEqual(self.client.get(reverse("kb:kb_pin", args=[self.general.slug])).status_code, 405)

    def test_pinned_kb_disappears_when_it_is_no_longer_visible(self):
        self.assertEqual(self.pin(self.general, "1").status_code, 200)
        self.general.department = "Operations"
        self.general.save(update_fields=["department"])
        page = self.client.get(reverse("kb:manage_list"))
        self.assertNotIn(self.general.slug, [kb.slug for kb in page.context["pinned_kbs"]])
        self.assertEqual(self.pin(self.general, "0").status_code, 404)
        self.assertTrue(KnowledgeBasePin.objects.filter(user=self.reader, kb=self.general).exists())
