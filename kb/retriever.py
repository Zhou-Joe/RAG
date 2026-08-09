"""多知识库检索封装。

每个 KB 有独立的 Chroma collection（persist_dir = data/chroma/<slug>/）。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from django.conf import settings
from langchain_chroma import Chroma

from .config import retrieval_settings
from .pipeline import _chroma_collection_name, _embeddings, _kb_persist_dir

# 进程级缓存：kb_slug -> Chroma。Chroma 的 Rust 后端在多线程下重复创建
# PersistentClient 会触发 'RustBindingsAPI' object has no attribute 'bindings'，
# 因此复用同一个客户端实例。
_VS_CACHE: dict[str, Chroma] = {}
_VS_LOCK = threading.Lock()


def get_kb_vectorstore(kb_slug: str) -> Chroma:
    """打开指定 KB 的 Chroma 向量库（缓存实例，线程安全）。"""
    with _VS_LOCK:
        vs = _VS_CACHE.get(kb_slug)
        if vs is None:
            vs = Chroma(
                collection_name=_chroma_collection_name(kb_slug),
                embedding_function=_embeddings(),
                persist_directory=str(_kb_persist_dir(kb_slug)),
            )
            _VS_CACHE[kb_slug] = vs
        return vs


def search(kb_slug: str, query: str, k: int | None = None) -> list[dict[str, Any]]:
    """在指定 KB 中语义检索。"""
    k = k or retrieval_settings()["top_k"]
    vs = get_kb_vectorstore(kb_slug)
    results = vs.similarity_search_with_relevance_scores(query, k=k)

    # 文件名 → doc_id 映射（来源出处链接需要 doc_id 指向查看页）。
    # 向量只带 source=文件名，故按 KB 一次性解析；查询失败则 doc_id 留空。
    name_to_id: dict[str, str] = {}
    try:
        from .models import Document
        for d in Document.objects.filter(kb__slug=kb_slug).values("original_name", "id"):
            name_to_id[d["original_name"]] = str(d["id"])
    except Exception:
        pass

    out: list[dict[str, Any]] = []
    for doc, score in results:
        meta = doc.metadata or {}
        src = meta.get("source", "")
        # 优先读 section；旧向量（retriever 改造前索引的）带的是 drug_name，回退读取
        section = meta.get("section") or meta.get("drug_name") or ""
        out.append({
            "text": doc.page_content,
            "source": src,
            "section": section,
            "doc_id": name_to_id.get(src, ""),
            "score": float(score) if score is not None else 0.0,
        })
    return out


def is_kb_ready(kb_slug: str) -> bool:
    """该 KB 是否已有向量。"""
    try:
        return get_kb_vectorstore(kb_slug)._collection.count() > 0  # noqa: SLF001
    except Exception:
        return False


def search_folder(child_slugs: list[str], query: str, k: int | None = None) -> list[dict[str, Any]]:
    """跨多个子文档库扇出检索：对每个子库各取 top_k，合并后按 score 排序取前 k。

    用于文件夹级搜索（通用问题跨所有子文档库）。
    """
    k = k or retrieval_settings()["top_k"]
    all_results: list[dict[str, Any]] = []
    for slug in child_slugs:
        try:
            all_results.extend(search(slug, query, k=k))
        except Exception:
            continue  # 某个子库缺失/出错不阻断整体
    all_results.sort(key=lambda r: r.get("score", 0), reverse=True)
    return all_results[:k]


def fetch_doc(
    kb_slug: str,
    source: str = "",
    section: str = "",
    contains: str = "",
    limit: int = 40,
) -> list[dict[str, Any]]:
    """按元数据/内容条件提取一个文档库的片段（不做相似度检索）。

    用于「取完整表格/列表」这类需求：相似度检索只返回 top_k 片段，会漏掉同一张表
    被切到其它块里的行；这里按 source/section 元数据 + 内容关键词一次性取出全部命中片段，
    按入库顺序（即文档原序）返回，便于把碎片拼回完整内容。

    参数:
        source: 限定文档文件名（不传则该库下所有文档）。
        section: 限定章节名（切块时按 ## 标题提取；不传则所有章节）。
        contains: 只保留正文含该子串的片段（用于跨章节/section 元数据缺失的表格，
                  如 contains="受力部件" 可抓全一张散落多块的表）。
        limit: 最多返回片段数（控制 token 用量，默认 40）。

    返回: [{"text","source","section"}, ...]，按入库顺序。
    """
    vs = get_kb_vectorstore(kb_slug)
    where: dict[str, str] = {}
    if source:
        where["source"] = source
    if section:
        where["section"] = section

    # 注意：limit 是对【最终输出】的上限，不能下推给 Chroma 的 limit。
    # 因为 contains 是取出后才做的过滤，若先 limit=N 再过滤，会把目标片段
    # 排在 N 之后的情况漏掉（整本文档可能几百块，关键词散落在各处）。
    # 所以先全量取（仅按 where 过滤），再用 contains 过滤，最后截断到 limit。
    res = vs._collection.get(  # noqa: SLF001  与 views.py 的 delete(where=) 同一模式
        where=where or None,
        include=["documents", "metadatas"],
    )
    out: list[dict[str, Any]] = []
    for doc, meta in zip(res.get("documents") or [], res.get("metadatas") or []):
        text = doc or ""
        if contains and contains not in text:
            continue
        m = meta or {}
        out.append({
            "text": text,
            "source": m.get("source", ""),
            # 优先 section，回退旧向量的 drug_name
            "section": m.get("section") or m.get("drug_name") or "",
        })
    return out[:limit]
