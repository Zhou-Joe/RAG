"""浏览器沙箱执行结果回报（write_analysis → check_analysis 自修复回路）。

链路：write_analysis（同步工具，在线程池执行）register(aid) 登记；
前端沙箱执行完 POST /kb/analysis/report/ 回填结果；check_analysis（异步
工具）经 asyncio.to_thread 调 wait() 阻塞等待。用 threading 原语而非
asyncio.Future：登记与等待可能发生在不同线程/协程，Event 跨线程安全。
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_RUNS: dict[str, dict] = {}
_MAX_KEPT = 64  # 防陈旧条目无限堆积（超时无人认领的）


def register(aid: str) -> None:
    """登记一次待回报的沙箱执行。"""
    with _LOCK:
        if len(_RUNS) >= _MAX_KEPT:
            for k in list(_RUNS)[: len(_RUNS) - _MAX_KEPT + 1]:
                _RUNS.pop(k, None)
        _RUNS[aid] = {"event": threading.Event(), "result": None}


def report(aid: str, ok: bool, error: str = "", stdout: str = "") -> bool:
    """浏览器回报执行结果。返回 False = 编号不存在或已回报过（迟到/无效）。"""
    with _LOCK:
        run = _RUNS.get(aid)
        if run is None or run["result"] is not None:
            return False
        run["result"] = {
            "ok": bool(ok),
            "error": (error or "")[:8000],
            "stdout": (stdout or "")[:8000],
        }
    run["event"].set()
    return True


def wait(aid: str, timeout: float) -> dict | None:
    """阻塞等待回报；超时返回 None。调用方应置于线程中（asyncio.to_thread）。"""
    with _LOCK:
        run = _RUNS.get(aid)
    if run is None:
        return None
    if run["event"].wait(timeout):
        return run["result"]
    return None
