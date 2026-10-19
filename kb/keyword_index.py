"""关键词检索索引（混合检索的 BM25 一路）。

设计（见 docs/rag-improvements-roadmap.md P0）：
- 与 Chroma 平行存同一批 chunk 的清洗文本（run_indexing 时双写）；
- 检索为纯文本匹配：CJK 二元组（bigram）+ 字母数字词，词频 × IDF 打分；
- 不用 FTS5：其分词器对中文（unicode61 连续 CJK 成单 token）和短词（trigram
  要求 ≥3 字符）都有坑，而本项目规模（每库 ≤ 数千 chunk）LIKE 预过滤 + Python
  打分是毫秒级，且对任意语言/长度确定性成立；
- 与 embedding 模型完全无关（换 WeMM/Qwen3 不影响本路）；
- 全部操作 try/except 包裹：关键词路任何故障只降级为纯向量，不阻断检索。

图片等多模态块无文本则不入库，仅走向量路；后续可用 MinerU 图片说明文字
作为文本代理入库（见 roadmap）。
"""
from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# CJK 字符区段（用于词项切分）
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# 字母数字词，允许中间的 - _ . 连接（图纸号 61250-56-0011、型号 M30x2 等保持整体）
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*|[\u4e00-\u9fff\u3400-\u4dbf]+")


def _db_path() -> Path:
    d = Path(settings.BASE_DIR) / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d / "keyword.db"


_SCHEMA_READY = False


def _conn() -> sqlite3.Connection:
    global _SCHEMA_READY
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    if not _SCHEMA_READY:  # DDL 只做一次（每次连接都建表是纯浪费）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunk ("
            " kb_slug TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',"
            " section TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,"
            " chunk_id TEXT NOT NULL DEFAULT '')"
        )
        # 旧库升级：chunk_id 列后加（溯源引入前的库没有）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(chunk)").fetchall()]
        if "chunk_id" not in cols:
            conn.execute("ALTER TABLE chunk ADD COLUMN chunk_id TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunk_kb ON chunk(kb_slug)"
        )
        _SCHEMA_READY = True
    return conn


# ------------------------------------------------------------------
# 写入 / 删除（与向量库生命周期同步）
# ------------------------------------------------------------------
def add_chunks(kb_slug: str, chunks: list, ids: list[str] | None = None, *, strict=False) -> None:
    """向量化成功后同步写入关键词索引。chunks 为 LCDocument 列表，
    ids 为对应的向量 id（与 Chroma 同源，溯源/证据面板按 id 关联；可缺省）。

    同 (kb_slug, source) 的旧行先删（同文档重建场景），保证幂等。
    """
    if not chunks:
        return
    rows = []
    for i, c in enumerate(chunks):
        meta = getattr(c, "metadata", None) or {}
        text = (getattr(c, "page_content", "") or "").strip()
        if not text:
            continue
        cid = (ids[i] if ids and i < len(ids) else "") or ""
        rows.append((kb_slug, meta.get("source", ""), meta.get("section", ""), text, cid))
    if not rows:
        return
    try:
        with _LOCK, contextlib.closing(_conn()) as conn, conn:
            conn.execute(
                "DELETE FROM chunk WHERE kb_slug = ? AND source = ?",
                (kb_slug, rows[0][1]),
            )
            conn.executemany(
                "INSERT INTO chunk (kb_slug, source, section, text, chunk_id) VALUES (?,?,?,?,?)",
                rows,
            )
    except Exception:
        logger.exception("关键词索引写入失败（kb=%s）", kb_slug)
        if strict:
            raise


def get_chunks(kb_slug: str, source: str) -> list[dict]:
    """取某文档在某库下的全部切片，按入库顺序（≈文档顺序）返回。

    供引用切片查看页（document_slices）列出命中切片用。失败返回 []。
    """
    try:
        with _LOCK, contextlib.closing(_conn()) as conn:
            rows = conn.execute(
                "SELECT text, section, chunk_id FROM chunk WHERE kb_slug = ? AND source = ? ORDER BY rowid",
                (kb_slug, source),
            ).fetchall()
        return [{"text": t, "section": s or "", "chunk_id": c or ""}
                for t, s, c in rows]
    except Exception:
        logger.exception("关键词索引读取失败（kb=%s source=%s）", kb_slug, source)
        return []


def delete_doc(kb_slug: str, source: str) -> None:
    """删除文档时同步清理（与 retriever.delete_doc_vectors 配对）。"""
    try:
        with _LOCK, contextlib.closing(_conn()) as conn, conn:
            conn.execute(
                "DELETE FROM chunk WHERE kb_slug = ? AND source = ?", (kb_slug, source))
    except Exception:
        logger.exception("关键词索引删除失败（kb=%s source=%s）", kb_slug, source)


def delete_kb(kb_slug: str) -> None:
    """整库重建前清空（与 reindex_clean 的集合重建配对）。"""
    try:
        with _LOCK, contextlib.closing(_conn()) as conn, conn:
            conn.execute("DELETE FROM chunk WHERE kb_slug = ?", (kb_slug,))
    except Exception:
        logger.exception("关键词索引清库失败（kb=%s）", kb_slug)


# ------------------------------------------------------------------
# 检索
# ------------------------------------------------------------------
def _terms(query: str) -> list[str]:
    """切词：字母数字词原样保留；CJK 连续串切成二元组（中文标准做法，
    完整短语会命中全部 bigram，散词只命中部分，天然带区分度）。"""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(query or ""):
        if _CJK_RE.match(tok[0]):  # CJK 串 → bigram
            if len(tok) == 1:
                out.append(tok)
            else:
                out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        else:
            out.append(tok)  # 编号 / 材质牌号 / 英文词
    # 去重保序
    seen, deduped = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped[:16]  # 防超长 query 拖慢打分


def search(kb_slug: str, query: str, limit: int = 20) -> list[dict]:
    """关键词检索：LIKE 预过滤 + 词频 × IDF 打分。

    返回 [{"text","source","section","score"}]（按分数降序，≤ limit）。
    任何异常返回 []（降级纯向量）。
    """
    terms = _terms(query)
    if not terms:
        return []
    try:
        # 锁内只做 fetch（连接即开即关）；打分在锁外，不阻塞其它库的读写
        with _LOCK, contextlib.closing(_conn()) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM chunk WHERE kb_slug = ?", (kb_slug,)).fetchone()[0]
            if not total:
                return []
            like_sql = " OR ".join(["text LIKE ? ESCAPE '\\'"] * len(terms))
            esc = [f"%{t.replace(chr(92), chr(92)*2).replace('%', chr(92)+'%').replace('_', chr(92)+'_')}%" for t in terms]
            rows = conn.execute(
                f"SELECT text, source, section, chunk_id FROM chunk WHERE kb_slug = ? AND ({like_sql})",
                [kb_slug, *esc],
            ).fetchall()
        if not rows:
            return []
        import math

        # 词频统计（IDF 用）：一次遍历同时累计所有词项的文档频率
        low_texts = [r[0].lower() for r in rows]
        n_t: dict[str, int] = {t: 0 for t in terms}
        for t in terms:
            tl = t.lower()
            n_t[t] = sum(1 for lt in low_texts if tl in lt)
        idf = {t: math.log(1 + total / (1 + n_t[t])) for t in terms}

        scored = []
        for (text, source, section, chunk_id), low in zip(rows, low_texts):
            sc = 0.0
            for t in terms:
                c = low.count(t.lower())
                if c:
                    sc += c * idf[t]
            if sc > 0:
                scored.append({"text": text, "source": source,
                               "section": section, "score": sc,
                               "chunk_id": chunk_id or ""})
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:limit]
    except Exception:
        logger.exception("关键词检索失败（kb=%s）", kb_slug)
        return []
