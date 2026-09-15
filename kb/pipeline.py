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
import io
import logging
import hashlib
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from urllib.parse import urlsplit

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


def _ocr_pdf(pdf_path: Path, on_progress=None) -> tuple[str, dict[str, str], list | None]:
    """调 MinerU API 把 PDF 转为 Markdown。

    返回 (md, images, content_list)：
    - images 是 {文件名: data:image/jpeg;base64,...}（MinerU 图片名按内容哈希
      生成，对已有文档重跑 OCR 可以无损补回图片）；
    - content_list 是 MinerU 的版面条目列表（每条带 page_idx + 归一化 bbox），
      供 chunk 溯源（定位到第几页/红圈框选）；服务端不支持时为 None。
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
                "return_images": "true",
                # 版面条目（页码 + bbox）：chunk 溯源与证据面板的原料
                "return_content_list": "true",
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
                # content_list 是 JSON 字符串（服务端按原文件读回）；解析交给
                # provenance.parse_content_list，这里原样透传
                return md, payload.get("images") or {}, payload.get("content_list")
        raise RuntimeError("MinerU 结果中无 md_content")


def run_ocr(file_path: Path, file_type: str, on_progress=None) -> str:
    """提取文档的 Markdown 文本（兼容旧调用方，只要文本）。"""
    return run_ocr_with_images(file_path, file_type, on_progress)[0]


def run_ocr_with_images(file_path: Path, file_type: str, on_progress=None) -> tuple[str, dict[str, str], list | None]:
    """提取 Markdown + 文档图片 + 版面条目。

    MD/TXT → 直接读取（无图片）；PDF → 调 MinerU（return_images + content_list）；
    IMAGE → 直传照片标准化（EXIF 转正 + 缩放）后包装成单图文档。
    返回 (md, images, content_list)；images = {文件名: data:image/...;base64,...}，
    content_list 仅 PDF 有（chunk 溯源用；服务端未返回时为 None）。
    on_progress 仅对 PDF / 大图有效。
    """
    if file_type in ("md", "markdown", "txt"):
        if on_progress:
            on_progress("正在读取文本文件…")
        return file_path.read_text(encoding="utf-8", errors="ignore"), {}, None
    elif file_type == "pdf":
        return _ocr_pdf(file_path, on_progress=on_progress)
    elif file_type == "image":
        md, images = _process_photo(file_path, on_progress=on_progress)
        return md, images, None
    else:
        raise ValueError(f"不支持的文件类型: {file_type}")


# 直传照片嵌入前的统一边长上限：手机原图动辄 4000px/5MB+，
# base64 后会撑爆 WeMM 请求；1568px 是主流多模态模型的常用输入档
_PHOTO_MAX_SIDE = 1568


def _process_photo(file_path: Path, on_progress=None) -> tuple[str, dict[str, str]]:
    """直传照片 → (md, images)，结构与 MinerU 的 PDF 输出一致，
    落盘/图片块索引/引用缩略图全部复用 PDF 插图链路。

    EXIF 转正（手机竖拍）→ 超 1568px 缩边 → JPEG 重编码 →
    以内容哈希命名（sha256，与 MinerU 约定相同，保证幂等）。
    md 里 caption 与图片同行：_image_chunks 剥掉图片语法后，
    剩余文本即该图片块的检索上下文。
    """
    from PIL import Image, ImageOps

    if on_progress:
        on_progress("正在处理图片…")
    with Image.open(file_path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            if im.mode in ("RGBA", "LA", "PA"):
                bg.paste(im, mask=im.convert("RGBA").split()[-1])
            else:
                bg.paste(im.convert("RGB"))
            im = bg
        if max(im.size) > _PHOTO_MAX_SIDE:
            im.thumbnail((_PHOTO_MAX_SIDE, _PHOTO_MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
    raw = buf.getvalue()
    name = hashlib.sha256(raw).hexdigest() + ".jpg"
    b64 = _base64.b64encode(raw).decode()
    caption = f"照片上传：{Path(file_path).name}"
    return f"{caption} ![](images/{name})", {name: f"data:image/jpeg;base64,{b64}"}


# ------------------------------------------------------------------
# 文档图片落盘与 URL 重写
# 图片存 data/doc_images/<doc_id>/<hash>.jpg（不进 media/：
# media 由 Django 静态服务直接暴露，会绕过部门级访问控制；
# 读取统一走 /kb/doc/<id>/img/<name> 视图做权限校验）。
# ------------------------------------------------------------------
import base64 as _base64

# MinerU 图片名 = 内容哈希（sha256，64 位 hex）；兼容 32 位（md5）
_DOC_IMG_NAME_RE = re.compile(r"^[0-9a-f]{32}(?:[0-9a-f]{32})?\.(jpg|jpeg|png|webp)$", re.IGNORECASE)


def doc_image_dir(doc_id) -> Path:
    d = Path(settings.DATA_DIR) / "doc_images" / str(doc_id)
    return d


def save_doc_images(doc_id, images: dict[str, str]) -> int:
    """把 MinerU 返回的 base64 图片落盘。返回新写入的数量（已存在则跳过）。"""
    if not images:
        return 0
    d = doc_image_dir(doc_id)
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for name, data_url in images.items():
        if not _DOC_IMG_NAME_RE.fullmatch(name):
            continue  # 文件名不是内容哈希格式，防路径注入
        b64 = (data_url or "").split(",", 1)[-1]
        try:
            raw = _base64.b64decode(b64)
        except Exception:
            continue
        out = d / name
        if out.exists() and out.stat().st_size == len(raw):
            continue
        out.write_bytes(raw)
        n += 1
    return n


def rewrite_img_srcs(html: str, doc_id) -> str:
    """把 <img> 的相对图片引用 images/x.jpg 重写为受权限保护的视图 URL，
    并注入 loading=lazy + decoding=async（带图文档整页可达几十 MB，懒加载
    只拉视口内的）。幂等：绝对 src 不再重写、已带 loading 的不重复注入。"""
    if not html:
        return html
    prefix = f"/kb/doc/{doc_id}/img/"

    def _md_repl(m):
        return m.group(1) + prefix + m.group(2) + m.group(3)

    html = re.sub(r"(!\[[^\]]*\]\()images/([0-9a-f]+\.(?:jpg|jpeg|png|webp))(\))",
                  _md_repl, html, flags=re.IGNORECASE)

    def _img_tag_repl(m):
        tag = m.group(0)
        # 相对 src → 受权限保护的视图 URL（兼容单/双引号）
        tag = re.sub(
            r'src=(["\'])images/([0-9a-f]+\.(?:jpg|jpeg|png|webp))\1',
            lambda s: f"src={s.group(1)}{prefix}{s.group(2)}{s.group(1)}",
            tag, flags=re.IGNORECASE)
        if "loading=" not in tag:
            tag = tag[:-1].rstrip() + ' loading="lazy" decoding="async">'
        return tag

    return re.sub(r"<img\b[^>]*>", _img_tag_repl, html, flags=re.IGNORECASE)


# ------------------------------------------------------------------
# 嵌入前清洗：把 MinerU 的原始 HTML（表格等）转成结构化纯文本。
# 仅用于嵌入路径（run_indexing）；查看页（md_to_html）仍用原始 md_content。
# ------------------------------------------------------------------
# HTML cell parsing is delegated to table_structure; tag syntax may vary.
_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.DOTALL | re.IGNORECASE)
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
    from .table_structure import table_text
    return table_text(table_html)


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
    input_version = _embed_input_version("text")
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
                metadata={
                    "source": source_name, "section": section,
                    "embedding_input_version": input_version,
                },
            ))
    return out


# ------------------------------------------------------------------
# Embedding（复用父项目客户端）
# ------------------------------------------------------------------
def is_wemm(model: str) -> bool:
    """模型名含 wemm → 走自托管 WeMM 服务的 /embed 协议（非 OpenAI 兼容）。"""
    return "wemm" in (model or "").lower()


# WeMM 官方评测口径：查询侧加 Instruct 前缀。但 A/B 实测（2026-09-07，
# 三个带图库 × 7 组中英查询）裸查询余弦距离全部更优（好 0.08~0.13），
# 故默认裸查询；设 WEMM_QUERY_INSTRUCT=1 可启用前缀做后续实验
# （如换中文 instruction 文案）。
_WEMM_QUERY_INSTRUCT = (
    "Given a user question, retrieve relevant passages and figures "
    "from the internal knowledge base"
)


def _embed_input_version(modality: str) -> str:
    """语料块嵌入时的输入格式版本，随 chunk metadata 入库（Indexed 同款做法）。
    查询侧格式调整不作数；此版本变化才意味着旧语料向量需要重嵌。
    modality: text=纯文本块，media=图文交错块（图片）。
    """
    model = embedding_settings()["model"]
    return f"{'wemm' if is_wemm(model) else 'openai'}-{modality}-user-v1"


class WeMMEmbeddings:
    """自托管 WeMM 向量化服务客户端（实现 langchain Embeddings 接口）。

    协议：POST {base_url}/embed  {"inputs": [{"text": ...}], "dimension": n}
          → {"embeddings": [[...], ...]}
    服务空闲 10 分钟会自动卸载模型，下一个请求现场冷加载（20s+），
    因此超时给足；批量压到 32 条/请求避免单次推理过久。
    """

    _BATCH = 32
    _TIMEOUT = 180

    def __init__(self, base_url: str, dimensions: int | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.dimensions = dimensions or None

    def _embed_raw(self, items: list[dict]) -> list[list[float]]:
        """按原生 EmbedItem 列表批量嵌入（text/image_b64/... 任选）。"""
        out: list[list[float]] = []
        for i in range(0, len(items), self._BATCH):
            batch = items[i:i + self._BATCH]
            payload: dict[str, Any] = {"inputs": batch}
            if self.dimensions:
                payload["dimension"] = self.dimensions
            r = httpx.post(self.base_url + "/embed", json=payload, timeout=self._TIMEOUT)
            r.raise_for_status()
            vecs = (r.json() or {}).get("embeddings")
            if not vecs or len(vecs) != len(batch):
                raise ValueError(
                    f"WeMM 返回向量数不匹配（{len(vecs or [])}/{len(batch)}），"
                    f"端点：{self.base_url}")
            out.extend(vecs)
        return out

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return self._embed_raw([{"text": t} for t in texts])

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(list(texts))

    def embed_query(self, text: str) -> list[float]:
        prompt = text
        if os.environ.get("WEMM_QUERY_INSTRUCT", "0").strip().lower() in ("1", "true", "on", "yes"):
            prompt = f"Instruct: {_WEMM_QUERY_INSTRUCT}\nQuery: {text}"
        return self._embed([prompt])[0]

    def embed_contents(self, inputs: list[list[dict]]) -> list[list[float]]:
        """多模态嵌入：每个 input 是原生 content 列表（图文交错）。

        例: [{"type": "text", "text": "轨道布置图"},
             {"type": "image", "image": "data:image/jpeg;base64,..."}]
        与 embed_documents 同批量/超时策略；服务端 content 格式不兼容时抛错，
        由调用方决定降级（image_b64 单图 或 跳过图片块）。
        """
        out: list[list[float]] = []
        for i in range(0, len(inputs), self._BATCH):
            batch = inputs[i:i + self._BATCH]
            payload: dict[str, Any] = {"inputs": [{"content": c} for c in batch]}
            if self.dimensions:
                payload["dimension"] = self.dimensions
            r = httpx.post(self.base_url + "/embed", json=payload, timeout=self._TIMEOUT)
            r.raise_for_status()
            vecs = (r.json() or {}).get("embeddings")
            if not vecs or len(vecs) != len(batch):
                raise ValueError(
                    f"WeMM 返回向量数不匹配（{len(vecs or [])}/{len(batch)}），"
                    f"端点：{self.base_url}")
            out.extend(vecs)
        return out


def _embeddings():
    e = embedding_settings()
    if is_wemm(e["model"]) and not e["base_url"].rstrip("/").endswith("/v1"):
        if not e["base_url"]:
            raise ValueError("WeMM 向量服务未配置 Base URL。")
        return WeMMEmbeddings(base_url=e["base_url"], dimensions=e["dimensions"])

    from langchain_openai import OpenAIEmbeddings

    # Batch capacity is a service contract, not a property of a loopback URL.
    batch_size = int(getattr(settings, 'EMBEDDING_BATCH_SIZE', 16))
    if not 1 <= batch_size <= 32:
        raise ValueError('EMBEDDING_BATCH_SIZE 必须在 1–32 范围内')
    return OpenAIEmbeddings(
        model=e["model"],
        dimensions=e["dimensions"],
        # Ollama's OpenAI-compatible API does not need authentication, but the
        # OpenAI SDK still requires a non-empty value when constructing it.
        api_key=e["api_key"] or "local-no-key",
        base_url=e["base_url"],
        check_embedding_ctx_length=False,
        chunk_size=batch_size,
        request_timeout=45,
        max_retries=0,
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


_IMG_MIN_BYTES = 3072  # 小于 3KB 的多为页眉 logo/装饰线，不入多模态索引


_IMG_CTX_LINES = 4      # caption 缺失时向上下行取上下文的窗口
_IMG_CTX_MAX_CHARS = 300


def _ctx_valid(s: str) -> bool:
    """上下文/caption 是否有语义锚定价值：CAD 图纸页的邻近文本常是零散
    单元格数字（"5 1 4"），无锚定价值反而稀释嵌入。"""
    return len(re.findall(r"[\u4e00-\u9fff]|[A-Za-z]{2,}", s or "")) >= 3


def _image_ctx_text(lines: list[str], idx: int) -> str:
    """图片引用行 caption 为空时，取前后邻近的文本行做语义锚定。

    图纸类 PDF 的插图常独占一行（行内无图注），但图名/标题栏文字就在
    上下几行（MinerU 把图注渲染为图片上/下方段落）。无文本锚定的裸图
    嵌入与文本查询距离实测 1.3+，带锚定 0.6 左右——caption 质量直接
    决定图片可检索性。跳过标题行（已由 section 承载）与其它图片引用行。
    """
    picked: list[str] = []

    def _clean(s: str) -> str:
        s = re.sub(r"<[^>]+>", " ", s)  # HTML 标签整段剥掉（td/rowspan 等是噪声）
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)|<img\s[^>]*/?>|images/[0-9a-f]+\.(?:jpg|jpeg|png|webp)",
                   "", s, flags=re.IGNORECASE)
        return re.sub(r"[!*\[\]()<>#|]", " ", s).strip()

    for span in (range(idx - 1, max(-1, idx - 1 - _IMG_CTX_LINES), -1),
                 range(idx + 1, min(len(lines), idx + 1 + _IMG_CTX_LINES))):
        for j in span:
            line = lines[j]
            if "images/" in line and re.search(r"!\[|<img", line, re.IGNORECASE):
                continue  # 相邻图片行（并排小图）不作上下文
            if _SECTION_HEADING.match(line.lstrip()):
                continue
            txt = _clean(line)
            if len(txt) < 2:
                continue
            picked.append(txt)
            if sum(len(p) for p in picked) >= _IMG_CTX_MAX_CHARS:
                return " ".join(picked)[:_IMG_CTX_MAX_CHARS]
    ctx = " ".join(picked)[:_IMG_CTX_MAX_CHARS].strip()
    return ctx if _ctx_valid(ctx) else ""


def _image_chunks(md_content: str, source_name: str, doc_id,
                  content_list: list | None = None) -> list[tuple[Any, Path]]:
    """提取文档图片 → (chunk, 图片文件路径) 列表。

    图片来源 = md 引用 ∪ content_list 的 image/chart 条目：MinerU 对表格
    截图/图例类插图只在 content_list 给 img_path（md 里用 HTML 表格代替，
    不放 ![]() 引用）——只扫 md 会漏掉这类图。
    caption 优先级：md 行内 → content_list 的 image_caption（图内标题，
    OCR 出的真图注）→ 图片行前后邻近文本 → 章节名 →「文档插图」。
    仅返回磁盘上确实存在且 ≥ _IMG_MIN_BYTES 的图片。
    """
    d = doc_image_dir(doc_id)
    out: list[tuple[Any, Path]] = []
    if not d.is_dir():
        return out
    section = ""
    seen: set[str] = set()
    input_version = _embed_input_version("media")
    lines = (md_content or "").splitlines()

    def _emit(name: str, caption: str, sec: str) -> None:
        if name in seen:
            return
        seen.add(name)
        p = d / name
        try:
            if not p.is_file() or p.stat().st_size < _IMG_MIN_BYTES:
                return
        except OSError:
            return
        ctx = f"【{sec}】" if sec else ""
        ctx = (ctx + (caption or sec or "文档插图")).strip()
        chunk = LCDocument(page_content=ctx, metadata={
            "source": source_name, "section": sec,
            "type": "image", "image": name,
            "embedding_input_version": input_version,
        })
        out.append((chunk, p))

    for i, line in enumerate(lines):
        head = _SECTION_HEADING.match(line.lstrip())
        if head:
            section = _clean_section_name(head.group(1))
        for m in re.finditer(r"images/([0-9a-f]+\.(?:jpg|jpeg|png|webp))", line, re.IGNORECASE):
            caption = re.sub(
                r"!\[[^\]]*\]\([^)]*\)|<img\s[^>]*/?>|images/[0-9a-f]+\.(?:jpg|jpeg|png|webp)",
                "", line, flags=re.IGNORECASE,
            )
            caption = re.sub(r"[!*\[\]()<>#]", "", caption).strip()[:120]
            if not _ctx_valid(caption):
                caption = _image_ctx_text(lines, i)
            _emit(m.group(1), caption[:_IMG_CTX_MAX_CHARS], section)

    # content_list 补充：md 未引用的图（表格截图/图例），caption 用图内标题
    if content_list:
        for block in content_list:
            if not isinstance(block, dict) or block.get("type") not in ("image", "chart"):
                continue
            img = block.get("img_path") or ""
            name = img.rsplit("/", 1)[-1]
            if not name:
                continue
            cap = " ".join(
                str(c) for c in (block.get("image_caption")
                                 or block.get("chart_caption") or []) if str(c).strip()
            ).strip()[:_IMG_CTX_MAX_CHARS]
            if not _ctx_valid(cap):
                cap = ""  # 图内标题是零散数字（CAD 图纸）时无锚定价值
            _emit(name, cap, section)
    return out


def _index_image_chunks(vs, img_items: list[tuple[Any, Path]], kb_slug: str,
                        source_name: str, doc_id=None) -> int:
    """把图片块用 WeMM 多模态嵌入（图 + 上下文）写入 Chroma。

    优先 content 图文交错编码；服务端不认该格式则退回 image_b64 单图编码；
    任何失败只记录告警并跳过图片块 —— 文本块已入库，检索不降级。
    id 带 doc_id 前缀：独立库多文档共用同一张图（同 sha256）时避免撞 id
    被 chromadb 静默丢弃、引用张冠李戴。
    """
    if not img_items:
        return 0
    log = logging.getLogger(__name__)
    ef = _embeddings()
    if not isinstance(ef, WeMMEmbeddings):
        log.info("当前 embedding 非多模态（%s），跳过 %d 个图片块 kb=%s",
                 type(ef).__name__, len(img_items), kb_slug)
        return 0
    import base64 as b64mod

    def _data_url(p: Path) -> str:
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        return f"data:{mime};base64," + b64mod.b64encode(p.read_bytes()).decode()

    def _raw_b64(p: Path) -> str:
        # image_b64 字段只收裸 base64（服务端拒绝 data URL：Only base64 data is allowed，
        # 已对 192.168.1.10:8300 实测确认）
        return b64mod.b64encode(p.read_bytes()).decode()

    inputs: list[list[dict]] = []
    for chunk, p in img_items:
        inputs.append([
            {"type": "text", "text": chunk.page_content},
            {"type": "image", "image": _data_url(p)},
        ])
    try:
        vecs = ef.embed_contents(inputs)
    except Exception as e:
        log.warning("WeMM 图文交错嵌入失败（%s），退回单图嵌入", str(e)[:120])
        try:
            vecs = ef._embed_raw([{"image_b64": _raw_b64(p)} for _c, p in img_items])
        except Exception as e2:
            log.warning("WeMM 图片嵌入不可用，跳过 %d 个图片块 kb=%s: %s",
                        len(img_items), kb_slug, str(e2)[:120])
            return 0
    try:
        col = vs._collection  # langchain_chroma 暴露的原生 collection
        col.add(
            ids=[f"img-{doc_id}-{m['image']}" for _c, m in
                 ((c, c.metadata) for c, _p in img_items)],
            embeddings=vecs,
            documents=[c.page_content for c, _p in img_items],
            metadatas=[c.metadata for c, _p in img_items],
        )
        return len(img_items)
    except Exception:
        log.exception("图片块写入 Chroma 失败 kb=%s source=%s", kb_slug, source_name)
        return 0


# ------------------------------------------------------------------
# 视觉文档模式：整页渲染 + 页文本锚定的多模态块（图纸/扫描件用）
# ------------------------------------------------------------------
# 渲染边长上限：Qwen 系视觉 patch=16，1024 边 ≈ 64×88 个 patch ≈ 5.6k 视觉
# token——超出会挤爆嵌入上下文，再大也会被服务端缩回
_PAGE_RENDER_MAX_SIDE = 1024
_PAGE_TEXT_CHARS = 400        # 页面文本锚定截断（够语义锚定，不撑爆输入）
_PAGE_EMBED_BATCH = 4        # 页面图 base64 大，批量压小避免单请求过大


def _render_page_jpeg(pdf_path: Path, page_no: int,
                      max_side: int = _PAGE_RENDER_MAX_SIDE) -> bytes | None:
    """渲染 PDF 某页为 JPEG（等比缩到 max_side 内）。失败返回 None。"""
    try:
        import pymupdf
        with pymupdf.open(pdf_path) as pdf:
            if page_no < 0 or page_no >= pdf.page_count:
                return None
            page = pdf[page_no]
            r = page.rect
            scale = max_side / max(r.width, r.height) if max(r.width, r.height) > 0 else 1.0
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            return pix.tobytes("jpeg") if hasattr(pix, "tobytes") else pix.tobytes("jpg")
    except Exception:
        logging.getLogger(__name__).warning(
            "页面渲染失败（跳过该页块）page=%s", page_no, exc_info=True)
        return None


def page_texts(content_list: list | None) -> dict[int, str]:
    """content_list → {页码: 页面文本}。

    表格块用与文本块嵌入同一套 _html_table_to_text 渲染（保证锚定文本与
    文本块可互相召回）；跳过页眉/页脚。每页截断到 _PAGE_TEXT_CHARS。
    """
    if not content_list:
        return {}
    out: dict[int, list[str]] = {}
    for block in content_list:
        if not isinstance(block, dict):
            continue
        btype = block.get("type") or ""
        page = block.get("page_idx")
        if not isinstance(page, int) or btype in ("header", "footer", "page_number"):
            continue
        if btype == "table":
            text = _html_table_to_text(block.get("table_body") or "")[:200]
        elif btype in ("image", "chart"):
            caps = block.get("image_caption") or block.get("chart_caption") or []
            text = " ".join(str(c) for c in caps if str(c).strip())[:120]
        else:
            text = str(block.get("text") or "").strip()[:200]
        if text:
            out.setdefault(page, []).append(text)
    return {p: (" ".join(parts))[:_PAGE_TEXT_CHARS] for p, parts in out.items()}


def _page_chunks(source_name: str, content_list: list) -> list[tuple[Any, int]]:
    """整页块：page_content = 【第N页】+ 页面文本（锚定），metadata 带
    type=image（图片召回道可召回）与 page=N（消费端区分页面块与插图块）。"""
    from langchain_core.documents import Document as _Doc
    input_version = _embed_input_version("media")
    out: list[tuple[Any, int]] = []
    for page, text in sorted(page_texts(content_list).items()):
        content = f"【第{page + 1}页】{text or '页面图像'}"
        out.append((_Doc(page_content=content, metadata={
            "source": source_name, "section": f"第{page + 1}页",
            "type": "image", "image": f"page{page}", "page": page,
            "embedding_input_version": input_version,
        }), page))
    return out


def _index_page_chunks(vs, doc, kb_slug: str, content_list: list) -> int:
    """整页块入库：渲染 → WeMM content 图文交错嵌入 → 写 Chroma + 溯源行。

    chunk_id 确定性（page-<doc_id>-<N>），溯源行 blocks 不带 bbox（证据面板
    只渲染整页不画框）。任何失败只跳过页面块，不影响文本/插图块。
    """
    import base64 as b64mod
    from .models import ChunkProvenance

    log = logging.getLogger(__name__)
    try:
        pdf_path = Path(doc.file.path)
    except (NotImplementedError, ValueError, AttributeError):
        return 0
    items = _page_chunks(doc.original_name, content_list)
    if not items:
        return 0
    ef = _embeddings()
    if not isinstance(ef, WeMMEmbeddings):
        log.info("当前 embedding 非多模态（%s），跳过 %d 个整页块 kb=%s",
                 type(ef).__name__, len(items), kb_slug)
        return 0

    def _data_url(page_no: int) -> str | None:
        raw = _render_page_jpeg(pdf_path, page_no)
        if not raw:
            return None
        return "data:image/jpeg;base64," + b64mod.b64encode(raw).decode()

    ids, vecs, docs, metas, prov_rows = [], [], [], [], []
    for i in range(0, len(items), _PAGE_EMBED_BATCH):
        batch = items[i:i + _PAGE_EMBED_BATCH]
        urls = [(_data_url(p), c, p) for c, p in batch]
        usable = [(u, c, p) for u, c, p in urls if u]
        if not usable:
            continue
        try:
            batch_vecs = ef.embed_contents([
                [{"type": "text", "text": c.page_content},
                 {"type": "image", "image": u}]
                for u, c, _p in usable
            ])
        except Exception as e:
            log.warning("整页块嵌入失败（本批跳过 %d 页）: %s", len(usable), str(e)[:120])
            continue
        for (u, c, p), v in zip(usable, batch_vecs):
            ids.append(f"page-{doc.id}-{p}")
            vecs.append(v)
            docs.append(c.page_content)
            metas.append(c.metadata)
            prov_rows.append(ChunkProvenance(
                chunk_id=f"page-{doc.id}-{p}", document=doc, kb_slug=kb_slug,
                page_start=p, page_end=p,
                blocks=[{"page": p, "bbox": None, "kind": "page"}],
            ))
    if not ids:
        return 0
    try:
        vs._collection.upsert(ids=ids, embeddings=vecs, documents=docs,  # noqa: SLF001
                               metadatas=metas)
        ChunkProvenance.objects.bulk_create(prov_rows, batch_size=500,
                                            ignore_conflicts=True)
        return len(ids)
    except Exception:
        log.exception("整页块写入失败 kb=%s doc=%s", kb_slug, doc.id)
        return 0


def _remove_page_chunks(kb_slug: str, doc_id) -> int:
    """删除某文档的全部整页块（关闭视觉文档模式时）。返回删除向量数。"""
    import chromadb
    from chromadb.config import Settings as CBSettings
    from .models import ChunkProvenance

    n = ChunkProvenance.objects.filter(document_id=doc_id,
                                       chunk_id__startswith=f"page-{doc_id}-").delete()[0]
    try:
        client = chromadb.PersistentClient(
            path=str(_kb_persist_dir(kb_slug)),
            settings=CBSettings(anonymized_telemetry=False))
        col = client.get_collection(_chroma_collection_name(kb_slug))
        col.delete(where={"$and": [{"type": "image"},
                                   {"source": {"$exists": True}},
                                   {"page": {"$gte": 0}}]})
    except Exception:
        logging.getLogger(__name__).warning(
            "整页块向量删除失败（溯源已清）kb=%s doc=%s", kb_slug, doc_id,
            exc_info=True)
    return n


def run_indexing(md_content: str, kb_slug: str, source_name: str, doc_id=None,
                 content_list=None) -> int:
    """切块 + 向量化 → 写入该 KB 的 Chroma。返回 chunk 数。

    嵌入前先清洗：把 MinerU 的原始 HTML 表格转成结构化纯文本（仅此路径清洗；
    查看页 md_to_html 仍用原始 md_content 渲染真表格）。
    doc_id 提供时，PDF 图片作为独立多模态块入同一向量库（需 WeMM），
    且 content_list（MinerU 版面条目）提供时为每个 chunk 写页码/坐标溯源
    （ChunkProvenance，证据面板与「第 N 页」引用的数据源）。
    """
    clean_md = _md_for_embedding(md_content)
    if md_content.strip() and not clean_md.strip():
        raise ValueError("解析内容清洗后为空，停止索引，请核对原文")
    chunks = _section_aware_chunk(clean_md, source_name)
    if not chunks and not doc_id:
        return 0
    vs = Chroma(
        collection_name=_chroma_collection_name(kb_slug),
        embedding_function=_embeddings(),
        persist_directory=str(_kb_persist_dir(kb_slug)),
    )
    chunk_ids: list[str] = []
    if chunks:
        # add_documents 返回每个 chunk 的向量 id —— 溯源表与关键词索引都以
        # id 关联（重跑入库 id 全换，溯源表先清后写）
        chunk_ids = list(vs.add_documents(chunks))
    # 混合检索：向量入库成功后同步写关键词索引（失败只降级，不阻断）
    from . import keyword_index
    if chunks:
        keyword_index.add_chunks(kb_slug, chunks, ids=chunk_ids)
    n_img = 0
    img_names: list[str] = []
    if doc_id:
        try:
            from .provenance import parse_content_list as _parse_cl
            cl = _parse_cl(content_list) if content_list is not None else None
            img_items = _image_chunks(md_content, source_name, doc_id,
                                      content_list=cl)
            n_img = _index_image_chunks(vs, img_items, kb_slug, source_name,
                                        doc_id=doc_id)
            img_names = [c.metadata.get("image", "") for c, _p in img_items]
        except Exception:
            logging.getLogger(__name__).exception(
                "图片块索引失败（不影响文本块）doc=%s", doc_id)
        # chunk 溯源（页码 + 版面 bbox）：content_list 为空 → 清掉旧行即可
        # （无溯源，引用退化到原文切片页）
        if doc_id:
            try:
                from . import provenance as _prov
                _prov.persist_content_list(doc_id, content_list)
                if content_list:
                    from .models import Document
                    _doc = Document.objects.get(id=doc_id)
                    _prov.save_provenance(
                        _doc, kb_slug, chunk_ids, chunks, clean_md,
                        content_list, image_names=img_names)
                else:
                    from .models import ChunkProvenance
                    ChunkProvenance.objects.filter(document_id=doc_id).delete()
                # 视觉文档模式（用户按文档勾选，图纸/扫描件用）：整页渲染 +
                # 页文本锚定的多模态块——文本查询可命中纯 CAD 图纸页
                if cl:
                    from .models import Document
                    _doc = Document.objects.get(id=doc_id)
                    if getattr(_doc, "page_embed", False) and _doc.file_type == "pdf":
                        try:
                            n_img += _index_page_chunks(vs, _doc, kb_slug, cl)
                        except Exception:
                            logging.getLogger(__name__).exception(
                                "整页块索引失败（不影响文本块）doc=%s", doc_id)
            except Exception:
                logging.getLogger(__name__).exception(
                    "chunk 溯源写入失败（不影响向量入库）doc=%s", doc_id)
    _stamp_embedding_model(kb_slug)
    return len(chunks) + n_img


def _stamp_embedding_model(kb_slug: str) -> None:
    """向量化成功后在 KB 上记录所用 embedding 模型（管理页徽章 / 判断是否需重建）。"""
    from .models import KnowledgeBase

    e = embedding_settings()
    KnowledgeBase.objects.filter(slug=kb_slug).update(
        embedding_model=e["model"] or "",
        embedding_dimensions=e["dimensions"],
    )


# ------------------------------------------------------------------
# 完整流水线（异步执行）
# ------------------------------------------------------------------
def build_doc_html(doc) -> None:
    """根据 doc.md_content 生成 HTML 正文并存库（供文档查看页使用）。

    幂等：每次用最新 md_content 重建。供流水线、回填命令、查看页懒构建复用。
    """
    from django.utils import timezone
    from .md2html import md_to_html as _md_to_html

    doc.html_content = rewrite_img_srcs(_md_to_html(doc.md_content or ""), doc.id)
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

        # 阶段 1: OCR（PDF 同时取回图片并落盘）
        doc.status = Document.Status.OCR
        doc.stage_detail = "开始 OCR…"
        doc.save(update_fields=["status", "stage_detail", "updated_at"])
        md, images, content_list = run_ocr_with_images(file_path, doc.file_type, on_progress=update_stage)
        # 在模型调用前保存版面数据，嵌入失败后仍可恢复原文定位。
        from .provenance import persist_content_list
        persist_content_list(doc.id, content_list)
        try:
            n_img = save_doc_images(doc.id, images)
        except Exception:
            logging.getLogger(__name__).exception("图片落盘失败（不影响文本入库）doc=%s", doc_id)
            n_img = 0
        doc.md_content = md
        doc.html_content = rewrite_img_srcs(md_to_html(md), doc.id)
        from django.utils import timezone
        doc.html_built_at = timezone.now()
        doc.save(update_fields=["md_content", "html_content", "html_built_at", "updated_at"])

        # 阶段 2: 向量化（文本块 + WeMM 多模态图片块）
        doc.status = Document.Status.INDEXING
        doc.stage_detail = "正在切块 + 向量化…"
        doc.save(update_fields=["status", "stage_detail", "updated_at"])
        n_chunks = run_indexing(md, doc.kb.slug, doc.original_name, doc_id=doc.id,
                                content_list=content_list)
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

        # 追踪表：库（或其父文件夹）启用了追踪 → 后台抽取关键信息登记
        try:
            from . import tracker as _tracker
            _tracker.run_extraction_async(doc.id)
        except Exception:
            logging.getLogger(__name__).exception(
                "追踪表抽取调度失败（不影响文档入库）doc=%s", doc_id)

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


def _page_embed_async(doc_id: str) -> None:
    """后台补建某文档的整页视觉块（管理页开启视觉模式时触发）。"""
    def _run():
        import django
        django.setup()
        from .models import Document
        from .provenance import load_content_list
        try:
            doc = Document.objects.get(id=doc_id)
            if not doc.page_embed or doc.file_type != "pdf":
                return
            cl = load_content_list(doc.id)
            if not cl:
                doc.page_embed = False
                doc.save(update_fields=["page_embed", "updated_at"])
                logging.getLogger(__name__).warning(
                    "视觉模式需版面数据（content_list）——旧入库文档请重新上传，doc=%s", doc_id)
                return
            vs = Chroma(
                collection_name=_chroma_collection_name(doc.kb.slug),
                embedding_function=_embeddings(),
                persist_directory=str(_kb_persist_dir(doc.kb.slug)),
            )
            n = _index_page_chunks(vs, doc, doc.kb.slug, cl)
            if n:
                doc.chunk_count = doc.chunk_count + n
                doc.save(update_fields=["chunk_count", "updated_at"])
                kb = doc.kb
                kb.chunk_count = sum(
                    d.chunk_count for d in kb.documents.filter(status=Document.Status.COMPLETED))
                kb.save(update_fields=["chunk_count", "updated_at"])
        except Exception:
            logging.getLogger(__name__).exception("视觉模式页面向量生成失败 doc=%s", doc_id)

    threading.Thread(target=_run, daemon=True).start()


def process_document_async(doc_id: str) -> None:
    """在后台 daemon 线程中启动流水线。"""
    t = threading.Thread(target=process_document, args=(str(doc_id),), daemon=True)
    t.start()
