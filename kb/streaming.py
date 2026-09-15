"""Bound a whole answer turn and keep the browser informed while models work."""
import asyncio
import contextlib
import logging
import time

logger = logging.getLogger(__name__)


async def bounded_events(source, *, timeout=240, heartbeat=10):
    started = time.monotonic()
    iterator = source.__aiter__()
    pending = None
    stage = '理解问题'
    try:
        yield 'heartbeat', {'elapsed': 0, 'stage': stage}
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                logger.warning('Answer deadline exceeded after %.1fs stage=%s', time.monotonic()-started, stage)
                yield 'error', {'message': f'本次问答超过 {timeout:g} 秒，已停止等待（阶段：{stage}）。回答未完成，请缩小问题范围后重试。', 'code': 'turn_timeout'}
                return
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait({pending}, timeout=min(heartbeat, remaining))
            if not done:
                yield 'heartbeat', {'elapsed': int(time.monotonic()-started), 'stage': stage}
                continue
            try:
                event, payload = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            if event == 'tool_start':
                stage = payload.get('label') or '检索资料'
            elif event == 'token':
                stage = '生成回答'
            elif event == 'step':
                stage = {'query_plan':'理解问题', 'hybrid_search':'检索资料', 'answer_generation':'生成回答', 'answer_verification':'核对答案'}.get(payload.get('stage'), stage)
            if event != 'token':
                logger.info('Answer progress event=%s stage=%s elapsed=%.2fs', event, stage, time.monotonic()-started)
            yield event, payload
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        close = getattr(iterator, 'aclose', None)
        if close is not None:
            await close()
