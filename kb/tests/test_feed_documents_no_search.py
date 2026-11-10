"""Cron-generated source documents stay outside semantic and keyword retrieval."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from kb.models import Document, FeedRecord, KnowledgeBase, KnowledgeFeed
from kb import retriever
from kb.agent import _image_search_core


class GeneratedFeedDocumentRetrievalTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("no-embed-feed-owner")
        self.folder = KnowledgeBase.objects.create(
            name="Collected news", slug="no-embed-folder", is_folder=True,
            category="business", created_by=user,
        )
        self.generated = KnowledgeBase.objects.create(
            name="News item", slug="feed-generated-news", parent=self.folder,
            category="business", created_by=user,
        )
        self.manual = KnowledgeBase.objects.create(
            name="Manual upload", slug="manual-news", parent=self.folder,
            category="business", created_by=user,
        )
        feed = KnowledgeFeed.objects.create(
            kb=self.folder, name=self.folder.name, created_by=user,
            adapter_spec={"ingest_mode": "documents"},
        )
        doc = Document.objects.create(
            kb=self.generated, original_name="news.md", file_type="md",
            status=Document.Status.COMPLETED, md_content="A news story",
        )
        FeedRecord.objects.create(feed=feed, kb=self.generated,
                                  external_id="news-1", document=doc)

    def test_generated_only_folder_does_not_embed_query_or_search(self):
        with patch("kb.retriever._embeddings") as embeddings, \
             patch("kb.retriever.search") as search:
            result = retriever.search_folder([self.generated.slug], "news", k=3)
        self.assertEqual(result, [])
        embeddings.assert_not_called()
        search.assert_not_called()

    def test_direct_generated_search_skips_vector_and_keyword_indexes(self):
        with patch("kb.retriever.get_kb_vectorstore") as vectorstore, \
             patch("kb.keyword_index.search") as keyword_search:
            result = retriever.search(self.generated.slug, "news", k=3)
        self.assertEqual(result, [])
        vectorstore.assert_not_called()
        keyword_search.assert_not_called()

    def test_mixed_folder_searches_only_manual_document_library(self):
        with patch("kb.retriever._embeddings") as embeddings, \
             patch("kb.retriever.search", return_value=[]) as search, \
             patch("kb.retriever._merge_graph"), \
             patch("kb.rerank.rerank_settings", return_value={"enabled": False}):
            embeddings.return_value.embed_query.return_value = [0.5]
            result = retriever.search_folder(
                [self.generated.slug, self.manual.slug], "news", k=3,
            )
        self.assertEqual(result, [])
        embeddings.return_value.embed_query.assert_called_once_with("news")
        self.assertEqual([call.args[0] for call in search.call_args_list],
                         [self.manual.slug])

    def test_image_lane_also_skips_generated_library(self):
        with patch("kb.retriever.get_kb_vectorstore") as vectorstore:
            result = retriever._image_lane([self.generated.slug], [0.5])
        self.assertEqual(result, [])
        vectorstore.assert_not_called()

    def test_image_search_skips_embedding_for_generated_only_library(self):
        with patch("kb.pipeline._embeddings") as embeddings, \
             patch("kb.retriever._image_lane") as image_lane:
            result = _image_search_core(
                "news image", self.generated.slug, [self.generated.slug],
                {}, [], 3,
            )
        self.assertIn("未召回图片候选", result)
        embeddings.assert_not_called()
        image_lane.assert_not_called()

    def test_explicit_fetch_reads_current_raw_document_without_index(self):
        record = FeedRecord.objects.select_related("document").get(kb=self.generated)
        record.document.md_content = "# Daily news\n\nIntro\n\n## Analysis\n\nUseful result"
        record.document.save(update_fields=["md_content"])
        with patch("kb.retriever.get_kb_vectorstore") as vectorstore:
            rows = retriever.fetch_doc(
                self.generated.slug, source="news.md", section="Analysis",
                contains="Useful", limit=40,
            )
        self.assertEqual(len(rows), 1)
        self.assertIn("Useful result", rows[0]["text"])
        self.assertEqual(rows[0]["doc_id"], str(record.document_id))
        vectorstore.assert_not_called()

    def test_explicit_fetch_rejects_deleted_or_superseded_version(self):
        record = FeedRecord.objects.get(kb=self.generated)
        record.deleted = True
        record.save(update_fields=["deleted"])
        with patch("kb.retriever.get_kb_vectorstore") as vectorstore:
            self.assertEqual(retriever.fetch_doc(self.generated.slug), [])
        vectorstore.assert_not_called()

    def test_manual_upload_in_generated_child_stays_searchable(self):
        Document.objects.create(
            kb=self.generated, original_name="manual-inside.md", file_type="md",
            status=Document.Status.COMPLETED, md_content="Manual answer", chunk_count=1,
        )
        with patch("kb.retriever.get_kb_vectorstore") as store, \
             patch("kb.keyword_index.search") as keyword_search, \
             patch("kb.retriever._merge_graph"), \
             patch("kb.rerank.rerank_settings", return_value={"enabled": False}):
            store.return_value.embeddings.embed_query.return_value = [0.5]
            store.return_value._collection.query.return_value = {
                "documents": [["Generated answer", "Manual answer"]],
                "metadatas": [[{"source": "news.md"}, {"source": "manual-inside.md"}]],
                "ids": [["generated-id", "manual-id"]],
            }
            keyword_search.return_value = [
                {"source": "news.md", "text": "Generated answer", "section": ""},
                {"source": "manual-inside.md", "text": "Manual answer", "section": ""},
            ]
            rows = retriever.search(self.generated.slug, "answer", k=3)
        self.assertEqual([row["source"] for row in rows], ["manual-inside.md", "manual-inside.md"])
        self.assertEqual(keyword_search.call_args.kwargs["sources"], ["manual-inside.md"])

    def test_explicit_fetch_routes_manual_source_to_index(self):
        Document.objects.create(
            kb=self.generated, original_name="manual-inside.md", file_type="md",
            status=Document.Status.COMPLETED, md_content="Manual answer", chunk_count=1,
        )
        with patch("kb.retriever.get_kb_vectorstore") as store:
            store.return_value._collection.get.return_value = {
                "documents": ["Manual answer", "Old generated answer"],
                "metadatas": [{"source": "manual-inside.md"}, {"source": "news.md"}],
                "ids": ["manual-id", "generated-id"],
            }
            manual = retriever.fetch_doc(self.generated.slug, source="manual-inside.md")
            both = retriever.fetch_doc(self.generated.slug)
        self.assertEqual([row["source"] for row in manual], ["manual-inside.md"])
        self.assertEqual([row["source"] for row in both], ["news.md", "manual-inside.md"])
