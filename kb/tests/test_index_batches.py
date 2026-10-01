from unittest.mock import patch
import httpx
from django.test import SimpleTestCase
from langchain_core.documents import Document
from kb.index_batches import write_batches


class Store:
    def __init__(self, fail_call=None):
        self.saved = {}
        self.calls = []
        self.fail_call = fail_call
    def get(self, ids, include):
        return {'ids': [i for i in ids if i in self.saved]}
    def add_documents(self, docs, ids):
        self.calls.append(list(ids))
        if len(self.calls) == self.fail_call:
            raise httpx.ReadTimeout('slow')
        self.saved.update(zip(ids, docs))
        return ids


class IndexBatchTests(SimpleTestCase):
    def chunks(self):
        return [Document(page_content=f'片段 {i}', metadata={'source':'manual'}) for i in range(9)]
    def write(self, store, **kwargs):
        return write_batches(store, self.chunks(), identity='doc1', fingerprint='model1', batch_size=4, **kwargs)
    def test_bounded_batches_and_resume_are_idempotent(self):
        store = Store()
        ids = self.write(store)
        self.assertEqual([len(c) for c in store.calls], [4,4,1])
        self.assertEqual(len(store.saved), 9)
        self.assertEqual(self.write(store), ids)
        self.assertEqual(len(store.calls), 3)
    def test_timeout_splits_only_failing_batch(self):
        store = Store(fail_call=2)
        self.write(store)
        self.assertEqual([len(c) for c in store.calls], [4,4,2,2,1])
        self.assertEqual(len(store.saved),9)
    def test_failure_preserves_checkpoint_and_retry_skips_it(self):
        store = Store()
        original = store.add_documents
        def fail_later(docs, ids):
            if len(store.saved) >= 4:
                raise httpx.ReadTimeout('slow')
            return original(docs, ids)
        with patch.object(store, 'add_documents', side_effect=fail_later):
            with self.assertRaisesMessage(RuntimeError,'已保留成功片段'):
                self.write(store)
        self.assertEqual(len(store.saved),4)
        self.write(store)
        self.assertEqual([len(c) for c in store.calls], [4,4,1])
        self.assertEqual(len(store.saved),9)
    def test_non_transient_failure_is_not_retried(self):
        store = Store()
        with patch.object(store,'add_documents',side_effect=ValueError('dimension mismatch')) as add:
            with self.assertRaises(ValueError):
                self.write(store)
            self.assertEqual(add.call_count,1)


from django.test import TestCase, override_settings
from langchain_core.embeddings import Embeddings
import tempfile
from pathlib import Path
from kb.models import KnowledgeBase


class PipelineCheckpointTests(TestCase):
    def test_real_chroma_resume_and_model_boundary(self):
        from kb.pipeline import run_indexing
        from kb.retriever import reset_kb_collection
        class FakeEmbeddings(Embeddings):
            chunk_size=4
            def __init__(self): self.calls=[]; self.fail=True
            def embed_documents(self,texts):
                self.calls.append(len(texts))
                if self.fail and len(self.calls) in [2,3]:
                    raise httpx.ReadTimeout('slow')
                return [[float(i),1.] for i in range(len(texts))]
            def embed_query(self,text): return [1.,1.]
        ef=FakeEmbeddings()
        KnowledgeBase.objects.create(name='checkpoint',slug='checkpoint')
        chunks=[Document(page_content=f'chunk {i}',metadata={'source':'test.txt'}) for i in range(9)]
        cfg={'model':'model1','base_url':'http://localhost/v1','dimensions':2,'api_key':''}
        with tempfile.TemporaryDirectory() as root, override_settings(CHROMA_ROOT=Path(root)), patch('kb.pipeline._embeddings',return_value=ef), patch('kb.pipeline.embedding_settings',return_value=cfg), patch('kb.pipeline._section_aware_chunk',return_value=chunks), patch('kb.keyword_index.add_chunks') as fts:
            with self.assertRaisesMessage(RuntimeError,'已保留成功片段'):
                run_indexing('source','checkpoint','test.txt')
            fts.assert_not_called()
            ef.fail=False
            self.assertEqual(run_indexing('source','checkpoint','test.txt'),9)
            self.assertEqual(ef.calls,[4,4,2,4,1])
            before=len(ef.calls)
            # Failure after vectors are complete is resumable too.
            fts.side_effect=OSError('keyword index unavailable')
            with self.assertRaises(OSError):
                run_indexing('source','checkpoint','test.txt')
            fts.side_effect=None
            self.assertEqual(run_indexing('source','checkpoint','test.txt'),9)
            self.assertEqual(len(ef.calls),before)
            cfg['model']='model2'
            with self.assertRaisesMessage(ValueError,'不能继续混用'):
                run_indexing('source','checkpoint','test.txt')
            reset_kb_collection('checkpoint')
