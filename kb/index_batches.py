"""Bounded, idempotent text embedding writes with vector checkpoints."""
import hashlib
import json
import httpx
from openai import APITimeoutError, APIStatusError


def transient(exc):
    if isinstance(exc, (httpx.TimeoutException, APITimeoutError)):
        return True
    if isinstance(exc, (httpx.HTTPStatusError, APIStatusError)):
        status = getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', 0)
        return status == 429 or status >= 500
    return False


def write_batches(vs, chunks, *, identity, fingerprint, batch_size, progress=None):
    ids = ["ingest-" + hashlib.sha256(json.dumps(
        [str(identity), fingerprint, i, c.page_content, c.metadata],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest() for i, c in enumerate(chunks)]
    done = 0
    def write(items, keys, can_split=True):
        nonlocal done
        try:
            vs.add_documents(items, ids=keys)  # Chroma upsert makes uncertain writes idempotent.
        except Exception as exc:
            if can_split and len(items) > 1 and transient(exc):
                mid = len(items) // 2
                write(items[:mid], keys[:mid], False)
                write(items[mid:], keys[mid:], False)
                return
            if transient(exc):
                raise RuntimeError("向量服务超时或暂时不可用；已保留成功片段，可点击重试继续。") from exc
            raise
        done += len(items)
        if progress:
            progress(done, len(chunks))
    for start in range(0, len(chunks), batch_size):
        keys = ids[start:start + batch_size]
        existing = set(vs.get(ids=keys, include=[])['ids'])
        done += len(existing)
        pairs = [(c, key) for c, key in zip(chunks[start:start+batch_size], keys) if key not in existing]
        if pairs:
            write([c for c, _ in pairs], [key for _, key in pairs])
        elif progress:
            progress(done, len(chunks))
    return ids
