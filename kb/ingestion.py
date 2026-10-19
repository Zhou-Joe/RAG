"""One durable document queue per deployment host (Mac/Linux).

Document.status is the queue; Chroma IDs are the embedding checkpoints.
A host file lock prevents two web workers from processing it concurrently.
"""
import fcntl
import logging
import threading
from pathlib import Path
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

_guard = threading.Lock()
_thread = None
_wake = threading.Event()
logger = logging.getLogger(__name__)


def recover_interrupted():
    from .models import Document
    return Document.objects.filter(status__in=[Document.Status.OCR, Document.Status.INDEXING]).update(
        status=Document.Status.PENDING, stage_detail="服务重启，等待从保存进度继续", updated_at=timezone.now())


def run_next():
    from .models import Document
    from .pipeline import process_document
    doc = Document.objects.filter(status=Document.Status.PENDING).order_by('created_at').first()
    if doc is None:
        return False
    claimed = Document.objects.filter(id=doc.id, status=Document.Status.PENDING).update(
        status=Document.Status.OCR, stage_detail="开始处理", updated_at=timezone.now())
    if claimed:
        try:
            process_document(str(doc.id))
        except Exception:
            logger.exception("文档任务异常 doc=%s", doc.id)
            Document.objects.filter(id=doc.id).update(status=Document.Status.FAILED,
                error_msg="文档处理异常，可点击重试继续", stage_detail="处理失败", updated_at=timezone.now())
    return True


def _loop(stop=None):
    # Hold this lock until process exit; a replacement worker can then resume.
    root = Path(settings.DATA_DIR)
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'ingestion.lock').open('a+') as lock:
        while stop is None or not stop.is_set():
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                _wake.wait(3)
                _wake.clear()
        else:
            return
        close_old_connections()
        recover_interrupted()
        while stop is None or not stop.is_set():
            try:
                close_old_connections()
                if run_next():
                    continue
            except Exception:
                logger.exception("文档队列异常，稍后重试")
            finally:
                close_old_connections()
            _wake.wait(3)
            _wake.clear()


def start():
    global _thread
    with _guard:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, daemon=True, name='document-ingestion')
            _thread.start()
    _wake.set()


def enqueue(doc_id):
    # Uploaded/retried documents already have persistent pending status.
    start()
