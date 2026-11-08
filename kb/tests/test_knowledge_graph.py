from __future__ import annotations

"""图谱事实生命周期、权限、歧义及与原文检索的集成回归。"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from kb.knowledge_graph import (
    attach_document, normalize_identifier, relation_current, relation_queryset,
    retrieve_passages, review_relation, seed_dataset, walk_graph,
)
from kb.models import Document, GraphEntity, GraphRelation, KnowledgeBase, StructuredDataset
from kb.structured_data import import_structured_dataset, lookup_related


class KnowledgeGraphTests(TestCase):
    def test_missing_graph_action_ids_return_useful_validation_errors(self):
        self.client.force_login(self.admin)
        url = reverse('kb:graph_action')
        for action, field in [('attach', 'relation'), ('review', 'relation'), ('bind', 'dataset')]:
            for value in ['', 'invalid', '-1', '0', '9999999999999999999999999999999']:
                response = self.client.post(url, {'kb': self.kb.slug, 'action': action, field: value})
                self.assertEqual(response.status_code, 400)
                self.assertIn('请先', response.json()['error'])
                self.assertNotIn("Field 'id'", response.json()['error'])
        response = self.client.post(url, {'kb': self.kb.slug, 'action': 'attach', 'relation': '1', 'document': ''})
        self.assertEqual(response.json()['error'], '请选择要关联的手册。')
        page = self.client.get(reverse('kb:graph'), {'kb': self.kb.slug})
        self.assertContains(page, 'id="attachSave" disabled')

    def test_direct_facts_publication_citations_and_revocation(self):
        from kb.knowledge_graph import lookup_facts
        from kb.publication import validate_sources, visible_citations, citation_key
        self.upload()
        self.verify_all()
        facts = lookup_facts([self.kb.slug], 'E-1')['facts']
        self.assertEqual(len(facts), 2)
        self.assertTrue(validate_sources(facts, self.reader.pk))
        citations = [{'kind': 'graph', 'relation_id': f['relation_id'],
                      'fact_digest': f['fact_digest'], 'graph_snapshot': f} for f in facts]
        self.assertEqual(len({citation_key(c) for c in citations}), 2)
        self.assertTrue(visible_citations(citations, self.reader))
        self.client.force_login(self.reader)
        url = reverse('kb:graph_fact', args=[facts[0]['relation_id']])
        self.assertContains(self.client.get(url, {'v': facts[0]['fact_digest']}), '当前来源有效')
        self.assertEqual(self.client.get(url, {'v': 'old-version'}).status_code, 410)
        relation = relation_queryset().get(pk=facts[0]['relation_id'])
        review_relation(relation, 'rejected', self.admin)
        self.assertFalse(validate_sources(facts, self.reader.pk))
        self.assertFalse(visible_citations(citations, self.reader))
        self.assertEqual(self.client.get(url).status_code, 410)

    def test_direct_fact_tool_records_evidence_without_vector_search(self):
        import json
        from kb.agent import _build_agent
        self.upload()
        self.verify_all()
        evidence, citations = [], []
        with patch('langchain.agents.create_agent') as create, patch('kb.agent._get_llm'):
            _build_agent(self.kb.slug, 'graph-test', {}, 5, None,
                         evidence=evidence, citations=citations, allowed_kb_slugs=[self.kb.slug])
        tools = {tool.__name__: tool for tool in create.call_args.kwargs['tools']}
        with patch('kb.retriever.search') as search:
            result = json.loads(tools['kb_graph_search']('E-1',kb_slug=self.kb.slug))
            search.assert_not_called()
        self.assertEqual(len(result['facts']), 2)
        self.assertEqual(len(evidence), 2)
        self.assertEqual(len(citations), 2)
        self.assertIn('不存在', tools['kb_graph_search']('E-1', kb_slug=self.child.slug))
        with patch('kb.analysis_runs.register') as register:
            self.assertIn('check_analysis', tools['write_analysis']('print(1)'))
        register.assert_called_once()

    def test_fact_snapshot_rejects_changed_source_scope_and_tampered_text(self):
        from kb.knowledge_graph import lookup_facts
        from kb.publication import validate_sources
        ds = self.upload()
        self.verify_all()
        facts = lookup_facts([self.kb.slug], 'E-1')['facts']
        self.assertFalse(validate_sources([{**facts[0], 'text': 'invented fact'}], self.reader.pk))
        self.kb.department = 'private'
        self.kb.save(update_fields=['department'])
        self.assertFalse(validate_sources(facts, self.reader.pk))
        self.assertTrue(validate_sources(facts, self.admin.pk))
        row = ds.records.get()
        row.raw_data['图纸号'] = 'changed'
        row.save(update_fields=['raw_data'])
        self.assertFalse(validate_sources(facts, self.admin.pk))

    def setUp(self):
        self.admin = get_user_model().objects.create_user("graph-admin", is_staff=True)
        self.reader = get_user_model().objects.create_user("graph-reader")
        self.kb = KnowledgeBase.objects.create(name="设备库", slug="graph", is_folder=True)
        self.child = KnowledgeBase.objects.create(name="手册", slug="graph-manual", parent=self.kb)
        self.doc = Document.objects.create(kb=self.child, original_name="manual.txt", file_type="txt",
            status=Document.Status.COMPLETED, md_content="部件 P-1 的安装检查要求见图纸 D-1，检查连接是否牢固。")

    def upload(self, text="设备号,部件号,图纸号\nE-1,P-1,D-1\n", name="parts.csv", kb=None):
        return import_structured_dataset(SimpleUploadedFile(name, text.encode("utf-8-sig")),
                                         "parts", self.admin, kb=kb or self.kb)

    def verify_all(self):
        for relation in relation_queryset():
            review_relation(relation, "verified", self.admin)

    def drawing_relation(self):
        return relation_queryset().get(predicate="documented_by")

    def test_import_candidates_require_review_and_backfill_is_idempotent(self):
        ds = self.upload()
        self.assertEqual(GraphEntity.objects.count(), 3)
        self.assertEqual(GraphRelation.objects.count(), 2)
        self.assertEqual(walk_graph([self.kb.slug], "E-1")["paths"], [])
        self.verify_all()
        self.assertEqual(seed_dataset(ds), 0)
        self.assertEqual(GraphRelation.objects.filter(status="verified").count(), 2)
        paths = walk_graph([self.kb.slug], "E-1")["paths"]
        self.assertEqual([len(p) for p in paths], [1, 2])
        self.assertEqual(paths[-1][-1].object.label, "D-1")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("kb:graph"), {"kb": self.kb.slug, "q": "E-1"})
        self.assertContains(page, "P-1 → 对应图纸 → D-1")

    def test_identifier_matching_preserves_punctuation_and_boundaries(self):
        self.upload()
        self.verify_all()
        self.assertNotEqual(normalize_identifier("P-1"), normalize_identifier("P1"))
        self.assertEqual(walk_graph([self.kb.slug], "P-10")["paths"], [])
        self.assertTrue(walk_graph([self.kb.slug], "设备E-1的图纸是什么？")["paths"])

    def test_same_part_number_on_two_equipment_is_ambiguous(self):
        self.upload("设备号,部件号,图纸号\nE-1,P-1,D-1\nE-2,P-1,D-2\n")
        self.verify_all()
        self.assertTrue(walk_graph([self.kb.slug], "P-1")["ambiguous"])
        result = walk_graph([self.kb.slug], "E-1 P-1")
        self.assertFalse(result["ambiguous"])
        self.assertTrue(result["paths"])
        self.assertNotIn("D-2", [r.object.label for p in result["paths"] for r in p])

    def test_replaced_dataset_does_not_resurrect_via_manual_evidence(self):
        old = self.upload()
        relation = self.drawing_relation()
        attach_document(relation, self.doc, self.doc.md_content, self.admin)
        self.verify_all()
        self.upload("设备号,部件号,图纸号\nE-1,P-1,D-2\n")
        old.refresh_from_db()
        self.assertFalse(old.active)
        self.assertFalse(relation_current(self.drawing_relation_old(relation.pk)))
        self.assertEqual(walk_graph([self.kb.slug], "E-1")["paths"], [])
        self.verify_all_current()
        result = walk_graph([self.kb.slug], "E-1")
        self.assertEqual(result["paths"][-1][-1].object.label, "D-2")

    def drawing_relation_old(self, pk):
        return relation_queryset().get(pk=pk)

    def verify_all_current(self):
        for relation in relation_queryset():
            if relation_current(relation):
                review_relation(relation, "verified", self.admin)

    def test_dataset_replacement_is_scoped_and_auto_kind_is_resolved_first(self):
        original = self.upload()
        other = KnowledgeBase.objects.create(name="其它", slug="graph-other")
        self.upload(kb=other)
        original.refresh_from_db()
        self.assertTrue(original.active)
        for text in ["设备号,部件号\nE-1,P-1\n", "设备号,部件号\nE-1,P-2\n"]:
            import_structured_dataset(SimpleUploadedFile("auto.csv", text.encode()), "", self.admin, kb=self.kb)
        self.assertEqual(StructuredDataset.objects.filter(source_name="auto.csv", active=True).count(), 1)

    @patch("kb.keyword_index.get_chunks")
    def test_graph_recalls_original_chunks_and_survives_new_chunk_ids(self, chunks):
        self.upload()
        relation = self.drawing_relation()
        attach_document(relation, self.doc, self.doc.md_content, self.admin)
        self.verify_all()
        for cid in ("old-model-id", "new-model-id"):
            chunks.return_value = [{"chunk_id": cid, "text": self.doc.md_content, "section": "安装"}]
            hits = retrieve_passages([self.kb.slug], [self.child.slug], "E-1")
            self.assertEqual(hits[0]["text"], self.doc.md_content)
            self.assertEqual(hits[0]["chunk_id"], cid)
            self.assertEqual(hits[0]["doc_id"], str(self.doc.pk))
            self.assertIn("E-1", hits[0]["graph_paths"][0])
        self.assertEqual(retrieve_passages([self.kb.slug], ["unrelated"], "E-1"), [])
        self.doc.md_content += "来源更新"
        self.doc.save(update_fields=["md_content"])
        self.assertEqual(retrieve_passages([self.kb.slug], [self.child.slug], "E-1"), [])

    def test_attaching_evidence_resets_review_and_rejects_cross_scope(self):
        self.upload()
        self.verify_all()
        relation = self.drawing_relation()
        attach_document(relation, self.doc, self.doc.md_content, self.admin)
        relation.refresh_from_db()
        self.assertEqual(relation.status, "candidate")
        other = KnowledgeBase.objects.create(name="外部", slug="external")
        self.doc.kb = other
        self.doc.save(update_fields=["kb"])
        with self.assertRaises(ValueError):
            attach_document(relation, self.doc, self.doc.md_content, self.admin)
        with self.assertRaises(ValueError):
            review_relation(relation, "verified", self.reader)

    def test_changed_row_invalidates_review_and_deleted_row_removes_relation(self):
        dataset = self.upload()
        self.verify_all()
        row = dataset.records.get()
        row.part_no = "P-CHANGED"
        row.save(update_fields=["part_no"])
        self.assertEqual(walk_graph([self.kb.slug], "E-1")["paths"], [])
        with self.assertRaises(ValueError):
            review_relation(self.drawing_relation(), "verified", self.admin)
        row.delete()
        self.assertFalse(GraphRelation.objects.exists())

    def test_graph_page_permissions_ajax_and_csrf(self):
        from django.test import Client
        self.upload()
        self.client.force_login(self.admin)
        url = reverse("kb:graph")
        self.assertContains(self.client.get(url, {"kb": self.kb.slug}), "包含部件")
        response = self.client.get(url, {"kb": self.kb.slug}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertIn("对应图纸", response.json()["html"])
        relation = self.drawing_relation()
        post = {"kb": self.kb.slug, "action": "review", "relation": relation.pk, "status": "verified"}
        self.assertEqual(self.client.post(reverse("kb:graph_action"), post).status_code, 200)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.post(reverse("kb:graph_action"), post).status_code, 404)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.admin)
        self.assertEqual(protected.post(reverse("kb:graph_action"), post).status_code, 403)
        self.kb.department = "机密部门"
        self.kb.save(update_fields=["department"])
        self.assertEqual(self.client.get(url, {"kb": self.kb.slug}).status_code, 404)
        self.assertEqual(lookup_related("E-1", department="通用")["records"], [])

    def test_unassigned_data_requires_explicit_admin_binding(self):
        ds = import_structured_dataset(SimpleUploadedFile("legacy.csv", b"Equipment,Part No\nE-1,P-1\n"), "parts")
        self.assertFalse(GraphRelation.objects.exists())
        self.client.force_login(self.admin)
        response = self.client.post(reverse("kb:graph_action"), {
            "kb": self.kb.slug, "action": "bind", "dataset": ds.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(GraphRelation.objects.count(), 1)
        self.assertContains(self.client.get(reverse("kb:graph"), {"kb": self.kb.slug}), "核对通过")

    def test_seed_failure_rolls_back_import_and_previous_active_version(self):
        old = self.upload()
        with patch("kb.knowledge_graph.seed_dataset", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                self.upload("设备号,部件号\nE-1,P-2\n")
        old.refresh_from_db()
        self.assertTrue(old.active)
        self.assertEqual(StructuredDataset.objects.count(), 1)

    @patch("kb.rerank.rerank_settings", return_value={"enabled": False})
    @patch("kb.keyword_index.search", return_value=[])
    @patch("kb.retriever.get_kb_vectorstore", side_effect=RuntimeError("embedding offline"))
    @patch("kb.keyword_index.get_chunks")
    def test_folder_graph_lane_works_without_embedding(self, chunks, vector, keyword, rerank):
        from kb.retriever import search_folder
        self.upload()
        attach_document(self.drawing_relation(), self.doc, self.doc.md_content, self.admin)
        self.verify_all()
        chunks.return_value = [{"chunk_id": "cid", "text": self.doc.md_content, "section": "安装"}]
        with patch("kb.retriever._embeddings", side_effect=RuntimeError("offline")):
            results = search_folder([self.child.slug], "E-1", graph_scope_slugs=[self.kb.slug])
        self.assertEqual(results[0]["via"], "graph")
        self.assertEqual(results[0]["doc_id"], str(self.doc.pk))

    def test_fusion_keeps_identical_text_from_different_documents(self):
        from kb.retriever import _result_key
        first = {"text": "相同正文", "source": "a.txt", "kb_slug": "one", "chunk_id": "a"}
        second = {**first, "source": "b.txt", "kb_slug": "two", "chunk_id": "b"}
        self.assertNotEqual(_result_key(first), _result_key(second))

    def test_user_supplied_html_is_escaped_in_graph_page(self):
        self.upload("设备号,部件号,图纸号\n<script>alert(1)</script>,P-1,D-1\n")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("kb:graph"), {"kb": self.kb.slug})
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")
