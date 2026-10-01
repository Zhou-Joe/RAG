from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from kb.models import Document, KnowledgeBase
from kb.ingestion import run_next, recover_interrupted


class IngestionQueueTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('queue-admin',is_staff=True)
        self.kb = KnowledgeBase.objects.create(name='queue',slug='queue',created_by=self.user)
        self.doc = Document.objects.create(kb=self.kb, original_name='x.txt',file='documents/x.txt', file_type='txt')
    def test_queue_claims_oldest_and_only_once(self):
        Document.objects.create(kb=self.kb,original_name='y.txt')
        with patch('kb.pipeline.process_document') as process:
            self.assertTrue(run_next())
            self.assertEqual(process.call_args.args,(str(self.doc.id),))
            self.assertTrue(run_next())
            self.assertFalse(run_next())
            self.assertEqual(process.call_count,2)
    def test_restart_requeues_only_interrupted_documents(self):
        self.doc.status = 'indexing'
        self.doc.md_content = 'saved text'
        self.doc.save()
        failed = Document.objects.create(kb=self.kb,original_name='failed.txt',status='failed')
        self.assertEqual(recover_interrupted(),1)
        self.doc.refresh_from_db(); failed.refresh_from_db()
        self.assertEqual((self.doc.status,self.doc.md_content),('pending','saved text'))
        self.assertEqual(failed.status,'failed')
    def test_retry_preserves_ocr_and_rejects_duplicate_submission(self):
        self.doc.status='failed'; self.doc.md_content='saved text'; self.doc.save()
        self.client.force_login(self.user)
        url=reverse('kb:doc_retry',args=[self.kb.slug,self.doc.id])
        with patch('kb.pipeline.process_document_async') as enqueue:
            self.assertEqual(self.client.post(url).status_code,302)
            self.assertEqual(self.client.post(url).status_code,409)
            enqueue.assert_called_once_with(self.doc.id)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.md_content,'saved text')
        self.assertEqual(self.doc.status,'pending')
    def test_saved_ocr_resume_does_not_repeat_parser(self):
        self.doc.md_content='saved text'; self.doc.save()
        with patch('kb.pipeline.run_ocr_with_images') as ocr, patch('kb.pipeline.run_indexing',return_value=9), patch('kb.tracker.run_extraction_async'):
            from kb.pipeline import process_document
            process_document(str(self.doc.id))
            ocr.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual((self.doc.status,self.doc.chunk_count),('completed',9))

    def test_retry_cannot_expand_department_permissions(self):
        from kb.models import DEPARTMENT_GENERAL
        outsider=get_user_model().objects.create_user('queue-outsider')
        self.doc.status='failed'; self.doc.save()
        self.kb.department='Restricted'; self.kb.save()
        self.client.force_login(outsider)
        with patch('kb.pipeline.process_document_async') as enqueue:
            result=self.client.post(reverse('kb:doc_retry',args=[self.kb.slug,self.doc.id]))
        self.assertIn(result.status_code,[302,403,404])
        enqueue.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status,'failed')

    def test_retry_requires_csrf(self):
        from django.test import Client
        self.doc.status='failed'; self.doc.save()
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        with patch('kb.pipeline.process_document_async') as enqueue:
            response=client.post(reverse('kb:doc_retry',args=[self.kb.slug,self.doc.id]))
        self.assertEqual(response.status_code,403)
        enqueue.assert_not_called()

    def test_progress_reports_saved_batch_count(self):
        self.doc.status='indexing'; self.doc.stage_detail='正在向量化 4/8 个片段；已完成片段可续传'; self.doc.save()
        self.client.force_login(self.user)
        response=self.client.get(reverse('kb:doc_status',args=[self.kb.slug]))
        self.assertEqual(response.json()['docs'][0]['progress'],62)

    def test_unexpected_job_failure_does_not_stall_queue(self):
        with patch('kb.pipeline.process_document',side_effect=RuntimeError('crash')):
            self.assertTrue(run_next())
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status,'failed')
        self.assertFalse(run_next())


from django.test import TransactionTestCase, override_settings
import tempfile
import threading
from pathlib import Path


class HostQueueConcurrencyTests(TransactionTestCase):
    def test_two_workers_share_one_host_lock_and_process_serially(self):
        from kb.ingestion import _loop, _wake
        kb=KnowledgeBase.objects.create(name='host',slug='host')
        first=Document.objects.create(kb=kb,original_name='first.txt')
        second=Document.objects.create(kb=kb,original_name='second.txt')
        entered=threading.Event(); release=threading.Event(); finished=threading.Event(); stop=threading.Event()
        calls=[]; active=0; peak=0; guard=threading.Lock()
        def process(doc_id):
            nonlocal active,peak
            with guard:
                active+=1; peak=max(peak,active); calls.append(doc_id)
            if doc_id==str(first.id):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test release timed out')
            Document.objects.filter(id=doc_id).update(status='completed')
            with guard: active-=1
            if doc_id==str(second.id): finished.set()
        with tempfile.TemporaryDirectory() as root, override_settings(DATA_DIR=Path(root)), patch('kb.pipeline.process_document',side_effect=process):
            workers=[threading.Thread(target=_loop,args=(stop,),daemon=True) for _ in range(2)]
            try:
                for worker in workers: worker.start()
                self.assertTrue(entered.wait(5))
                self.assertEqual(calls,[str(first.id)])
                release.set()
                self.assertTrue(finished.wait(5))
            finally:
                release.set(); stop.set(); _wake.set()
                for worker in workers: worker.join(5)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(calls,[str(first.id),str(second.id)])
        self.assertEqual(peak,1)
