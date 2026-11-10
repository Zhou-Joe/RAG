"""多知识库检索封装。

每个 KB 有独立的 Chroma collection（persist_dir = data/chroma/<slug>/）。
"""
from __future__ import annotations

import logging
import math
import re
import threading
from pathlib import Path
from typing import Any

from django.conf import settings
from langchain_chroma import Chroma

from .config import embedding_settings, retrieval_settings
from .pipeline import _chroma_collection_name, _embeddings, _kb_persist_dir

# 进程级缓存：kb_slug -> Chroma。Chroma 的 Rust 后端在多线程下重复创建
# PersistentClient 会触发 'RustBindingsAPI' object has no attribute 'bindings'，
# 因此复用同一个客户端实例。
_VS_CACHE: dict[str, Chroma] = {}
_VS_LOCK = threading.Lock()
# 缓存对应的 embedding 配置指纹（base_url + model）。设置页换了 embedding
# 端点/模型后指纹变化，缓存全部失效——否则运行中的服务会继续拿【旧模型】
# 编码查询向量去比【新模型】入库的向量，结果静默变成垃圾。
_EMB_FINGERPRINT: str | None = None


def get_kb_vectorstore(kb_slug: str) -> Chroma:
    """打开指定 KB 的 Chroma 向量库（缓存实例，线程安全）。"""
    global _EMB_FINGERPRINT
    with _VS_LOCK:
        e = embedding_settings()
        # 指纹含 key 哈希：只换 key（同端点同模型）时缓存也要失效，
        # 否则旧客户端持旧 key → 401 → 静默降级纯关键词
        import hashlib
        key_h = hashlib.sha256((e["api_key"] or "").encode()).hexdigest()[:8]
        fp = f"{e['base_url']}|{e['model']}|{key_h}"
        if fp != _EMB_FINGERPRINT:
            _VS_CACHE.clear()
            _EMB_FINGERPRINT = fp
        vs = _VS_CACHE.get(kb_slug)
        if vs is None:
            vs = Chroma(
                collection_name=_chroma_collection_name(kb_slug),
                embedding_function=_embeddings(),
                persist_directory=str(_kb_persist_dir(kb_slug)),
            )
            _VS_CACHE[kb_slug] = vs
        return vs


def close_chroma_handles() -> None:
    """释放本进程持有的全部 Chroma 底层句柄（sqlite / Rust hnsw 段文件）。

    只清 _VS_CACHE 或只丢引用都不够：hnsw 段在 Rust 侧持有文件句柄，
    必须 System.stop()（Client.close() 在最后一个引用时同样触发）。
    Windows 上删除/移动 Chroma 目录前必须调用本函数，否则 WinError 32；
    Linux 无此约束，调用亦无害。
    """
    with _VS_LOCK:
        instances = list(_VS_CACHE.values())
        _VS_CACHE.clear()
    from chromadb.api.shared_system_client import SharedSystemClient
    for vs in instances:
        try:
            getattr(vs, "_client").close()
        except Exception:
            pass
    # 调用方可能仍握有裸 PersistentClient（如测试）：直接 stop 其 System
    for system in list(SharedSystemClient._identifier_to_system.values()):
        try:
            system.stop()
        except Exception:
            pass
    SharedSystemClient.clear_system_cache()


def reset_kb_collection(kb_slug: str) -> None:
    """Delete the collection through Chroma, never unlink an open SQLite file.

    Deleting/recreating the collection also resets its embedding dimension.
    Other collections and the database file remain intact.
    """
    from chromadb import PersistentClient
    from chromadb.errors import NotFoundError
    with _VS_LOCK:
        cached = _VS_CACHE.get(kb_slug)
        client = cached._client if cached is not None else PersistentClient(path=str(_kb_persist_dir(kb_slug)))
        try:
            client.delete_collection(_chroma_collection_name(kb_slug))
        except NotFoundError:
            pass
        _VS_CACHE.pop(kb_slug, None)


def delete_doc_vectors(kb_slug: str, source: str) -> bool:
    """删除某文档在 Chroma 中的全部向量（按 source=文件名 过滤）。

    刻意使用**原生 chromadb 客户端**而非 langchain Chroma：
    - 删除只依赖 metadata 过滤，不需要 embedding 计算；
    - embedding 配置（api_key 等）未就绪时，langchain 的 Chroma() 构造即抛
      'Missing credentials'，导致文档已删但向量残留（检索仍能搜到已删内容）。
    """
    from chromadb import PersistentClient
    from chromadb.config import Settings
    from chromadb.errors import NotFoundError
    from . import keyword_index

    persist_dir = str(_kb_persist_dir(kb_slug))
    coll_name = _chroma_collection_name(kb_slug)
    # 整体兜底：任何阶段失败（含 PersistentClient 构造期，如目录损坏）
    # 都不放过关键词清理——否则已删文档在关键词路"复活"
    vectors_removed = True
    try:
        client = PersistentClient(path=persist_dir, settings=Settings(anonymized_telemetry=False))
        try:
            col = client.get_collection(coll_name)
        except NotFoundError:
            # An unindexed source has no collection to clean.
            pass
        else:
            col.delete(where={"source": source})
    except Exception:
        vectors_removed = False
        logging.getLogger(__name__).exception(
            "Chroma 向量删除失败（继续清理关键词索引）kb=%s source=%s", kb_slug, source)
    # 两路清理互相独立：任一路失败不拖累另一路（否则残留孤儿行）
    keywords_removed = keyword_index.delete_doc(kb_slug, source)
    return vectors_removed and keywords_removed is not False


def _fuse_key(text: str) -> str:
    """RRF 融合的 chunk 身份键：全文 md5（向量路与关键词路同源同文；
    不能用前缀——表格类 chunk 常共享相同开头，会把不同块错误合并）。"""
    import hashlib
    return hashlib.md5((text or "").strip().encode("utf-8")).hexdigest()


# RRF（倒数排名融合）常数：score = Σ 1/(K + rank)，与两路分数的量纲无关
RRF_K = 60


def _result_key(row: dict) -> str:
    """保留库、文档和块身份，不能把不同文档的相同文字合并成一份证据。"""
    return "\0".join((row.get("kb_slug", ""), row.get("source", ""),
                       row.get("chunk_id") or _fuse_key(row.get("text", "")),
                       row.get("image", "")))


def _merge_graph(fused: dict, scope_slugs: list[str], document_slugs: list[str],
                 query: str, limit: int) -> None:
    """图谱作为可降级的第三路召回，共享 RRF；只返回真实原文块。"""
    try:
        from .knowledge_graph import retrieve_passages
        rows = retrieve_passages(scope_slugs, document_slugs, query, limit=limit)
        for rank, row in enumerate(rows, 1):
            item = fused.setdefault(_result_key(row), {**row, "score": 0.0, "via": set()})
            via = item["via"]
            via = set(via.split("+")) if isinstance(via, str) else via
            if "graph" not in via:
                item["score"] += 1.0 / (RRF_K + rank)
            item["via"] = via | {"graph"}
            item["graph_paths"] = list(dict.fromkeys(item.get("graph_paths", []) + row["graph_paths"]))
    except Exception:
        logging.getLogger(__name__).warning("图谱召回失败（继续常规检索）", exc_info=True)


def _feed_current_sources(kb_slug):
    """自动数据子库只检索当前已发布指针，排除暂存、历史和已删除版本。"""
    if not kb_slug.startswith('syncitem-'):return None
    from .models import FeedRecord,Document
    record=FeedRecord.objects.select_related('document','feed').filter(kb__slug=kb_slug).first()
    if record and record.feed.adapter_spec.get('ingest_mode') == 'documents':
        # Document-mode feed children may also contain ordinary uploads.
        return None
    if not record or record.deleted or not record.document_id or record.document.status!=Document.Status.COMPLETED:return {}
    return {record.document.original_name:str(record.document_id)}


def _feed_document_search_scope(kb_slug: str) -> tuple[set[str], set[str]] | None:
    """Return searchable manual sources and generated sources for a feed child.

    The current FeedRecord pointer identifies the source document even for old
    names. The generated filename pattern also covers superseded versions.
    """
    from .models import Document, FeedRecord
    from .feeds import _is_managed_feed_document

    record = (FeedRecord.objects.select_related('feed').filter(kb__slug=kb_slug,
              feed__adapter_spec__ingest_mode='documents').first())
    if record is None:
        return None
    generated_ids = {pk for pk in (record.document_id, record.pending_document_id) if pk}
    manual_sources: set[str] = set()
    generated_sources: set[str] = set()
    for doc in Document.objects.filter(kb_id=record.kb_id).only('id', 'original_name', 'status'):
        if doc.pk in generated_ids or _is_managed_feed_document(doc):
            generated_sources.add(doc.original_name)
        elif doc.status == Document.Status.COMPLETED:
            manual_sources.add(doc.original_name)
    return manual_sources, generated_sources


def _without_generated_feed_documents(slugs: list[str]) -> list[str]:
    """Skip only child libraries that have no searchable manual upload."""
    if not slugs:
        return []
    from .models import Document, FeedRecord
    from .feeds import _is_managed_feed_document

    records = list(FeedRecord.objects.filter(
        kb__slug__in=slugs, feed__adapter_spec__ingest_mode='documents',
    ).values('kb_id', 'kb__slug', 'document_id', 'pending_document_id'))
    if not records:
        return slugs
    generated = {row['kb__slug']: row for row in records}
    manual: set[str] = set()
    for doc in Document.objects.filter(
            kb_id__in=[row['kb_id'] for row in records],
            status=Document.Status.COMPLETED).select_related('kb').only(
                'id', 'kb_id', 'kb__slug', 'original_name', 'status'):
        row = generated[doc.kb.slug]
        if doc.pk not in (row['document_id'], row['pending_document_id']) \
                and not _is_managed_feed_document(doc):
            manual.add(doc.kb.slug)
    return [slug for slug in slugs if slug not in generated or slug in manual]


def search(kb_slug: str, query: str, k: int | None = None,
           query_embedding: list[float] | None = None,
           rerank_enabled: bool = True) -> list[dict[str, Any]]:
    """混合检索：向量（语义）+ 关键词（精确编号/术语）双路召回，RRF 融合。

    - 向量路：Chroma 相似度（当前配置的 embedding 模型）
    - 关键词路：keyword.db 文本匹配（与 embedding 模型无关，换模型不影响）
    - 任一路失败自动降级为单路，不互相拖累
    - query_embedding：调用方（search_folder/eval）已嵌好的查询向量，传入则
      本库不再重复嵌入——跨库扇出时 N 次相同查询的远程嵌入变 1 次
    - rerank_enabled=False：跳过精排（search_folder 合并后做一次全局 rerank，
      替代此前每库各一次）
    """
    feed_scope = _feed_document_search_scope(kb_slug)
    if feed_scope is not None and not feed_scope[0]:
        return []
    manual_sources = feed_scope[0] if feed_scope is not None else None
    k = k or retrieval_settings()["top_k"]
    recall = max(k * 3, 15)
    published=_feed_current_sources(kb_slug)
    if published is not None and not published:return []

    # ---- 向量路 ----
    vec_docs: list[tuple[Any, dict]] = []
    try:
        vs = get_kb_vectorstore(kb_slug)
        if query_embedding is None:
            query_embedding = vs.embeddings.embed_query(query)
        if query_embedding is not None:
            # include 不收 "ids"（chromadb 校验拒绝），但 query 结果恒带 ids
            res = vs._collection.query(  # noqa: SLF001 同 fetch_doc 的原生查询模式
                query_embeddings=[query_embedding], n_results=recall,
                **({'where': {'source': next(iter(published))}} if published is not None
                   else {'where': {'source': {'$in': sorted(manual_sources)}}}
                   if manual_sources is not None else {}),
                include=["documents", "metadatas"])
            from langchain_core.documents import Document as _Doc
            for d, m, cid in zip((res.get("documents") or [[]])[0] or [],
                                 (res.get("metadatas") or [[]])[0] or [],
                                 (res.get("ids") or [[]])[0] or []):
                meta = dict(m or {})
                meta["chunk_id"] = cid  # 溯源表按向量 id 关联（页码/证据面板）
                vec_docs.append((_Doc(page_content=d, metadata=meta), meta))
    except Exception as e:
        # 降级为纯关键词检索，但必须留痕——embedding 端点挂掉时运维要能发现
        logging.getLogger(__name__).warning("向量检索失败（降级纯关键词）kb=%s: %s", kb_slug, e)
        vec_docs = []

    # ---- 关键词路 ----
    from . import keyword_index
    kw_rows = keyword_index.search(
        kb_slug, query, limit=recall,
        **({'source': next(iter(published))} if published is not None else
           {'sources': sorted(manual_sources)} if manual_sources is not None else {}))

    # ---- RRF 融合（同一路内重复文本只计首次——语料里存在内容相同的 chunk） ----
    fused: dict[str, dict[str, Any]] = {}
    seen_vec: set[str] = set()
    for rank, (doc, meta) in enumerate(vec_docs, start=1):
        if published is not None and meta.get('source') not in published:continue
        if manual_sources is not None and meta.get('source') not in manual_sources:continue
        # 图片块以「图+上下文」独立身份参与融合（同名图的上下文与文本块不同，
        # 但 page_content 可能与章节文本块相同 → 融合键必须带上图片名防撞）
        key = _result_key({**meta, "text": doc.page_content, "kb_slug": kb_slug})
        if key in seen_vec:
            continue
        seen_vec.add(key)
        item = fused.setdefault(key, {
            "text": doc.page_content, "source": meta.get("source", ""),
            "section": meta.get("section") or meta.get("drug_name") or "",
            "score": 0.0, "via": set(),
            "chunk_id": meta.get("chunk_id", ""),
            "kb_slug": kb_slug,
        })
        if meta.get("type") == "image":
            item["type"] = "image"
            item["image"] = meta.get("image", "")
        item["score"] += 1.0 / (RRF_K + rank)
        item["via"].add("vec")
    seen_kw: set[str] = set()
    for rank, row in enumerate(kw_rows, start=1):
        if published is not None and row.get('source') not in published:continue
        if manual_sources is not None and row.get('source') not in manual_sources:continue
        key = _result_key({**row, "kb_slug": kb_slug})
        if key in seen_kw:
            continue
        seen_kw.add(key)
        item = fused.setdefault(key, {
            "text": row["text"], "source": row["source"], "section": row["section"],
            "score": 0.0, "via": set(), "chunk_id": row.get("chunk_id", ""),
            "kb_slug": kb_slug,
        })
        item["score"] += 1.0 / (RRF_K + rank)
        item["via"].add("kw")

    _merge_graph(fused, [kb_slug], [kb_slug], query, recall)
    if manual_sources is not None:
        fused = {key: row for key, row in fused.items()
                 if row.get('source') in manual_sources}
    results = sorted(fused.values(), key=lambda r: r["score"], reverse=True)[:k]

    # ---- 重排序（可选）：RRF 融合序取前若干 → cross-encoder 精排 → 取 top_k。
    # 失败/未启用自动降级用融合序，不阻断。----
    from . import rerank as rerank_mod
    rr_cfg = rerank_mod.rerank_settings()
    if rerank_enabled and rr_cfg["enabled"] and len(results) > 1:
        candidates = sorted(fused.values(), key=lambda r: r["score"], reverse=True)[:max(k * 2, 8)]
        rr = rerank_mod.rerank(query, [c["text"] for c in candidates], top_n=k)
        if rr:
            reranked = []
            for it in rr:
                idx = it.get("index")
                if idx is not None and 0 <= idx < len(candidates):
                    item = dict(candidates[idx])
                    via = item["via"]
                    via = "+".join(sorted(via)) if isinstance(via, set) else str(via)
                    item["via"] = via + "+rr"
                    item["rerank_score"] = round(it.get("relevance_score", 0.0), 4)
                    reranked.append(item)
            if reranked:
                results = reranked[:k]

    # set 不能 JSON 序列化 → 转字符串（vec / kw / vec+kw）
    for r in results:
        if isinstance(r.get("via"), set):
            r["via"] = "+".join(sorted(r["via"]))

    # 文件名 → doc_id 映射（来源出处链接需要 doc_id 指向查看页）。
    name_to_id: dict[str, str] = {}
    try:
        from .models import Document
        for d in Document.objects.filter(kb__slug=kb_slug).values("original_name", "id"):
            name_to_id[d["original_name"]] = str(d["id"])
    except Exception:
        pass

    for r in results:
        r["doc_id"] = (published.get(r['source'],'') if published is not None else r.get("doc_id") or name_to_id.get(r["source"], ""))
    if published is not None and _feed_current_sources(kb_slug)!=published:return []
    return results


def is_kb_ready(kb_slug: str) -> bool:
    """该 KB 是否已有向量。"""
    try:
        return get_kb_vectorstore(kb_slug)._collection.count() > 0  # noqa: SLF001
    except Exception:
        return False


IMAGE_RESULT_LIMIT = 8
IMAGE_MAX_DISTANCE = 1.45


def _image_distance(value, max_distance: float = IMAGE_MAX_DISTANCE) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        distance = float(value)
    except (TypeError, ValueError):
        return None
    return distance if math.isfinite(distance) and 0 <= distance <= max_distance else None


def _image_lane(slugs: list[str], query_embedding: list[float] | None,
                limit: int = IMAGE_RESULT_LIMIT, max_distance: float = IMAGE_MAX_DISTANCE) -> list[dict[str, Any]]:
    """图片块专用召回道（WeMM 多模态块，type=image）。

    实测多模态块与文本查询存在固有模态鸿沟（同查询下文本块余弦≈0.5、
    图片块仅≈0.35），887 个文本块混排时 20 个图片块永远进不了 top-k 召回窗口，
    图纸类文档的图片因此「搜不到」。这里按 where=type=image 独立召回，
    跨库合并后按距离升序取前 limit 张，由调用方并入结果尾部——
    语义命中区的「可命中图片」承诺由此兑现。
    max_distance：相关性阈值（余弦距离）。相关图（带文本锚定）实测
    0.5~0.7、弱锚定 CAD 图纸 ≈1.35、无关图（如随手照片对扭矩查询）
    ≥1.6——1.45 在「滤掉真无关」与「保留弱相关图纸」之间取折中。
    任何失败降级为空列表，不影响文本检索。
    """
    if query_embedding is None:
        return []
    slugs = _without_generated_feed_documents(slugs)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for slug in slugs:
        feed_scope = _feed_document_search_scope(slug)
        manual_sources = feed_scope[0] if feed_scope is not None else None
        try:
            col = get_kb_vectorstore(slug)._collection  # noqa: SLF001
            image_where: dict = {'type': 'image'}
            if manual_sources is not None:
                image_where = {'$and': [image_where,
                                       {'source': {'$in': sorted(manual_sources)}}]}
            res = col.query(
                query_embeddings=[query_embedding], n_results=limit,
                where=image_where,
                include=["documents", "metadatas", "distances"],
            )
            docs = (res.get("documents") or [[]])[0] or []
            metas = (res.get("metadatas") or [[]])[0] or []
            dists = (res.get("distances") or [[]])[0] or []
            cids = (res.get("ids") or [[]])[0] or []
            for d, m, dist, cid in zip(docs, metas, dists, cids):
                m = m or {}
                if manual_sources is not None and m.get('source') not in manual_sources:
                    continue
                distance = _image_distance(dist, max_distance)
                if not m.get("image") or distance is None:
                    continue
                item = {
                    "text": (d or "").strip() or "文档插图",
                    "source": m.get("source", ""),
                    "section": m.get("section", ""),
                    "score": 0.0, "via": "img",
                    "type": "image", "image": m["image"],
                    "chunk_id": cid or "",
                    "kb_slug": slug,
                    # 原始余弦距离（0=完全相同，2=相反）：供前端换算图文相似度
                    "distance": distance,
                }
                if isinstance(m.get("page"), int):
                    item["page_block"] = True  # 整页视觉块（视觉文档模式）
                ranked.append((distance, item))
        except Exception:
            logging.getLogger(__name__).warning(
                "图片召回道失败（跳过该库）kb=%s", slug, exc_info=True)
    ranked.sort(key=lambda t: t[0])
    out = []
    seen = set()
    for _distance, item in ranked:
        identity = (item['kb_slug'], item['source'], item['image'])
        if identity in seen:
            continue
        seen.add(identity)
        out.append(item)
        if len(out) >= limit:
            break
    # doc_id 映射（与 search() 同款：来源出处链接需要 doc_id）
    if out:
        try:
            from .models import Document
            name_to_id = {
                (d["kb__slug"], d["original_name"]): str(d["id"])
                for d in Document.objects.filter(
                    kb__slug__in=slugs).values("kb__slug", "original_name", "id")
            }
            for r in out:
                r["doc_id"] = name_to_id.get((r['kb_slug'], r["source"]), "")
                # 图片块 id 规则 img-<doc_id>-<hash>（入库时生成）；老向量库
                # 查询未带 id 的在这里按同一规则补
                if r.get("type") == "image" and not r.get("chunk_id"):
                    r["chunk_id"] = f"img-{r['doc_id']}-{r['image']}"
            out = [r for r in out if r["doc_id"]]
        except Exception:
            logging.getLogger(__name__).warning("图片召回道 doc_id 映射失败", exc_info=True)
            return []
    return out


def search_folder(child_slugs: list[str], query: str, k: int | None = None,
                  include_images: bool = False,
                  graph_scope_slugs: list[str] | None = None) -> list[dict[str, Any]]:
    """跨多个子文档库扇出检索：查询向量只嵌一次，各库本地召回（不精排）
    后合并去重，再对融合序做一次全局 rerank。

    此前每库各嵌一次查询 + 各做一次 rerank——N 库 = 2N 次远程调用且全串行
    （30 库 ≈ 15-45 秒）；现在固定 1 次嵌入 + 1 次精排，每库只剩本地
    Chroma/SQLite 查询。子库失败跳过并记日志，不阻断整体。

    include_images=True：额外跑图片专用召回道并把图片块并入结果尾部
    （文本 rerank 只看占位文本「文档插图」必弃图片，故在精排后并入）。
    """
    child_slugs = _without_generated_feed_documents(child_slugs)
    k = k or retrieval_settings()["top_k"]
    if not child_slugs:
        return []

    q_emb: list[float] | None = None
    try:
        q_emb = _embeddings().embed_query(query)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "跨库查询嵌入失败（各库降级纯关键词）: %s", str(e)[:120])

    all_results: list[dict[str, Any]] = []
    for slug in child_slugs:
        try:
            all_results.extend(
                search(slug, query, k=k, query_embedding=q_emb, rerank_enabled=False))
        except Exception:
            logging.getLogger(__name__).exception("子库检索失败（跳过）kb=%s", slug)
            continue

    if include_images:
        # The image panel has a separate distance-scored lane. Generic vector
        # recall has no distances and must not bypass that lane's cutoff or cap.
        all_results = [r for r in all_results if r.get('type') != 'image']

    # 跨库去重：同一块只保留一次，RRF 分累加后统一排序
    fused: dict[str, dict[str, Any]] = {}
    for r in all_results:
        key = _result_key(r)
        if key in fused:
            fused[key]["score"] = (fused[key].get("score", 0) or 0) + (r.get("score", 0) or 0)
        else:
            fused[key] = r
    _merge_graph(fused, list(dict.fromkeys(child_slugs + (graph_scope_slugs or []))),
                 child_slugs, query, max(k * 3, 15))
    results = sorted(fused.values(), key=lambda r: r["score"], reverse=True)[:k]

    # ---- 全局单次精排（失败/未启用降级融合序）----
    from . import rerank as rerank_mod
    rr_cfg = rerank_mod.rerank_settings()
    if rr_cfg["enabled"] and len(results) > 1:
        candidates = sorted(fused.values(), key=lambda r: r["score"], reverse=True)[:max(k * 2, 8)]
        rr = rerank_mod.rerank(query, [c["text"] for c in candidates], top_n=k)
        if rr:
            reranked = []
            for it in rr:
                idx = it.get("index")
                if idx is not None and 0 <= idx < len(candidates):
                    item = dict(candidates[idx])
                    via = item.get("via")
                    via = "+".join(sorted(via)) if isinstance(via, set) else str(via)
                    item["via"] = via + "+rr"
                    item["rerank_score"] = round(it.get("relevance_score", 0.0), 4)
                    reranked.append(item)
            if reranked:
                results = reranked[:k]

    for r in results:
        if isinstance(r.get("via"), set):
            r["via"] = "+".join(sorted(r["via"]))

    # ---- 图片专用召回道（精排后并入：rerank 只看文本，图片必被弃）----
    if include_images:
        results.extend(_image_lane(child_slugs, q_emb))
    return results


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
    from .models import Document, FeedRecord
    generated = (FeedRecord.objects.select_related('document')
                 .filter(kb__slug=kb_slug,
                         feed__adapter_spec__ingest_mode='documents').first())
    feed_scope = _feed_document_search_scope(kb_slug) if generated is not None else None
    manual_sources = feed_scope[0] if feed_scope is not None else None
    generated_sources = feed_scope[1] if feed_scope is not None else set()
    raw_out: list[dict[str, Any]] = []
    if generated is not None and (not source or source == getattr(generated.document, 'original_name', None)):
        # These source files are deliberately not embedded or keyword-indexed.
        # Read the current saved Markdown directly for an explicit document
        # request, using bounded in-memory chunks for normal evidence selection.
        doc = generated.document
        if not generated.deleted and doc is not None and doc.status == Document.Status.COMPLETED:
            markdown = doc.md_content or ''
            headings = list(re.finditer(r'(?m)^#{1,6}\s+(.+?)\s*$', markdown))
            sections = ([('', markdown[:headings[0].start()])] if headings and headings[0].start() else [])
            sections.extend((match.group(1).strip(), markdown[match.start():
                             headings[index + 1].start() if index + 1 < len(headings) else len(markdown)])
                            for index, match in enumerate(headings))
            if not headings:
                sections = [('', markdown)]
            for name, content in sections:
                if section and section != name:
                    continue
                for start in range(0, len(content), 4000):
                    text = content[start:start + 4000]
                    if contains and contains not in text:
                        continue
                    raw_out.append({'text': text, 'source': doc.original_name,
                                    'section': name, 'chunk_id': '', 'doc_id': str(doc.pk)})
                    if len(raw_out) >= limit:
                        return raw_out
    if source and source in generated_sources:
        return raw_out
    if source and manual_sources is not None and source not in manual_sources:
        return []
    if manual_sources is not None and not manual_sources:
        return raw_out
    if len(raw_out) >= limit:
        return raw_out
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
    docs = res.get("documents") or []
    metas = res.get("metadatas") or []
    ids = list(res.get("ids") or []) + [""] * max(0, len(docs) - len(res.get("ids") or []))
    for doc, meta, cid in zip(docs, metas, ids):
        text = doc or ""
        if contains and contains not in text:
            continue
        m = meta or {}
        if manual_sources is not None and m.get('source') not in manual_sources:
            continue
        out.append({
            "text": text,
            "source": m.get("source", ""),
            # 优先 section，回退旧向量的 drug_name
            "section": m.get("section") or m.get("drug_name") or "",
            "chunk_id": cid or "",
        })
    return (raw_out + out)[:limit]
