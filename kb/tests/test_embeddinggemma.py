"""Gemma's local service contract and regression coverage for WeMM."""
import base64
import hashlib
import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import httpx
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from langchain_core.documents import Document as Chunk

from kb.models import ChunkProvenance, Document, KnowledgeBase, SiteConfig
from kb.pipeline import (
    EmbeddingGemmaEmbeddings, WeMMEmbeddings, _embed_input_version,
    _embedding_fingerprint, _embeddings, _index_image_chunks, _index_page_chunks,
    openai_gemma_vision_class, run_indexing, supports_image_embedding,
)


CONFIG = {
    "model": "google/embeddinggemma-2", "base_url": "http://localhost:8301",
    "dimensions": 256, "api_key": "",
}
# 部署形态：服务只提供 OpenAI 兼容 /v1/embeddings（无 WeMM 的 /embed），
# 图片走同端点的 image_base64 条目，图文同条=融合向量。
SERVICE = {
    "model": "embeddinggemma-2", "base_url": "http://10.136.185.9:8001/v1",
    "dimensions": 768, "api_key": "",
}
IMAGE = "data:image/jpeg;base64," + base64.b64encode(b"image").decode()


def response(vectors):
    return httpx.Response(200, json={"embeddings": vectors},
                          request=httpx.Request("POST", CONFIG["base_url"] + "/embed"))


class GemmaClientTests(SimpleTestCase):
    @override_settings(WEMM_BATCH_SIZE=2, WEMM_REQUEST_TIMEOUT=150)
    def test_native_text_requests_use_distinct_prompts_and_bounded_batches(self):
        with patch("kb.pipeline.embedding_settings", return_value=CONFIG):
            client = _embeddings()
        self.assertIsInstance(client, EmbeddingGemmaEmbeddings)
        with patch.dict(os.environ, {"WEMM_QUERY_INSTRUCT": "1"}), patch(
            "kb.pipeline.httpx.post", side_effect=[response([[1.]]), response([[1.], [2.]]), response([[3.]])],
        ) as post:
            client.embed_query("bearing temperature")
            client.embed_documents(["first passage", "second passage", "third passage"])
        self.assertEqual([c.args[0] for c in post.call_args_list], [CONFIG["base_url"] + "/embed"] * 3)
        self.assertEqual([c.kwargs["json"] for c in post.call_args_list], [
            {"inputs": [{"text": "bearing temperature", "prompt_name": "SearchQuery"}], "dimension": 256},
            {"inputs": [{"text": "first passage", "prompt_name": "Document"},
                        {"text": "second passage", "prompt_name": "Document"}], "dimension": 256},
            {"inputs": [{"text": "third passage", "prompt_name": "Document"}], "dimension": 256},
        ])
        self.assertTrue(all(c.kwargs["timeout"] == 150 for c in post.call_args_list))

    def test_gemma_interleaving_preserves_caption_and_image_order(self):
        parts = [{"type": "text", "text": "diagram"}, {"type": "image", "image": IMAGE},
                 {"type": "text", "text": "detail"}, {"type": "image", "image": IMAGE + "2"}]
        with patch("kb.pipeline.httpx.post", return_value=response([[1.]])) as post:
            EmbeddingGemmaEmbeddings(CONFIG["base_url"], 256).embed_contents([parts])
        self.assertEqual(post.call_args.kwargs["json"], {
            "inputs": [{"content": {"text": "title: none | text: diagram\n<|image|>\ndetail\n<|image|>",
                                     "image": [IMAGE, IMAGE + "2"]}}], "dimension": 256,
        })

    def test_wemm_keeps_plain_text_and_list_content(self):
        cfg = {**CONFIG, "model": "Tencent/WeMM-Embedding-4B", "dimensions": 2560}
        with patch("kb.pipeline.embedding_settings", return_value=cfg):
            client = _embeddings()
        self.assertIs(type(client), WeMMEmbeddings)
        parts = [{"type": "text", "text": "diagram"}, {"type": "image", "image": IMAGE}]
        with patch.dict(os.environ, {"WEMM_QUERY_INSTRUCT": "0"}), patch(
            "kb.pipeline.httpx.post", return_value=response([[1.]]),
        ) as post:
            client.embed_documents(["passage"])
            client.embed_query("question")
            client.embed_contents([parts])
        self.assertEqual([c.kwargs["json"] for c in post.call_args_list], [
            {"inputs": [{"text": "passage"}], "dimension": 2560},
            {"inputs": [{"text": "question"}], "dimension": 2560},
            {"inputs": [{"content": parts}], "dimension": 2560},
        ])

    def test_openai_endpoints_remain_on_openai_protocol(self):
        from langchain_openai import OpenAIEmbeddings
        for model in ("Tencent/WeMM-Embedding-4B", "google/embeddinggemma-2", "other"):
            with self.subTest(model=model), patch("kb.pipeline.embedding_settings", return_value={
                **CONFIG, "model": model, "base_url": "http://localhost:8081/v1/",
            }):
                client = _embeddings()
            self.assertIsInstance(client, OpenAIEmbeddings)
            self.assertEqual(client.model, model)
            self.assertEqual(client.dimensions, 256)

    def test_invalid_gemma_dimension_fails_before_a_request(self):
        with patch("kb.pipeline.httpx.post") as post, self.assertRaisesMessage(ValueError, "128/256/512/768"):
            EmbeddingGemmaEmbeddings(CONFIG["base_url"], 1024)
        post.assert_not_called()

    def test_mismatched_response_count_is_rejected(self):
        with patch("kb.pipeline.httpx.post", return_value=response([[1.]])), self.assertRaisesMessage(ValueError, "向量数不匹配"):
            EmbeddingGemmaEmbeddings(CONFIG["base_url"], 256).embed_documents(["one", "two"])

    def test_only_native_gemma_changes_index_identity(self):
        for model, url, changes in ((CONFIG["model"], CONFIG["base_url"], True),
                                    (CONFIG["model"], "http://localhost/v1", False),
                                    ("WeMM-Embedding-4B", "http://localhost:8300", False)):
            cfg = {**CONFIG, "model": model, "base_url": url}
            legacy = hashlib.sha256(repr((model, 256, url)).encode()).hexdigest()
            with self.subTest(model=model, url=url), patch("kb.pipeline.embedding_settings", return_value=cfg):
                self.assertEqual(_embedding_fingerprint(cfg) != legacy, changes)
                expected = "embeddinggemma-text-document-v1" if changes else "wemm-text-user-v1"
                self.assertEqual(_embed_input_version("text"), expected)


class GemmaPipelineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.kb = KnowledgeBase.objects.create(name="Gemma manual", slug="gemma-manual")
        cls.doc = Document.objects.create(kb=cls.kb, original_name="manual.pdf", file="manual.pdf")

    def test_illustration_and_whole_page_are_indexed_with_their_text(self):
        calls = []
        def service(url, *, json, **kwargs):
            self.assertEqual(url, CONFIG["base_url"] + "/embed")
            for item in json["inputs"]:
                content = item["content"]  # image-only fallback is not acceptable here
                self.assertIsInstance(content, dict)
                self.assertIn("<|image|>", content["text"])
                self.assertTrue(content["image"][0].startswith("data:image/jpeg;base64,"))
            calls.append(json)
            return response([[1., 2.]] * len(json["inputs"]))

        vs = MagicMock()
        chunk = Chunk(page_content="bearing diagram", metadata={"source": "manual.pdf", "image": "a.jpg"})
        with tempfile.TemporaryDirectory() as root, patch("kb.pipeline.embedding_settings", return_value=CONFIG), patch(
            "kb.pipeline.httpx.post", side_effect=service,
        ), patch("kb.pipeline._render_page_jpeg", return_value=b"jpeg"):
            image = Path(root) / "a.jpg"
            image.write_bytes(b"jpeg")
            self.assertEqual(_index_image_chunks(vs, [(chunk, image)], self.kb.slug, "manual.pdf", doc_id=self.doc.pk), 1)
            self.assertEqual(_index_page_chunks(vs, self.doc, self.kb.slug, [
                {"type": "text", "text": "bearing page", "page_idx": 0},
            ]), 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("bearing diagram", calls[0]["inputs"][0]["content"]["text"])
        self.assertIn("bearing page", calls[1]["inputs"][0]["content"]["text"])
        vs._collection.add.assert_called_once()
        vs._collection.upsert.assert_called_once()
        self.assertTrue(ChunkProvenance.objects.filter(document=self.doc, chunk_id=f"page-{self.doc.pk}-0").exists())

    def test_old_unprompted_gemma_index_requires_rebuild(self):
        vs = MagicMock()
        vs._collection.metadata = {"embedding_fingerprint": hashlib.sha256(
            repr((CONFIG["model"], CONFIG["dimensions"], CONFIG["base_url"])).encode()).hexdigest()}
        with tempfile.TemporaryDirectory() as root, override_settings(CHROMA_ROOT=Path(root)), patch(
            "kb.pipeline.embedding_settings", return_value=CONFIG,
        ), patch("kb.pipeline.Chroma", return_value=vs), self.assertRaisesMessage(ValueError, "请重建该库"):
            run_indexing("inspect the bearing", self.kb.slug, "manual.pdf")
        vs.add_documents.assert_not_called()


class GemmaServiceProtocolTests(SimpleTestCase):
    """部署中的 EmbeddingGemma 服务：OpenAI 协议上额外的图片嵌入能力。"""

    def service_client(self, **overrides):
        cls = openai_gemma_vision_class()
        client = cls(model=SERVICE["model"], dimensions=SERVICE["dimensions"],
                     api_key="local-no-key", base_url=SERVICE["base_url"],
                     check_embedding_ctx_length=False, chunk_size=2,
                     request_timeout=180, max_retries=0)
        client.client = MagicMock()
        return client

    def reply(self, vectors):
        return MagicMock(data=[MagicMock(embedding=v) for v in vectors])

    def test_service_endpoint_client_keeps_openai_protocol_and_gains_images(self):
        with patch("kb.pipeline.embedding_settings", return_value=SERVICE):
            client = _embeddings()
        from langchain_openai import OpenAIEmbeddings
        self.assertIsInstance(client, OpenAIEmbeddings)   # 文本协议不变
        self.assertTrue(supports_image_embedding(client))  # 图片块不再被跳过
        self.assertEqual(client.chunk_size, 4)             # 图片 base64 大，批次压到 WEMM_BATCH_SIZE

    def test_wemm_openai_endpoint_stays_text_only(self):
        """vLLM 类 WeMM /v1 端点不认 image_base64 条目，不能误判为可嵌图。"""
        with patch("kb.pipeline.embedding_settings",
                   return_value={**SERVICE, "model": "Tencent/WeMM-Embedding-2B"}):
            self.assertFalse(supports_image_embedding(_embeddings()))

    def test_embed_contents_sends_fused_text_and_bare_image(self):
        client = self.service_client()
        client.client.create.return_value = self.reply([[1.0], [2.0]])
        vecs = client.embed_contents([
            [{"type": "text", "text": "diagram"}, {"type": "image", "image": IMAGE}],
            [{"type": "text", "text": "detail"}, {"type": "image", "image": IMAGE}],
        ])
        self.assertEqual(vecs, [[1.0], [2.0]])
        self.assertEqual(client.client.create.call_args.kwargs["input"], [
            {"text": "diagram", "image_base64": base64.b64encode(b"image").decode()},
            {"text": "detail", "image_base64": base64.b64encode(b"image").decode()},
        ])                                  # data URL 前缀必须剥掉
        self.assertEqual(client.client.create.call_args.kwargs["dimensions"], 768)

    def test_images_are_batched_and_count_mismatch_rejected(self):
        client = self.service_client()
        client.client.create.side_effect = [self.reply([[1.0], [2.0]]), self.reply([[3.0]])]
        parts = [{"type": "text", "text": "图"}, {"type": "image", "image": IMAGE}]
        self.assertEqual(len(client.embed_contents([parts] * 3)), 3)
        self.assertEqual(client.client.create.call_count, 2)   # chunk_size=2

    def test_short_response_is_rejected_instead_of_misaligned(self):
        client = self.service_client()
        client.client.create.return_value = self.reply([[1.0]])   # 2 条目只返 1 向量
        parts = [{"type": "text", "text": "图"}, {"type": "image", "image": IMAGE}]
        with self.assertRaisesMessage(ValueError, "向量数不匹配"):
            client.embed_contents([parts, parts])

    def test_multiple_images_and_unknown_types_are_refused(self):
        client = self.service_client()
        one = {"type": "image", "image": IMAGE}
        with self.assertRaisesMessage(ValueError, "只接受一张图片"):
            client.embed_contents([[one, dict(one)]])
        with self.assertRaisesMessage(ValueError, "不支持内容类型"):
            client.embed_contents([[{"type": "audio", "audio": "x"}]])

    def test_raw_image_fallback_matches_service_field_names(self):
        """_index_image_chunks 的降级路径用 WeMM 的 image_b64 写法。"""
        client = self.service_client()
        client.client.create.return_value = self.reply([[1.0]])
        self.assertEqual(client._embed_raw([{"image_b64": "QUJD"}]), [[1.0]])
        self.assertEqual(client.client.create.call_args.kwargs["input"],
                         [{"image_base64": "QUJD"}])


class GemmaServiceImageIndexTests(TestCase):
    """图片块索引门控：OpenAI 协议的 Gemma 服务不得被当作「非多模态」跳过。"""

    @classmethod
    def setUpTestData(cls):
        cls.kb = KnowledgeBase.objects.create(name="Gemma service KB", slug="gemma-service")
        cls.doc = Document.objects.create(kb=cls.kb, original_name="manual.pdf", file="manual.pdf")

    def test_illustration_chunk_is_indexed_over_openai_protocol(self):
        client = MagicMock(spec=["embed_contents", "chunk_size", "model", "dimensions"])
        client.embed_contents.return_value = [[1.0, 2.0]]
        vs = MagicMock()
        chunk = Chunk(page_content="bearing diagram", metadata={
            "source": "manual.pdf", "image": "a.jpg", "type": "image"})
        with tempfile.TemporaryDirectory() as root, patch(
            "kb.pipeline._embeddings", return_value=client,
        ):
            image = Path(root) / "a.jpg"
            image.write_bytes(b"jpeg")
            self.assertEqual(_index_image_chunks(
                vs, [(chunk, image)], self.kb.slug, "manual.pdf", doc_id=self.doc.pk), 1)
        sent = client.embed_contents.call_args.args[0][0]
        self.assertEqual([p["type"] for p in sent], ["text", "image"])
        vs._collection.add.assert_called_once()

    def test_text_only_client_still_skips_images_without_failing(self):
        client = MagicMock(spec=["chunk_size", "model", "dimensions"])  # 无 embed_contents
        chunk = Chunk(page_content="x", metadata={"source": "s", "image": "a.jpg", "type": "image"})
        vs = MagicMock()
        with tempfile.TemporaryDirectory() as root, patch("kb.pipeline._embeddings", return_value=client):
            image = Path(root) / "a.jpg"
            image.write_bytes(b"jpeg")
            self.assertEqual(_index_image_chunks(
                vs, [(chunk, image)], self.kb.slug, "manual.pdf", doc_id=self.doc.pk), 0)
        vs._collection.add.assert_not_called()


class NativeConnectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="gemma-admin", is_staff=True)
        self.client.force_login(self.user)

    def test_connection_test_uses_saved_native_client_and_dimension(self):
        for model, dimension, item in (
            ("google/embeddinggemma-2", 256, {"text": "连接测试", "prompt_name": "SearchQuery"}),
            ("WeMM-Embedding-4B", 2560, {"text": "连接测试"}),
        ):
            with self.subTest(model=model):
                cfg = SiteConfig.get()
                cfg.embedding_base_url = CONFIG["base_url"]
                cfg.embedding_model = model
                cfg.embedding_dimensions = dimension
                cfg.save()
                with patch.dict(os.environ, {"WEMM_QUERY_INSTRUCT": "0"}), patch(
                    "kb.pipeline.httpx.post", return_value=response([[1.] * dimension]),
                ) as post:
                    result = self.client.post(reverse("kb:settings_test"), {"target": "embedding"}).json()["results"]["embedding"]
                self.assertTrue(result["ok"])
                self.assertEqual(post.call_args.kwargs["json"], {"inputs": [item], "dimension": dimension})

    def test_connection_test_reports_invalid_saved_gemma_dimension(self):
        cfg = SiteConfig.get()
        cfg.embedding_base_url = CONFIG["base_url"]
        cfg.embedding_model = CONFIG["model"]
        cfg.embedding_dimensions = 1024
        cfg.save()
        with patch("kb.pipeline.httpx.post") as post:
            result = self.client.post(reverse("kb:settings_test"), {"target": "embedding"}).json()["results"]["embedding"]
        self.assertFalse(result["ok"])
        self.assertIn("128/256/512/768", result["detail"])
        post.assert_not_called()
