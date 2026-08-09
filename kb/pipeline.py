"""
OCR + 向量化流水线引擎。

处理一份 Document：
  1. MD/TXT → 直接读取
  2. PDF    → 调 MinerU API（submit→poll→fetch）→ Markdown
  3. 切块   → section-aware 分块（复用父项目逻辑）
  4. 向量化 → embedding → 写入该 KB 的 Chroma collection

异步执行：process_document() 在后台线程中调用。
"""
from __future__ import annotations

import html
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

# 让 FOS_RAG 能复用父项目的切块逻辑
_PARENT = Path(__file__).resolve().parent.parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from langchain_chroma import Chroma  # noqa: E402
from langchain_core.documents import Document as LCDocument  # noqa: E402
from langchain_text_splitters import RecursiveCharacterTextSplitter  # noqa: E402

from django.conf import settings  # noqa: E402

from .config import (  # noqa: E402
    embedding_settings,
    mineru_settings,
    retrieval_settings,
)
from .md2html import md_to_html  # noqa: E402


# ------------------------------------------------------------------
# MinerU OCR（PDF → Markdown）
# ------------------------------------------------------------------
def _mineru_headers() -> dict[str, str]:
    h: dict[str, str] = {"Accept": "application/json"}
    key = mineru_settings()["api_key"]
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


# MinerU 要求文件名只含 [a-zA-Z0-9._-]，3–512 字符，且以合法字符开头。
# 上传文件名常含中文/空格/特殊符号（如 "大型游乐设施…报告.pdf"），会被拒绝。
def _mineru_safe_name(raw: str) -> str:
    """把任意文件名转成 MinerU 可接受的 ASCII 名称，保留扩展名并保证唯一。"""
    stem = Path(raw).stem
    ext = Path(raw).suffix.lower() or ".pdf"
    # 非法字符 → 下划线，再去掉开头不合法字符
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", stem)
    safe = re.sub(r"^[^a-zA-Z0-9]+", "", safe)
    # 压缩连续下划线/点
    safe = re.sub(r"[_\-.]{2,}", "_", safe).strip("._-") or "doc"
    # 加短后缀保证唯一 & 最小长度 ≥3
    suffix = "_" + os.urandom(3).hex()
    # 截断到给扩展名留出空间（512 上限）
    max_stem = 512 - len(ext) - len(suffix)
    safe = safe[:max_stem]
    return f"{safe}{suffix}{ext}"


def _ocr_pdf(pdf_path: Path, on_progress=None) -> str:
    """调 MinerU API 把 PDF 转为 Markdown。

    on_progress: 可选回调 fn(str) → None，在轮询时调用以更新进度描述。
    """
    api_base = mineru_settings()["api_base"]

    with httpx.Client(timeout=7200) as client:
        # 健康检查
        try:
            client.get(f"{api_base}/health", headers=_mineru_headers(), timeout=10)
        except Exception as e:
            raise RuntimeError(f"无法连接 MinerU ({api_base}): {e}")

        # 提交任务
        if on_progress:
            on_progress("正在提交到 MinerU…")
        with pdf_path.open("rb") as f:
            files = {"files": (_mineru_safe_name(pdf_path.name), f, "application/pdf")}
            data = {
                "backend": mineru_settings()["backend"],
                "lang_list": mineru_settings()["lang"],
                "return_md": "true",
            }
            r = client.post(f"{api_base}/tasks", files=files, data=data, headers=_mineru_headers(), timeout=600)
        r.raise_for_status()
        task_id = r.json().get("task_id")
        if not task_id:
            raise RuntimeError(f"MinerU 未返回 task_id: {r.json()}")

        # 轮询
        waited = 0
        while waited < 7200:
            r = client.get(f"{api_base}/tasks/{task_id}", headers=_mineru_headers(), timeout=30)
            r.raise_for_status()
            status = (r.json().get("status") or "unknown").lower()
            if status in {"completed", "success", "succeeded", "done", "finished"}:
                break
            if status in {"failed", "error"}:
                raise RuntimeError(f"MinerU 任务失败: {r.json().get('error')}")
            # 每 12s 更新一次进度描述
            if on_progress and waited % 12 == 0:
                on_progress(f"OCR 识别中…（已等待 {waited}s）")
            time.sleep(6)
            waited += 6

        if on_progress:
            on_progress("正在获取 OCR 结果…")

        # 取结果
        r = client.get(f"{api_base}/tasks/{task_id}/result", headers=_mineru_headers(), timeout=120)
        r.raise_for_status()
        results = r.json().get("results") or {}
        for _fname, payload in results.items():
            md = payload.get("md_content") or payload.get("markdown") or payload.get("md")
            if md:
                return md
        raise RuntimeError("MinerU 结果中无 md_content")


def run_ocr(file_path: Path, file_type: str, on_progress=None) -> str:
    """提取文档的 Markdown 文本。

    MD/TXT → 直接读取；PDF → 调 MinerU。
    on_progress 仅对 PDF 有效（OCR 耗时较长）。
    """
    if file_type in ("md", "markdown", "txt"):
        if on_progress:
            on_progress("正在读取文本文件…")
        return file_path.read_text(encoding="utf-8", errors="ignore")
    elif file_type == "pdf":
        return _ocr_pdf(file_path, on_progress=on_progress)
    else:
        raise ValueError(f"不支持的文件类型: {file_type}")


# ------------------------------------------------------------------
# 嵌入前清洗：把 MinerU 的原始 HTML（表格等）转成结构化纯文本。
# 仅用于嵌入路径（run_indexing）；查看页（md_to_html）仍用原始 md_content。
# ------------------------------------------------------------------
# MinerU 的 <td> 恒为 <td rowspan=N colspan=M>text</td>（属性无引号、顺序固定），
# 故一条正则即可覆盖全部单元格。
_TD_RE = re.compile(r"<td\s+rowspan=(\d+)\s+colspan=(\d+)>(.*?)</td>", re.DOTALL)
_TR_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
_TABLE_RE = re.compile(r"<table>.*?</table>", re.DOTALL)
_DETAILS_RE = re.compile(r"<details>\s*<summary>[^<]*</summary>(.*?)</details>", re.DOTALL)
_IMG_TAG_RE = re.compile(r"<img\s[^>]*/?>", re.IGNORECASE)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LATEX_RE = re.compile(r"\$([^$]+)\$")


def _html_table_to_text(table_html: str) -> str:
    """把一个 <table>…</table> 转成「每行一格、| 分隔」的纯文本。

    用二维网格 + rowspan/colspan 下沉算法正确还原合并单元格：一个 rowspan=3 的单元格
    会向下方 2 行同一列「下沉」其值，使每行自描述（优于留空，便于嵌入/检索）。
    全宽分隔行（单格 colspan 跨所有列，如「主要受力结构部件」）只输出其文本，作表内小标题。
    """
    # rows[r] = (cells, is_divider) —— cells = [(text, rowspan, colspan), ...]
    # is_divider：本行是否由「单个单元格、其 colspan 覆盖该行全部列」构成（表内小标题）。
    rows: list[tuple[list[tuple[str, int, int]], bool]] = []
    for tr_m in _TR_RE.finditer(table_html):
        cells = [
            (html.unescape(c.group(3).strip()), int(c.group(1)), int(c.group(2)))
            for c in _TD_RE.finditer(tr_m.group(1))
        ]
        if cells:
            rows.append((cells, False))
    if not rows:
        return ""

    # 第一遍：确定总列数，并标记全宽分隔行（单格 colspan == 总列数）。
    # 总列数 = 任意「正常」行（非单格全宽）的列数之和的最大值。
    ncols = 0
    norm_row_cols: list[int | None] = []  # 每个非分隔行的列数
    for cells, _ in rows:
        row_cols = sum(cs for _, _, cs in cells)
        if len(cells) > 1 or cells[0][2] < row_cols:  # 多格，或单格但未跨满自身
            norm_row_cols.append(row_cols)
            if row_cols > ncols:
                ncols = row_cols
        else:
            norm_row_cols.append(None)  # 候选分隔行
    if ncols == 0:
        # 全表都是单格行 → 退化为逐行输出
        ncols = max((sum(cs for _, _, cs in cells) for cells, _ in rows), default=1)

    # 标记分隔行：单格且 colspan >= ncols
    parsed: list[tuple[list[tuple[str, int, int]], bool]] = []
    for (cells, _), rc in zip(rows, norm_row_cols):
        is_div = rc is None and len(cells) == 1 and cells[0][2] >= ncols
        parsed.append((cells, is_div))

    # 第二遍：铺二维网格（分隔行跳过网格，单独记下）。
    grid: list[list[str | None]] = []
    grid_divider: list[bool] = []  # grid_divider[gi] = True 表示该网格行是分隔行
    carries: dict[int, dict[int, str]] = {}

    def _set(row: list[str | None], col: int, val: str) -> None:
        while len(row) <= col:
            row.append(None)
        row[col] = val

    gi = 0  # 网格行指针（分隔行也占一行）
    for cells, is_div in parsed:
        while len(grid) <= gi:
            grid.append([])
            grid_divider.append(False)
        grid_row = grid[gi]
        if is_div:
            # 分隔行：把文本填进第 0 列，渲染时只输出它
            _set(grid_row, 0, cells[0][0])
            grid_divider[gi] = True
            gi += 1
            continue
        covered: set[int] = set()
        # 1) 放 carry（上方 rowspan 下沉进来的）
        for col, val in carries.get(gi, {}).items():
            _set(grid_row, col, val)
            covered.add(col)
        # 2) 放本行实际发射的单元格
        for text, rs, cs in cells:
            col = 0
            while col in covered:
                col += 1
            for j in range(cs):
                _set(grid_row, col + j, text)
                covered.add(col + j)
            # rowspan>1：下沉到下方 rs-1 行的同一批列
            for k in range(1, rs):
                for j in range(cs):
                    carries.setdefault(gi + k, {})[col + j] = text
        gi += 1

    # 渲染：分隔行只输出文本；普通行用「 | 」连接。
    out_lines: list[str] = []
    for gr, is_div in zip(grid, grid_divider):
        if is_div:
            out_lines.append((gr[0] if gr and gr[0] else "").strip())
            continue
        vals = [(gr[c] if c < len(gr) and gr[c] is not None else "") for c in range(ncols)]
        out_lines.append(" | ".join(vals).rstrip(" |"))
    return "\n".join(out_lines)


def _md_for_embedding(md: str) -> str:
    """生成「用于嵌入」的纯文本版 markdown（不改原始 md_content）。

    转换：HTML 表格 → 结构化文本；去 <img>/markdown 图片；<details> 只留正文；
    去 LaTeX 的 $ 包裹；HTML 实体反转义。保留 markdown 标题/列表/段落结构
    （切块仍按 ## 分节）。
    """
    if not md:
        return ""
    # 1. 表格 → 结构化文本
    out = _TABLE_RE.sub(lambda m: _html_table_to_text(m.group(0)), md)
    # 2. <details> 只留内部正文
    out = _DETAILS_RE.sub(lambda m: html.unescape(m.group(1).strip()), out)
    # 3. 去图片标签（HTML <img> 与 markdown ![](...)）
    out = _IMG_TAG_RE.sub("", out)
    out = _MD_IMG_RE.sub("", out)
    # 4. LaTeX 去掉 $ 包裹，保留内部文本
    out = _LATEX_RE.sub(r"\1", out)
    # 5. 全局反转义 HTML 实体
    out = html.unescape(out)
    # 6. 压缩连续 3+ 空行为 2 空（清洗后表格展开可能产生大量空行）
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


# ------------------------------------------------------------------
# 切块（section-aware，按 ## 标题分节）
# ------------------------------------------------------------------
_SECTION_HEADING = re.compile(r"^#+\s*([^\n【\(]+)", re.MULTILINE)
_SECTION_SPLIT = re.compile(r"(?=\n##\s)", re.MULTILINE)


def _clean_section_name(raw: str) -> str:
    """从标题文本里取一段干净的节名（取首个中文片段，回退原文）。"""
    m = re.search(r"[\u4e00-\u9fff]{1,10}", raw)
    return (m.group(0) if m else raw).strip()


def _section_aware_chunk(text: str, source_name: str) -> list[LCDocument]:
    """按 ## 标题分节切块，每块携带所属节名（section）。

    切块器保留 chunk_overlap（默认 150 字），让相邻块共享一段重叠内容，
    避免表格/段落被硬切断后丢失上下文。MinerU 的原始 HTML（表格/图片）原样保留。
    """
    rs = retrieval_settings()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=rs["chunk_size"],
        chunk_overlap=rs["chunk_overlap"],
        separators=["\n\n", "。", "；", "\n", " ", ""],
    )
    out: list[LCDocument] = []
    sections = _SECTION_SPLIT.split(text)
    for sec in sections:
        if not sec.strip():
            continue
        head_m = _SECTION_HEADING.match(sec.lstrip())
        section = _clean_section_name(head_m.group(1)) if head_m else ""
        sub_texts = splitter.split_text(sec) if len(sec) > rs["chunk_size"] else [sec]
        for ct in sub_texts:
            ct = ct.strip()
            if not ct:
                continue
            if section and section not in ct[:50]:
                ct = f"【{section}】\n{ct}"
            out.append(LCDocument(
                page_content=ct,
                metadata={"source": source_name, "section": section},
            ))
    return out


# ------------------------------------------------------------------
# Embedding（复用父项目客户端）
# ------------------------------------------------------------------
def _embeddings():
    from langchain_openai import OpenAIEmbeddings

    e = embedding_settings()
    return OpenAIEmbeddings(
        model=e["model"],
        api_key=e["api_key"],
        base_url=e["base_url"],
        check_embedding_ctx_length=False,
    )


def _kb_persist_dir(kb_slug: str) -> Path:
    d = Path(settings.CHROMA_ROOT) / kb_slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _chroma_collection_name(kb_slug: str) -> str:
    """把 kb_slug 转成合法的 Chroma collection 名。

    Chroma 要求 collection 名：3–512 字符，仅 [a-zA-Z0-9._-]，
    且以字母/数字开头和结尾。短 slug（如 'P8' 只有 2 字符）会被拒绝，
    所以补足长度；非法字符替换为下划线。
    """
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", kb_slug)
    safe = re.sub(r"^[^a-zA-Z0-9]+", "", safe)
    safe = re.sub(r"[_\-.]{2,}", "_", safe).strip("._-") or "kb"
    # 保证长度 ≥ 3 且以字母/数字结尾（Chroma 要求 3–512，首尾须为 [a-zA-Z0-9]）
    if len(safe) < 3:
        safe = (safe + "012"[:3 - len(safe)])
    # 最终再保险一次：去掉尾部非字母数字
    safe = re.sub(r"[^a-zA-Z0-9]+$", "", safe)
    if len(safe) < 3:
        safe = (safe + "012")[:3]
    return safe[:512]


def run_indexing(md_content: str, kb_slug: str, source_name: str) -> int:
    """切块 + 向量化 → 写入该 KB 的 Chroma。返回 chunk 数。

    嵌入前先清洗：把 MinerU 的原始 HTML 表格转成结构化纯文本（仅此路径清洗；
    查看页 md_to_html 仍用原始 md_content 渲染真表格）。
    """
    clean_md = _md_for_embedding(md_content)
    chunks = _section_aware_chunk(clean_md, source_name)
    if not chunks:
        return 0
    vs = Chroma(
        collection_name=_chroma_collection_name(kb_slug),
        embedding_function=_embeddings(),
        persist_directory=str(_kb_persist_dir(kb_slug)),
    )
    vs.add_documents(chunks)
    return len(chunks)


# ------------------------------------------------------------------
# 完整流水线（异步执行）
# ------------------------------------------------------------------
def build_doc_html(doc) -> None:
    """根据 doc.md_content 生成 HTML 正文并存库（供文档查看页使用）。

    幂等：每次用最新 md_content 重建。供流水线、回填命令、查看页懒构建复用。
    """
    from django.utils import timezone
    from .md2html import md_to_html as _md_to_html

    doc.html_content = _md_to_html(doc.md_content or "")
    doc.html_built_at = timezone.now()
    doc.save(update_fields=["html_content", "html_built_at", "updated_at"])


def process_document(doc_id: str) -> None:
    """处理一份 Document 的完整流水线（OCR → 切块 → 向量化）。

    设计为在后台线程中执行；内部捕获所有异常并更新 Document.status。
    """
    # 延迟 import 避免 AppRegistryNotReady
    import django
    django.setup()
    from .models import Document, KnowledgeBase

    try:
        doc = Document.objects.get(id=doc_id)
        file_path = Path(doc.file.path)

        # 进度回调：更新 stage_detail
        def update_stage(detail: str):
            doc.stage_detail = detail
            doc.save(update_fields=["stage_detail", "updated_at"])

        # 阶段 1: OCR
        doc.status = Document.Status.OCR
        doc.stage_detail = "开始 OCR…"
        doc.save(update_fields=["status", "stage_detail", "updated_at"])
        md = run_ocr(file_path, doc.file_type, on_progress=update_stage)
        doc.md_content = md
        doc.html_content = md_to_html(md)
        from django.utils import timezone
        doc.html_built_at = timezone.now()
        doc.save(update_fields=["md_content", "html_content", "html_built_at", "updated_at"])

        # 阶段 2: 向量化
        doc.status = Document.Status.INDEXING
        doc.stage_detail = "正在切块 + 向量化…"
        doc.save(update_fields=["status", "stage_detail", "updated_at"])
        n_chunks = run_indexing(md, doc.kb.slug, doc.original_name)
        doc.chunk_count = n_chunks

        # 阶段 3: 完成
        doc.status = Document.Status.COMPLETED
        doc.stage_detail = f"已完成 · {n_chunks} 个片段"
        doc.save(update_fields=["status", "stage_detail", "chunk_count", "updated_at"])

        # 更新 KB 缓存
        kb = doc.kb
        kb.doc_count = kb.documents.filter(status=Document.Status.COMPLETED).count()
        kb.chunk_count = sum(d.chunk_count for d in kb.documents.filter(status=Document.Status.COMPLETED))
        kb.save(update_fields=["doc_count", "chunk_count", "updated_at"])

    except Exception as e:
        # 标记失败
        try:
            doc = Document.objects.get(id=doc_id)
            doc.status = Document.Status.FAILED
            doc.error_msg = str(e)[:2000]
            doc.stage_detail = "处理失败"
            doc.save(update_fields=["status", "error_msg", "stage_detail", "updated_at"])
        except Exception:
            pass


def process_document_async(doc_id: str) -> None:
    """在后台 daemon 线程中启动流水线。"""
    t = threading.Thread(target=process_document, args=(str(doc_id),), daemon=True)
    t.start()
