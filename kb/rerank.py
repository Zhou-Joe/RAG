"""重排序客户端（混合召回后的 cross-encoder 精排）。

调用 OpenAI/Jina 兼容的 /v1/rerank 端点（本地 llama-server --rerank，
或任何兼容服务）。任何失败只记录并返回 None → 调用方降级用 RRF 融合序，
绝不阻断检索。

⚠️ llama.cpp 的 /rerank 在部分版本有正确性问题
   （github.com/ggml-org/llama.cpp/issues/16407），上线前用 sanity_check()
   验证过再启用（评估面板 /kb/eval/ 也可观察整体效果）。
"""
from __future__ import annotations

import logging

import httpx

from .config import rerank_settings

logger = logging.getLogger(__name__)


def rerank(query: str, documents: list[str], top_n: int | None = None) -> list[dict] | None:
    """对 documents 按与 query 的相关度重排。

    返回 [{"index": i, "relevance_score": s}]（降序，≤ top_n）；失败返回 None。
    """
    cfg = rerank_settings()
    if not (cfg["enabled"] and cfg["base_url"] and cfg["model"]):
        return None
    payload = {
        "model": cfg["model"],
        "query": query,
        "documents": documents,
    }
    if top_n:
        payload["top_n"] = top_n
    try:
        r = httpx.post(
            (cfg["base_url"].rstrip("/") if cfg["base_url"].rstrip("/").endswith("/rerank") else cfg["base_url"].rstrip("/") + "/rerank"),
            json=payload,
            headers={"Authorization": f"Bearer {cfg['api_key'] or 'local-no-key'}"},
            timeout=15,
        )
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        # 缺 index 的条目视为协议错误直接丢弃（默认绑到第 0 篇会产生重复项）
        out = [{"index": it.get("index"),
                "relevance_score": float(it.get("relevance_score", 0.0))}
               for it in results if it.get("index") is not None]
        out.sort(key=lambda x: x["relevance_score"], reverse=True)
        return out if out else None
    except Exception as e:
        logger.warning("rerank 调用失败（降级 RRF 序）: %s", e)
        return None


def sanity_check() -> dict:
    """明显相关 vs 明显无关的对照测试（部署后先跑这个，防 llama.cpp bug）。"""
    docs = [
        "座椅受力部件采用 CrNiMo 合金钢，许用应力 205MPa",
        "每日开园前检查安全带和压杠",
        "轨道高度 27.9 米，运行速度 24.1 米/s",
    ]
    res = rerank("座椅受力部件的材料牌号是什么", docs)
    if res is None:
        return {"ok": False, "detail": "rerank 服务不可用或未启用"}
    first = res[0]["index"]
    return {
        "ok": first == 0,
        "detail": f"期望 top1=index0（材质句），实际 index{first}；"
                  f"分数: {[round(x['relevance_score'], 3) for x in res]}",
    }
