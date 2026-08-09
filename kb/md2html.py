"""零依赖 Markdown → HTML 正文转换器。

MinerU 输出的「Markdown」其实是 HTML 超集：表格、图片、折叠块已经是
原始 HTML（`<table>` / `<img>` / `<details>`）。因此本转换器只把 Markdown
语法（标题、列表、段落、行内标记）转成 HTML，**原始 HTML 块原样透传**，
不引入任何第三方库。

输出是「正文片段」（无 <html>/<body> 包裹），由查看页模板负责整体布局。
"""
from __future__ import annotations

import html as _html
import re

# 原样透传的块级 HTML 起始标签（行首匹配，大小写不敏感）。
# MinerU 常见：<table>、<img、<details>、<figure>、<br>、<hr>、<details。
_RAW_HTML_RE = re.compile(
    r"^\s*<(/?)\s*(table|thead|tbody|tr|td|th|img|details|summary|figure|"
    r"figcaption|br|hr|blockquote|div|p|h[1-6]|pre|code|ul|ol|li)\b",
    re.IGNORECASE,
)

# 行内标记
_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_INLINE_ITALIC = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
# ![alt](src) 图片：Markdown 语法的图片也支持
_INLINE_IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_OLIST_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$")
_ULIST_RE = re.compile(r"^\s*[-*+]\s+(.*)$")


def _inline(text: str) -> str:
    """行内标记转换：图片、链接、粗体、斜体、行内代码。

    在普通文本行上调用；原始 HTML 块不走这里。
    """
    # 先处理图片（含 ! 前缀，避免被链接规则误吃）
    text = _INLINE_IMG.sub(r'<img alt="\1" src="\2">', text)
    text = _INLINE_LINK.sub(r'<a href="\2">\1</a>', text)
    text = _INLINE_BOLD.sub(r"<strong>\1</strong>", text)
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _INLINE_ITALIC.sub(r"<em>\1</em>", text)
    return text


def md_to_html(md: str) -> str:
    """把 MinerU Markdown 转成 HTML 正文片段。"""
    if not md:
        return ""
    lines = md.split("\n")
    out: list[str] = []

    list_type: str | None = None     # "ul" | "ol" | None：当前是否在列表中
    para: list[str] = []             # 当前段落的累积行

    def flush_para():
        nonlocal para
        if para:
            body = " ".join(para).strip()
            if body:
                out.append(f"<p>{_inline(body)}</p>")
            para = []

    def close_list():
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    in_code_fence = False
    code_buf: list[str] = []

    for raw in lines:
        # ---- 代码围栏 ``` ----
        if raw.strip().startswith("```"):
            if in_code_fence:
                code = _html.escape("\n".join(code_buf))
                out.append(f"<pre><code>{code}</code></pre>")
                code_buf = []
                in_code_fence = False
            else:
                flush_para()
                close_list()
                in_code_fence = True
            continue
        if in_code_fence:
            code_buf.append(raw)
            continue

        # ---- 原始 HTML 块：原样透传 ----
        if _RAW_HTML_RE.match(raw):
            flush_para()
            close_list()
            out.append(raw)
            continue

        # ---- 空行：段落/列表边界 ----
        if not raw.strip():
            flush_para()
            close_list()
            continue

        # ---- 标题 ----
        m = _HEADING_RE.match(raw)
        if m:
            flush_para()
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            continue

        # ---- 有序列表 ----
        m = _OLIST_RE.match(raw)
        if m:
            flush_para()
            if list_type != "ol":
                close_list()
                out.append("<ol>")
                list_type = "ol"
            out.append(f"<li>{_inline(m.group(2).strip())}</li>")
            continue

        # ---- 无序列表 ----
        m = _ULIST_RE.match(raw)
        if m:
            flush_para()
            if list_type != "ul":
                close_list()
                out.append("<ul>")
                list_type = "ul"
            out.append(f"<li>{_inline(m.group(1).strip())}</li>")
            continue

        # ---- 普通文本行 → 累积成段落 ----
        close_list()
        para.append(raw.strip())

    # 收尾
    if in_code_fence and code_buf:
        code = _html.escape("\n".join(code_buf))
        out.append(f"<pre><code>{code}</code></pre>")
    flush_para()
    close_list()

    return "\n".join(out)
