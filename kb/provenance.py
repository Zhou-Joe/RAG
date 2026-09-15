"""chunk 溯源引擎：MinerU content_list → 每 chunk 的页码 + 版面 bbox。

原理：
- MinerU（3.x）的 content_list 每个条目带 page_idx + bbox（0-1000 归一化、
  左上原点），表格带 table_body（HTML，与 md 里的 <table> 同源）、图片带
  img_path（images/<内容哈希>.jpg，与 md 引用同名）。
- 入库切块是在 _md_for_embedding 清洗文本上做的；这里把 content_list 的
  每个块【签名】（只留字母数字 + CJK）后在清洗文本的签名流里顺序定位，
  得到每个块的字符区间；再算每个 chunk 的字符区间，区间相交 = 该 chunk
  覆盖哪些版面块 → 页码 / bbox / 表格行号。
- 签名匹配天然免疫 markdown 转义、空格、标点、HTML 标记差异；表格块用与
  嵌入清洗同一套 _html_table_to_text 渲染后参与匹配，保证与清洗文本一致。

任何失败都只导致「该 chunk 无溯源」（引用退化为原文切片页），不阻断入库。
"""
from __future__ import annotations

import json
from bisect import bisect_left
import math
import logging
import re
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# content_list 里这些类型是页眉/页脚/页码，md 正文不包含（MinerU 渲染时丢弃），
# 参与匹配只会产生找不到的噪音
_SKIP_TYPES = {"header", "footer", "page_number", "aside_text", "page_footnote"}

# 签名短于该长度的块不参与定位（过短在全文中不唯一，定位不可靠）
_MIN_SIG = 4

_SIG_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]")


def valid_bbox(value):
    """Accept only finite MinerU 0–1000 rectangles; never invent a location."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        return None
    x0, y0, x1, y1 = value
    return list(value) if 0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000 else None


def _sig(text: str) -> str:
    """匹配用签名：小写、只留字母数字 + CJK（剥掉一切排版字符）。"""
    return _SIG_RE.sub("", (text or "").lower())


# ------------------------------------------------------------------
# content_list 的存取（落盘一份，供 reindex_clean 不重跑 OCR 也能重建溯源）
# ------------------------------------------------------------------
def _meta_dir(doc_id) -> Path:
    return Path(settings.DATA_DIR) / "doc_meta" / str(doc_id)


def persist_content_list(doc_id, content_list) -> None:
    """把 content_list 存盘（规范化为 list）。失败只记日志。"""
    cl = parse_content_list(content_list) if not isinstance(content_list, list) else content_list
    if not cl:
        return
    try:
        d = _meta_dir(doc_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "content_list.json").write_text(
            json.dumps(cl, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.warning("content_list 落盘失败（不影响入库）doc=%s", doc_id, exc_info=True)


def load_content_list(doc_id) -> list | None:
    p = _meta_dir(doc_id) / "content_list.json"
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            # 兼容旧版双重编码（MinerU 原始 JSON 字符串被再 dumps 一层）
            if isinstance(data, str):
                data = json.loads(data)
            return data if isinstance(data, list) else None
    except Exception:
        logger.warning("content_list 读取失败 doc=%s", doc_id, exc_info=True)
    return None


def parse_content_list(raw) -> list | None:
    """MinerU 结果里的 content_list 字段（JSON 字符串或已解析的 list）→ list。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else None
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------
# 块定位：content_list 块 → 清洗文本字符区间
# ------------------------------------------------------------------
class _SigIndex:
    """清洗文本的签名索引：签名流里第 i 个字符 → 原文位置。"""

    __slots__ = ("sig", "pos")

    def __init__(self, text: str):
        sig_chars: list[str] = []
        pos: list[int] = []
        for i, original in enumerate(text):
            for ch in original.lower():
                if _SIG_RE.fullmatch(ch):
                    continue
                sig_chars.append(ch)
                pos.append(i)
        self.sig = "".join(sig_chars)
        self.pos = pos

    def find(self, needle_sig: str, cursor: int) -> tuple[tuple[int, int], int] | None:
        """在签名流 cursor 起找 needle_sig。

        返回 (原文区间 [start, end), 下次查找游标)；找不到返回 None。
        """
        if not needle_sig:
            return None
        idx = self.sig.find(needle_sig, cursor)
        if idx < 0:
            return None
        end_i = min(idx + len(needle_sig) - 1, len(self.pos) - 1)
        return (self.pos[idx], self.pos[end_i] + 1), idx + len(needle_sig)


def _block_locate_text(block: dict) -> tuple[str, list[str]] | None:
    """取该块的「定位文本」与（表格的）逐行文本。返回 None 表示不参与定位。"""
    btype = block.get("type") or ""
    if btype in _SKIP_TYPES:
        return None
    if btype == "table":
        from .pipeline import _html_table_to_text
        html_body = block.get("table_body") or block.get("html") or ""
        rendered = _html_table_to_text(html_body) if html_body else ""
        rows = [r for r in rendered.split("\n") if r.strip()]
        if rows:
            return "\n".join(rows), rows
        cap = "".join(block.get("table_caption") or [])
        return (cap, []) if cap.strip() else None
    if btype in ("image", "chart"):
        return None  # 图片不在清洗文本里（嵌入前已剥除），单独走 img_path 映射
    text = block.get("text") or ""
    if not text and btype == "equation":
        text = block.get("text") or ""
    return (text, []) if text.strip() else None


def _locate_blocks(content_list: list, clean_md: str) -> tuple[list[dict], dict[str, dict]]:
    """把 content_list 块定位到 clean_md。

    返回 (spans, image_map)：
    - spans: [{"start", "end", "page", "bbox", "kind", "rows"}]（rows = 表格
      逐行文本，供 chunk 级行号归属），按文档序、区间单调递增
    - image_map: {图片文件名: {"page", "bbox"}}
    """
    index = _SigIndex(clean_md)
    spans: list[dict] = []
    image_map: dict[str, dict] = {}
    cursor = 0
    for block in content_list:
        if not isinstance(block, dict):
            continue
        btype = block.get("type") or ""
        page = block.get("page_idx")
        bbox = block.get("bbox") or None
        if btype in ("image", "chart"):
            img = block.get("img_path") or ""
            name = img.rsplit("/", 1)[-1]
            if name and type(page) is int and page >= 0:
                image_map[name] = {"page": page, "bbox": valid_bbox(bbox)}
            continue
        located = _block_locate_text(block)
        if located is None:
            continue
        text, rows = located
        s = _sig(text)
        if len(s) < _MIN_SIG or type(page) is not int or page < 0:
            continue
        hit = index.find(s, cursor)
        if hit is None:
            continue
        span, cursor = hit
        spans.append({
            "start": span[0], "end": span[1], "page": page,
            "bbox": valid_bbox(bbox),
            "kind": "table" if btype == "table" else "text",
            "rows": rows,
        })
    return spans, image_map


def _chunk_span(index: _SigIndex, clean_md: str, body: str, cursor: int):
    """chunk 正文在 clean_md 中的区间（正文是清洗文本的子串）。

    返回 (区间 or None, 下次游标)。优先子串直查（切块器产物即子串），
    失败再退签名匹配；游标回看 200 字符容忍重叠切块造成的回退。
    """
    body = body.strip()
    if not body:
        return None, cursor
    from_pos = max(0, cursor - 200)
    pos = clean_md.find(body, from_pos)
    if pos >= 0:
        return (pos, pos + len(body)), pos + len(body)
    sig = _sig(body)
    if len(sig) < _MIN_SIG:
        return None, cursor
    hit = index.find(sig, bisect_left(index.pos, from_pos))
    if hit is not None:
        span, _ = hit
        return span, span[1]
    return None, cursor


_SECTION_PREFIX_RE = re.compile(r"^【[^】]*】\s*\n?")


def build_provenance_rows(document, kb_slug: str, chunk_ids: list[str],
                          chunks: list, clean_md: str, content_list: list,
                          image_names: list[str] | None = None) -> list:
    """生成该文档全部 chunk 的 ChunkProvenance 行（不落库，调用方 bulk_create）。"""
    from .models import ChunkProvenance

    spans, image_map = _locate_blocks(content_list, clean_md)
    if not spans and not image_map:
        return []
    index = _SigIndex(clean_md)
    rows: list[ChunkProvenance] = []
    cursor = 0
    seen_ids: set[str] = set()
    for cid, chunk in zip(chunk_ids, chunks):
        if not cid or cid in seen_ids:
            continue
        body = _SECTION_PREFIX_RE.sub("", chunk.page_content or "", count=1)
        span, cursor = _chunk_span(index, clean_md, body, cursor)
        prov_blocks: list[dict] = []
        if span:
            cs, ce = span
            for sp in spans:
                if sp["end"] > cs and sp["start"] < ce:
                    prov_blocks.append(sp)
        if not prov_blocks:
            continue
        pages = sorted({sp["page"] for sp in prov_blocks})
        blocks_out: list[dict] = []
        chunk_sig = _sig(body)
        for sp in prov_blocks:
            entry = {"page": sp["page"], "bbox": sp["bbox"], "kind": sp["kind"]}
            if sp["kind"] == "table" and sp["rows"]:
                hit_rows = [i for i, r in enumerate(sp["rows"])
                            if len(_sig(r)) >= 2 and _sig(r) in chunk_sig]
                if hit_rows:
                    entry["rows"] = [hit_rows[0] + 1, hit_rows[-1] + 1]  # 1 基行号
            blocks_out.append(entry)
        seen_ids.add(cid)
        rows.append(ChunkProvenance(
            chunk_id=cid, document=document, kb_slug=kb_slug,
            page_start=pages[0], page_end=pages[-1], blocks=blocks_out,
        ))
    # 图片块：chunk_id 规则 img-<doc_id>-<hash>，页码/框直接来自 content_list
    for name in image_names or []:
        info = image_map.get(name)
        cid = f"img-{document.id}-{name}"
        if info and cid not in seen_ids:
            seen_ids.add(cid)
            rows.append(ChunkProvenance(
                chunk_id=cid, document=document, kb_slug=kb_slug,
                page_start=info["page"], page_end=info["page"],
                blocks=[{"page": info["page"], "bbox": info["bbox"], "kind": "image"}],
            ))
    return rows


def save_provenance(document, kb_slug: str, chunk_ids: list[str], chunks: list,
                    clean_md: str, content_list, image_names=None) -> int:
    """写入某文档全部 chunk 溯源（先清旧行——重跑入库时 chunk_id 全换）。

    返回写入行数；content_list 缺失/解析失败返回 0（文档无溯源，引用退化）。
    """
    from .models import ChunkProvenance

    cl = parse_content_list(content_list) if not isinstance(content_list, list) else content_list
    ChunkProvenance.objects.filter(document=document).delete()
    if not cl or not (clean_md or "").strip():
        return 0
    try:
        rows = build_provenance_rows(document, kb_slug, chunk_ids, chunks,
                                     clean_md, cl, image_names=image_names)
        ChunkProvenance.objects.bulk_create(rows, batch_size=500,
                                            ignore_conflicts=True)
        return len(rows)
    except Exception:
        logger.exception("chunk 溯源写入失败（不影响向量入库）doc=%s", document.id)
        return 0


# ------------------------------------------------------------------
# 检索结果页码标注（agent kb_search / 综合搜索共用）
# ------------------------------------------------------------------
def annotate_results(results: list[dict]) -> None:
    """就地为检索结果补 page_start/page_end/page_label（无溯源的结果不动）。"""
    ids = [r.get("chunk_id") for r in results if r.get("chunk_id")]
    if not ids:
        return
    try:
        from .models import ChunkProvenance
        provs = {
            p.chunk_id: p
            for p in ChunkProvenance.objects.filter(chunk_id__in=ids)
        }
    except Exception:
        logger.warning("溯源批量查询失败（跳过页码标注）", exc_info=True)
        return
    for r in results:
        p = provs.get(r.get("chunk_id") or "")
        if p is not None:
            r["page_start"] = p.page_start
            r["page_end"] = p.page_end
            r["page_label"] = p.page_label()
            r["prov_blocks"] = p.blocks


# ------------------------------------------------------------------
# 手册原文命中（行级字面匹配）→ chunk 溯源对齐
# ------------------------------------------------------------------
def locate_snippets(hits: list[dict]) -> None:
    """给「手册原文命中」的行级结果就地标 chunk_id 与页码。

    手册原文路在【原始 md_content】上逐行匹配（含 HTML 表格标签），而 chunk
    存的是清洗后的切块文本——签名匹配（只留字母数字+CJK）对两侧免疫标签/
    空白差异，行文本签名落在某 chunk 签名内即对齐。找不到就保持原状
    （chip 退化为跳查看页高亮）。doc 级缓存 chunks 的签名，30 处命中毫秒级。
    """
    from . import keyword_index
    from .models import Document

    content_hits = [h for h in hits if h.get("doc_id")
                    and h.get("match_type") == "content"]
    if not content_hits:
        return
    doc_ids = {h["doc_id"] for h in content_hits}
    try:
        docs = {str(d.id): d for d in
                Document.objects.filter(id__in=doc_ids)
                .only("id", "original_name", "kb__slug")
                .select_related("kb")}
    except Exception:
        logger.warning("手册原文溯源：文档查询失败", exc_info=True)
        return

    sig_cache: dict[str, list[tuple[str, str]]] = {}  # doc_id -> [(chunk_id, sig)]
    matched: list[dict] = []

    def _find_chunk(cands: list[str], cache: list[tuple[str, str]]) -> str:
        """候选签名（长→短前缀）逐级匹配；跨 chunk 的整行窗口用前缀也能命中。"""
        for s in cands:
            if len(s) < 4:
                continue
            for cand in (s, s[:32]):
                if len(cand) < 4:
                    continue
                for cid, csig in cache:
                    if cid and cand in csig:
                        return cid
        return ""

    for h in content_hits:
        doc = docs.get(h["doc_id"])
        if doc is None:
            continue
        cache_key = h["doc_id"]
        if cache_key not in sig_cache:
            try:
                sig_cache[cache_key] = [
                    (c.get("chunk_id") or "", _sig(c.get("text") or ""))
                    for c in keyword_index.get_chunks(doc.kb.slug, doc.original_name)
                ]
            except Exception:
                sig_cache[cache_key] = []
        if not sig_cache[cache_key]:
            continue
        # 优先短锚点（query±10 字，几乎必落单块）；整行窗口可能跨 chunk，
        # 只能靠前缀降级命中（命中词前部的块 = 该行所在表/段落的起始块）
        cands = [
            _sig(h.get("highlight_short") or ""),
            _sig(h.get("highlight") or ""),
        ]
        cid = _find_chunk(cands, sig_cache[cache_key])
        if cid:
            h["chunk_id"] = cid
            matched.append(h)
    # 批量补页码 + 短页码徽标文本（「第 43-45 页」→「43-45」）
    annotate_results(matched)
    for h in matched:
        label = h.get("page_label") or ""
        h["page_short"] = label.replace("第", "").replace(" ", "").replace("页", "")
        if not label:
            # chunk 对齐成功但该 chunk 无溯源行（照片直传/老入库）——
            # 保留 chunk_id 会让前端渲染 📍 按钮然后点击 404，撤回
            h.pop("chunk_id", None)


# ------------------------------------------------------------------
# 证据面板数据
# ------------------------------------------------------------------
def get_chunk_text(kb_slug: str, chunk_id: str) -> str:
    """从 Chroma 按 id 取 chunk 原文。

    优先复用进程内共享的 langchain Chroma 实例（与检索同源；新开
    PersistentClient 会与已缓存实例在同一 persist 目录上打架）。取不到
    （embedding 配置缺失/库不存在）返回空串，调用方降级即可。
    """
    try:
        from .retriever import get_kb_vectorstore
        res = get_kb_vectorstore(kb_slug)._collection.get(  # noqa: SLF001
            ids=[chunk_id], include=["documents"])
        docs = res.get("documents") or []
        return (docs[0] or "") if docs else ""
    except Exception:
        logger.warning("chunk 原文读取失败 kb=%s chunk=%s", kb_slug, chunk_id,
                       exc_info=True)
        return ""
