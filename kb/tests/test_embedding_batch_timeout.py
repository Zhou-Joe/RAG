from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from kb.pipeline import _embeddings, WeMMEmbeddings
import httpx


class EmbeddingBatchTimeoutTests(SimpleTestCase):
    @override_settings(EMBEDDING_BATCH_SIZE=16, WEMM_BATCH_SIZE=4, WEMM_REQUEST_TIMEOUT=180)
    def test_openai_wemm_sends_small_batches_with_consistent_timeout(self):
        cfg = {"model": "WeMM-Embedding-9B", "base_url": "http://localhost:8081/v1", "dimensions": 2, "api_key": "local"}
        calls = []
        def handler(request):
            import json
            body = json.loads(request.content)
            calls.append(body["input"])
            return httpx.Response(200, json={"object":"list", "data":[{"object":"embedding", "index":i, "embedding":[1.,2.]} for i in range(len(body["input"]))], "model":cfg["model"], "usage":{"prompt_tokens":1,"total_tokens":1}})
        with patch("kb.pipeline.embedding_settings", return_value=cfg):
            embeddings = _embeddings()
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            from openai import OpenAI
            sdk = OpenAI(api_key="local", base_url=cfg["base_url"], http_client=client)
            embeddings.client = sdk.embeddings
            vectors = embeddings.embed_documents(["片段" + str(i) for i in range(9)])
        self.assertEqual([len(batch) for batch in calls], [4,4,1])
        self.assertEqual(len(vectors), 9)
        self.assertEqual(embeddings.request_timeout, 180)

    @override_settings(WEMM_BATCH_SIZE=3, WEMM_REQUEST_TIMEOUT=150)
    def test_native_wemm_uses_same_batch_configuration(self):
        client = WeMMEmbeddings("http://localhost:8081")
        with patch("kb.pipeline.httpx.post") as post:
            post.side_effect = [type("Response", (), {"raise_for_status":lambda self:None,"json":lambda self,n=n:{"embeddings":[[1.]] * n}})() for n in (3,2)]
            self.assertEqual(len(client.embed_documents(["x"]*5)), 5)
        self.assertEqual([len(c.kwargs["json"]["inputs"]) for c in post.call_args_list], [3,2])
        self.assertTrue(all(c.kwargs["timeout"] == 150 for c in post.call_args_list))
