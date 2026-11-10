"""零依赖 Markdown → HTML 正文转换器。

MinerU 输出的「Markdown」其实是 HTML 超集：表格、图片、折叠块已经是
原始 HTML（`<table>` / `<img>` / `<details>`）。因此本转换器只把 Markdown
语法（标题、列表、段落、行内标记）转成 HTML，**原始 HTML 块经 nh3 白名单
消毒后透传**。

安全（XSS）：文档由用户上传、输出直接进模板 `|safe`，因此——
- 普通文本先整体 html.escape 再套行内 markdown 规则（<script> 变 &lt;script&gt;）；
- 透传的 HTML 走 nh3（Rust ammonia）白名单解析消毒：剥 on* 事件属性与
  一切非白名单标签/属性，URL 仅允许 http(s)/data。此前用正则剥 on* 可被
  `<img/src=x/onerror=...>`（斜杠分隔属性）和 `<img src="x"onerror=...>`
  （引号后紧跟）绕过，故必须真解析器；nh3 不可用时整体转义（保安全牺牲渲染）；
- markdown 链接/图片的 URL 同样走 scheme 校验。

输出是「正文片段」（无 <html>/<body> 包裹），由查看页模板负责整体布局。
"""
from __future__ import annotations

import html as _html
import re

try:
    import nh3
except ImportError:  # 依赖缺失时退化为整体转义（渲染降级，安全不降级）
    nh3 = None

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

# ---- 安全（XSS）：文档由用户上传，渲染进 |safe 前必须消毒 ----
# 危险 scheme 一律替换为 '#'（markdown 侧链接/图片用；原始 HTML 块由 nh3 处理）
_DANGEROUS_SCHEME_RE = re.compile(r"^\s*(javascript|vbscript|file)\s*:", re.IGNORECASE)

# nh3 白名单：与本转换器透传的块级 HTML 保持一致（_RAW_HTML_RE 的标签集）
_NH3_TAGS = {
    "table", "thead", "tbody", "tr", "td", "th", "img", "details", "summary",
    "figure", "figcaption", "br", "hr", "blockquote", "div", "p",
    "h1", "h2", "h3", "h4", "h5", "h6", "pre", "code", "ul", "ol", "li",
}
_NH3_ATTRS = {
    "*": {"colspan", "rowspan", "align", "width", "height"},
    "img": {"src", "alt", "loading", "decoding"},
    "details": {"open"},
}


def _safe_url(url: str) -> str:
    """URL 进属性前的 scheme 校验：危险协议替换为 '#'，其余原样。"""
    u = (url or "").strip()
    if _DANGEROUS_SCHEME_RE.match(u) or u.lower().startswith("data:text/"):
        return "#"
    return u


def _sanitize_raw_html(block: str) -> str:
    """白名单原始 HTML 块（<table>/<img>/<details> 等）消毒。

    nh3（Rust ammonia）按标签/属性白名单解析消毒：on* 事件属性、
    javascript: URL、白名单外的标签全部被剥掉。nh3 不可用时整体转义。
    """
    if nh3 is None:
        return _html.escape(block)
    return nh3.clean(
        block,
        tags=_NH3_TAGS,
        attributes=_NH3_ATTRS,
        url_schemes={"http", "https", "data"},
        strip_comments=True,
    )


def _inline(text: str) -> str:
    """行内标记转换：图片、链接、粗体、斜体、行内代码。

    在普通文本行上调用；原始 HTML 块不走这里。
    先整体 HTML 转义再套 markdown 规则（escape 只动 & < > "，
    不影响 * ` [ ] ( ) 标记语法），杜绝 <script> 直进 <p>。
    """
    escaped = []
    prefix = '\x00ESC'
    while prefix in text:
        prefix += 'E'
    def protect(match):
        escaped.append(match.group(1))
        return prefix + str(len(escaped) - 1) + '\x00'
    # Preserve literal source punctuation before applying Markdown patterns.
    # Restoring after conversion keeps escaped image/link syntax inert.
    text = re.sub(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\]\\^_`{|}~])', protect, text)
    text = _html.escape(text, quote=False)

    def restore(value):
        for index, char in enumerate(escaped):
            value = value.replace(prefix + str(index) + '\x00', char)
        return value

    def _img_repl(m):
        alt = _html.escape(_html.unescape(restore(m.group(1))), quote=True)
        src = _html.escape(_safe_url(_html.unescape(restore(m.group(2)))), quote=True)
        return f'<img alt="{alt}" src="{src}">'

    def _link_repl(m):
        href = _html.escape(_safe_url(_html.unescape(restore(m.group(2)))), quote=True)
        return f'<a href="{href}">{m.group(1)}</a>'  # 链接文字已随整体转义

    text = _INLINE_IMG.sub(_img_repl, text)
    text = _INLINE_LINK.sub(_link_repl, text)
    text = _INLINE_BOLD.sub(r"<strong>\1</strong>", text)
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _INLINE_ITALIC.sub(r"<em>\1</em>", text)
    for index, char in enumerate(escaped):
        # A protected quote may sit inside a generated href/src attribute.
        # Escape for both text and attribute contexts when restoring it.
        text = text.replace(prefix + str(index) + '\x00', _html.escape(char, quote=True))
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
    fence_marker = ''
    code_buf: list[str] = []

    for raw in lines:
        # A longer opening fence may safely contain shorter literal fences.
        fence = re.match(r'^\s*(`{3,}|~{3,})(.*)$', raw)
        if fence:
            marker, suffix = fence.groups()
            if in_code_fence and marker[0] == fence_marker[0] \
                    and len(marker) >= len(fence_marker) and not suffix.strip():
                code = _html.escape("\n".join(code_buf))
                out.append(f"<pre><code>{code}</code></pre>")
                code_buf = []
                in_code_fence = False
                fence_marker = ''
                continue
            elif not in_code_fence:
                flush_para()
                close_list()
                in_code_fence = True
                fence_marker = marker
                continue
        if in_code_fence:
            code_buf.append(raw)
            continue

        # ---- 原始 HTML 块：透传前消毒（剥 on* 事件属性 + URL scheme 校验） ----
        if _RAW_HTML_RE.match(raw):
            flush_para()
            close_list()
            out.append(_sanitize_raw_html(raw))
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
