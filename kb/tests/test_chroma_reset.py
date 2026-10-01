import tempfile
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings


class ChromaResetTests(SimpleTestCase):
    def test_reset_preserves_database_and_allows_new_dimension_with_open_client(self):
        from pathlib import Path
        import chromadb
        from kb.retriever import reset_kb_collection, _VS_CACHE
        from kb.pipeline import _chroma_collection_name
        with tempfile.TemporaryDirectory() as root, override_settings(CHROMA_ROOT=Path(root)):
            slug = "reset-regression"
            client = chromadb.PersistentClient(path=str(Path(root) / slug))
            name = _chroma_collection_name(slug)
            collection = client.get_or_create_collection(name)
            collection.add(ids=["old"], embeddings=[[1., 2.]])
            other = client.get_or_create_collection("keep-collection")
            other.add(ids=["keep"], embeddings=[[1., 2.]])
            db = Path(root) / slug / "chroma.sqlite3"
            inode = db.stat().st_ino
            reset_kb_collection(slug)
            reset_kb_collection(slug)  # missing collection is safe
            fresh = client.get_or_create_collection(name)
            fresh.add(ids=["new"], embeddings=[[1., 2., 3.]])
            self.assertEqual(fresh.count(), 1)
            self.assertEqual(other.count(), 1)
            self.assertEqual(db.stat().st_ino, inode)
            self.assertNotIn(slug, _VS_CACHE)
