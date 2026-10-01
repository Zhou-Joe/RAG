"""Single-process ASGI answer jobs. Subscribers never own generation lifetime.

Message snapshots are durable; process restart does not resume model inference.
"""
import asyncio
import contextvars
import json
import logging
from collections import deque
from weakref import WeakKeyDictionary
from asgiref.sync import sync_to_async

_runs = {}
_gates = WeakKeyDictionary()
ACTIVE = ('queued', 'running')


class AnswerRun:
    def __init__(self, answer_id):
        self.answer_id = answer_id
        self.frames = deque(maxlen=4096)
        self.sequence = 0
        self.changed = asyncio.Event()
        self.finished = False
        self.stopped = False
        self.progress = {'stage': '排队等待回答', 'elapsed': 0}
        self.task = None

    def emit(self, frame):
        self.frames.append((self.sequence, frame))
        self.sequence += 1
        self.changed.set()
        if frame.startswith('event: heartbeat\n'):
            self.progress = json.loads(frame.split('data: ', 1)[1])

    async def subscribe(self):
        cursor = 0
        while True:
            self.changed.clear()
            if self.frames and cursor < self.frames[0][0]:
                yield 'event: error\ndata: {"message":"页面接收落后，请重新打开会话恢复已保存内容。"}\n\n'
                return
            for sequence, frame in list(self.frames):
                if sequence >= cursor:
                    cursor = sequence + 1
                    yield frame
            if self.finished:
                return
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=10)
            except asyncio.TimeoutError:
                yield 'event: heartbeat\ndata: ' + json.dumps(self.progress, ensure_ascii=False) + '\n\n'


def start(answer_id, source_factory):
    loop = asyncio.get_running_loop()
    gate = _gates.setdefault(loop, asyncio.Semaphore(1))
    run = AnswerRun(answer_id)
    _runs[answer_id] = run

    async def update(**fields):
        from .models import Message
        await sync_to_async(lambda: Message.objects.filter(pk=answer_id).update(**fields))()

    async def produce():
        try:
            async with gate:
                await update(completion_status='running')
                from contextlib import aclosing
                async with aclosing(source_factory()) as source:
                    async for frame in source:
                        run.emit(frame)
        except asyncio.CancelledError:
            run.emit('event: error\ndata: ' + json.dumps({'message': '已按要求停止回答。' if run.stopped else '回答服务已关闭，请重试。'}, ensure_ascii=False) + '\n\n')
            await update(completion_status='incomplete', failure_reason='已按要求停止回答。' if run.stopped else '回答服务已关闭，请重试。')
        except Exception:
            logging.getLogger(__name__).exception('Background answer failed id=%s', answer_id)
            await update(completion_status='incomplete', failure_reason='后台回答失败，请重试。')
            run.emit('event: error\ndata: {"message":"后台回答失败，请重试。"}\n\n')
        finally:
            run.finished = True
            run.emit('event: done\ndata: {}\n\n')
            _runs.pop(answer_id, None)

    # Do not inherit the HTTP request's ThreadSensitiveContext: it closes its
    # executor when the browser disconnects while this task still needs SQLite.
    run.task = asyncio.create_task(produce(), context=contextvars.Context())
    return run


def stop(answer_id):
    run = _runs.get(answer_id)
    if not run or run.finished:
        return False
    if run.stopped:
        return True
    run.stopped = True
    run.task.get_loop().call_soon_threadsafe(run.task.cancel)
    return True


def state(answer_id):
    run = _runs.get(answer_id)
    return dict(run.progress) if run and not run.finished else None


def reconcile(messages):
    """Mark interrupted jobs after process restart, never pretend they are busy."""
    from .models import Message
    from django.utils import timezone
    for message in messages:
        if message.completion_status in ACTIVE and state(message.pk) is None and (timezone.now() - message.created_at).total_seconds() > 10:
            Message.objects.filter(pk=message.pk, completion_status__in=ACTIVE).update(
                completion_status='incomplete', failure_reason='回答服务已重启，任务未能完成，请重试。')
