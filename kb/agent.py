"""按指定 KB 构建 LangChain Agent 并流式问答。

每个请求根据选中的 KB 动态构建 agent（工具 = 该 KB 的检索），
与父项目 MedicalAgent 的 SSE 事件协议一致。
"""
from __future__ import annotations

import traceback
from functools import lru_cache
from pathlib import Path
from typing import AsyncGenerator

from django.conf import settings
from langchain_openai import ChatOpenAI

from .config import llm_settings, retrieval_settings

# SSE 事件类型（与前端约定）
SSE_REASONING = "reasoning"
SSE_TOOL_START = "tool_start"
SSE_TOOL_END = "tool_end"
SSE_TOKEN = "token"
SSE_ERROR = "error"
SSE_CODE_RUN = "code_run"  # 浏览器 Pyodide 沙箱执行的代码
SSE_USAGE = "usage"        # 本轮 token 用量统计
SSE_CITATIONS = "citations"
SSE_STEP = "step"          # 管线阶段（query_plan/hybrid_search/answer_generation/answer_verification）
SSE_VERIFY = "verify"      # 核实结论 {ok, issues}——不过则前端拒答展示
def _pick_anchor(clean_text: str) -> str:
    """从片段正文挑一个「查看页高亮锚点」。

    优先部件名/材质/中文短语（独特、能定位到具体行）；排除报告号/日期型
    「数字-字母-数字」串（如 25Y0457 全文出现几十次，落点会到首页而非目标处）。
    """
    import re as _re
    first_line = clean_text.split("\n", 1)[0].strip()
    cands = _re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{6,40}", first_line)
    for c in cands:
        if _re.fullmatch(r"\d+[A-Za-z]\d+[A-Za-z0-9]*", c):
            continue  # 报告号/日期型，太泛
        return c
    # 全部候选都被排除（整行都是报告号类 token）→ 退而求其次取行首
    return first_line[:30]


# ── 视觉模式：检索命中的图片以多模态内容块随工具结果发给模型 ──
# LangChain 工具返回 list[dict] 内容块（{"type":"image","base64":...}）时，
# 会被转成多模态 ToolMessage，模型下一轮直接「看到」图片本体。
_VISION_MAX_IMAGES = 4   # 每次检索最多附带张数（防 base64 撑爆上下文）
_VISION_MAX_SIDE = 1568  # 缩边上限（主流多模态输入档；与照片入库同规格）


def _image_content_block(doc_id: str, name: str) -> dict | None:
    """文档插图 → LangChain 图片内容块（base64；超边自动缩放重编码）。

    name 形如 page<N>（视觉文档模式的整页块）时按需渲染原 PDF 第 N 页。
    """
    import base64 as _b64
    import io as _io
    from .pipeline import doc_image_dir

    data: bytes | None = None
    mime = "image/jpeg"
    if name.startswith("page") and name[4:].isdigit():
        # 整页块：渲染原 PDF 页（页面渲染图不落盘，按需生成）
        try:
            from .models import Document
            from .pipeline import _render_page_jpeg
            doc = Document.objects.get(id=doc_id)
            raw = _render_page_jpeg(Path(doc.file.path), int(name[4:]))
            if raw:
                data, mime = raw, "image/jpeg"
        except Exception:
            data = None
    if data is None:
        p = doc_image_dir(doc_id) / name
        try:
            data = p.read_bytes()
        except OSError:
            return None
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    try:
        from PIL import Image
        with Image.open(_io.BytesIO(data)) as im:
            if im.width > _VISION_MAX_SIDE or im.height > _VISION_MAX_SIDE:
                im.thumbnail((_VISION_MAX_SIDE, _VISION_MAX_SIDE))
                buf = _io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=85)
                data, mime = buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 —— 解码失败用原图，交给服务端报错
        pass
    return {"type": "image",
            "base64": _b64.b64encode(data).decode(),
            "mime_type": mime}

  # 本轮检索的来源出处（含 doc_id/text，供前端渲染链接）

# 进程级持久化 checkpointer（AsyncSqliteSaver，data/checkpoints.sqlite3）。
# 同一 thread_id 跨请求/跨重启共享同一会话上下文。
_CHECKPOINTER = None
_CHECKPOINTER_LOCK = None


async def _get_checkpointer():
    """惰性创建并返回进程级 AsyncSqliteSaver（连接常驻整个进程生命周期）。"""
    global _CHECKPOINTER, _CHECKPOINTER_LOCK
    import asyncio as _asyncio

    if _CHECKPOINTER is not None:
        return _CHECKPOINTER
    if _CHECKPOINTER_LOCK is None:
        _CHECKPOINTER_LOCK = _asyncio.Lock()
    async with _CHECKPOINTER_LOCK:
        if _CHECKPOINTER is None:
            import aiosqlite
            from django.conf import settings as _settings
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            cp_path = _settings.DATA_DIR / "checkpoints.sqlite3"
            cp_path.parent.mkdir(parents=True, exist_ok=True)
            # 自行打开常驻连接（不在 async with 里，保持存活），交给 saver
            conn = await aiosqlite.connect(str(cp_path))
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            _CHECKPOINTER = saver
    return _CHECKPOINTER


def _get_llm(llm_cfg: dict) -> ChatOpenAI:
    return ChatOpenAI(
        model=llm_cfg["model"],
        # OpenAI's client requires a non-empty credential even when a local
        # OpenAI-compatible server (for example LM Studio) disables auth.
        api_key=llm_cfg["api_key"] or "local-no-key",
        base_url=llm_cfg["base_url"],
        temperature=llm_cfg["temperature"],
        # Ask OpenAI-compatible local servers (LM Studio included) to include
        # usage in the final streaming chunk, otherwise UI token counts stay 0.
        stream_usage=True,
        max_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
        extra_body=settings.LLM_EXTRA_BODY,
        timeout=180,
        max_retries=0,
    )


def _rows_label(prov_blocks) -> str:
    """溯源块里的表格行区间 → 人读标签（「表第3-9行」）；无行信息返回空。"""
    for b in prov_blocks or []:
        rs = b.get("rows") if isinstance(b, dict) else None
        if isinstance(b, dict) and b.get("kind") == "table" and isinstance(rs, list) and len(rs) == 2:
            return f"表第{rs[0]}行" if rs[0] == rs[1] else f"表第{rs[0]}-{rs[1]}行"
    return ""


def _kb_tree_text(dept_filter: dict) -> str:
    """list_knowledge_bases 工具的正文（提炼为模块函数便于测试）。

    「说明」= 管理员可选填写的描述：库级说明写库的主题，文档级说明写
    单份文档的内容——帮助 LLM 判断该检索哪个库/引用哪份文档。
    """
    from .models import KbTracker, KnowledgeBase
    lines = []

    # 追踪表信息：kb_id → 摘要（启用状态/字段数/登记条数）。字段名清单不在树里
    # 铺开（会膨胀）——模型用 tracker_lookup 留空查询即可见全部字段名。
    trk_info: dict = {}
    for tr in KbTracker.objects.select_related("kb"):
        n_rows = tr.rows.count()
        if not tr.fields and not n_rows:
            continue  # 空壳行（首次访问设置页时自动创建），树里不显示
        mark = "✓" if tr.enabled else "✗"
        fields_txt = f"{len(tr.fields)}字段" if tr.fields else "无字段"
        trk_info[tr.kb_id] = f"追踪表:{mark}{fields_txt}/{n_rows}条登记"

    def _trk(kb) -> str:
        t = trk_info.get(kb.id)
        return f"｜{t}" if t else ""

    def _desc(obj) -> str:
        d = (getattr(obj, "description", "") or "").strip()
        return f"｜说明: {d[:80]}" if d else ""

    def _doc_lines(kb, indent: str) -> list[str]:
        """文档级说明：每份文档一行（上限 4 份，超出提示）。"""
        docs = list(kb.documents.all())
        if not docs:
            return [f"{indent}（无文档）"]
        out = []
        for d in docs[:4]:
            desc = (d.description or "").strip()
            tag = f"｜{desc[:60]}" if desc else ""
            out.append(f"{indent}· {d.original_name[:48]}{tag}")
        if len(docs) > 4:
            out.append(f"{indent}…另有 {len(docs) - 4} 份文档")
        return out

    # 文件夹 + 其子文档库（部门过滤：子库部门与文件夹同步，故按文件夹过滤即可）
    folders = KnowledgeBase.objects.filter(is_folder=True, **dept_filter).order_by("name")
    for folder in folders:
        docs, chunks = folder.aggregate_counts()
        lines.append(f"📁 {folder.name}（文件夹, slug={folder.slug}, {chunks} 向量块）{_desc(folder)}{_trk(folder)}")
        for child in folder.children.filter(is_folder=False).order_by("name"):
            lines.append(f"  📄 {child.name}（slug={child.slug}, {child.chunk_count} 向量块）{_desc(child)}{_trk(child)}")
            lines.extend(_doc_lines(child, "     "))
    # 独立文档库（无父库的顶层文档库）
    standalone = KnowledgeBase.objects.filter(is_folder=False, parent__isnull=True, **dept_filter).order_by("name")
    for kb in standalone:
        lines.append(f"📄 {kb.name}（slug={kb.slug}, {kb.chunk_count} 向量块）{_desc(kb)}{_trk(kb)}")
        lines.extend(_doc_lines(kb, "   "))
    if not lines:
        return "（暂无知识库）"
    return ("可用知识库（📁=文件夹可跨文档检索，📄=文档库搜单份文档；"
            "「说明」为库/文档的内容描述，选库与引用来源时参考它判断相关性；"
            "「追踪表」=该库的结构化登记台账（✓启用/✗停用，字段数/登记条数），"
            "查登记信息用 tracker_lookup）：\n" + "\n".join(lines))


def _build_agent(kb_slug: str, thread_id: str, llm_cfg: dict, top_k: int, checkpointer,
                 citations: list | None = None, department: str = "",
                 evidence: list | None = None):
    """为指定 KB + thread 构建一个 create_agent。

    llm_cfg / top_k / department 由调用方在同步上下文中解析后传入，避免在 async
    上下文里访问数据库（Django 默认禁止）。

    department: 当前用户部门。空 = 不过滤（兼容旧调用）；非空则
    list_knowledge_bases 只列「通用 ∪ 该部门」的库，kb_search / kb_fetch_doc
    对不可访问的库返回不存在（不泄露存在性）。

    citations: 可选的可变列表；kb_search 每次检索会把来源出处追加进去，
    供调用方在流结束后发出 citations 事件。每轮应传入一个全新的空列表。

    evidence: 可选的可变列表；检索工具把带页码的证据片段（供答案核实步
    对照）追加进去。每轮传入全新空列表。
    """
    from langchain.agents import create_agent

    from .models import DEPARTMENT_GENERAL
    from .retriever import search

    kb_slug_default = kb_slug  # 避免在 kb_search 内部与参数名冲突
    cite_sink = citations if citations is not None else []
    evidence_sink = evidence if evidence is not None else []

    # 部门可见范围（空部门 = 不过滤，仅内部调试场景）
    if department:
        _dept_filter = {"department__in": [DEPARTMENT_GENERAL, department]}
    else:
        _dept_filter = {}

    def _dept_allowed(kb_obj) -> bool:
        if not department:
            return True
        return kb_obj.department in (DEPARTMENT_GENERAL, department)

    system_prompt = f"""你是知识库问答助手。

知识库结构（重要）：
知识库分两层：**文件夹**（含若干文档库）和**文档库**（每份文档独占一个向量库）。
- 用户提到具体文档名（如「P8」「Dumbo」）时，先调 list_knowledge_bases 查看有哪些文档库及其 slug，再用对应 slug 检索该文档库——这样只返回该文档的内容，不会混杂其它文档。
- 通用问题（不限定某份文档）时，可用文件夹 slug 检索，系统会跨该文件夹下所有文档库合并结果。
- 当前默认搜索范围是「{kb_slug}」（系统已按用户问题自动定位到名称最相关的文档库；问题明确指向某文档时不要再去明显无关的库检索）。
- 当前用户所在部门为「{department or DEPARTMENT_GENERAL}」；你能看到的仅是该部门与「通用」的知识库，这是正常的权限范围，不是知识库缺失——不要向用户提及其它部门库的存在。

工作准则：
1. **选库**：不确定有哪些文档库时调一次 list_knowledge_bases 查看层级与 slug，之后按需用 kb_slug 定位到具体文档库。不要每次都调。
2. **检索（两种工具，按需选择）**：
   - **kb_search**：按问题语义检索最相关的少数片段（指定 kb_slug 选库，不传则用默认范围）。适合「问某个点」「查某个指标」。单次问题内最多调 2 次。
   - **多模态检索**：kb_search 的结果里可能混有 `[图片]` 条目——这是从 PDF 提取的图纸/示意图/表格截图，其文本是图片所在章节与图注（OCR 片段）。用户问「图纸/布置图/示意图/长什么样」这类视觉问题时，这些命中很有价值：依据其上下文文字回答，并提及「相关图纸见回答下方引用区的图片」。图片会**自动**附在引用区供用户查看——不要在回答里拼贴图片地址或「查看: /kb/...」链接。**视觉模式开启时**，最相关的命中图片会以图片本体直接附在检索结果里——这时请依据图片内容本身作答（可以描述图中结构、标注、细节）。
   - **kb_fetch_doc**：按 文档/章节/关键词 提取**全部**匹配片段（非相似度排序，按文档原序）。适合用户要「完整表格/完整清单/全部条目」「导出整张表」，或当 kb_search 返回的表格/清单明显被切断（缺行缺列）时改用它补全。**取完整表格的正确策略**：一张大表常被切成很多块，且数据行往往不含表名关键词（如表名是「受力部件」但数据行是材料牌号/规格）。所以①先用表名关键词（contains=表名）取表头/说明性块；②看其中出现的材料牌号/编号/类别词（如 API 5L、CrNiMo、QT、序号等），再用这些作为 contains 各取一次，把数据行抓全；③最后把多批片段按文档原序拼回整表。单次问题内最多调 4 次（该工具不走向量、开销小）。
   - **tracker_lookup**：查**追踪表**——文档入库时 AI 自动登记的结构化台账。**字段由各库管理员自行配置，不同库字段不同**；不确定某库登记了哪些信息时，先留空 query 调一次看字段配置与最近登记，再按字段值查。用户问「某文档登记的某项信息是多少」「登记台账里有没有…」这类**登记信息**问题时优先用它，比检索原文片段更准。单次问题内最多调 2 次。
3. **检索经济性**：已取到的内容直接用于回答，不要用相近关键词重复检索。若第一次结果不足，换一个实质不同的关键词再查一次。生成 write_analysis 前，确保已取到足够完整的数据。
4. **如何描述你的依据（重要）**：你的知识来自 kb_search / kb_fetch_doc 取回的**知识库片段**，不是你「打开了 PDF」或「读取了整份文档」。回答时请如实表述为「根据从 XX 文档检索到的内容」「知识库中的相关片段显示」，并标注来源文档名。片段可能不完整或含 OCR 误差——不要假装你看到了完整的原文/整张表格；若已用 kb_fetch_doc 多次仍取不到某部分，明确说明「检索到的片段中未包含该部分」。
5. **页码与行号引用**：检索结果的来源行若带「第 N 页」（或区间），在回答中引用该内容时请一并注明页码（如「见 XX 文档第 43 页」）；表格片段带「表第 X-Y 行」时引用「原表第 X 行」。这些页码来自 OCR 版面定位，直接使用即可，不要自己推算页码。
6. 回答须准确、客观，语言专业且易于理解。
7. 若知识库中无相关内容，如实说明，不得编造（禁止幻觉）。

如果用户只要求「列个表格」「整理成表格」，直接返回简洁的 Markdown 表格，不调用 write_analysis，不生成 Python 或下载文件。表格按资料中真实条目组织，避免重复双语和冗长前言；不要把未检索到的条目补齐。
只有用户明确要求计算分析、画图、下载或导出 Excel/CSV/文件时：
- 先取数据：若是要某张表/清单的【完整】内容（如「导出受力部件表」「列出全部故障代码」），优先用 kb_fetch_doc（contains=关键词）一次性抓全；若是查某个具体指标，用 kb_search；若是查登记台账信息，用 tracker_lookup。
- 再调用 write_analysis，传入完整可运行的 Python 代码（code 参数）。
- 代码会在用户浏览器的 Pyodide 沙箱里运行，可用 pandas、numpy、matplotlib。
- 把检索到的数据作为字面量写进代码，例如：df = pd.DataFrame([...])。
- **字符串引号安全**：中文/英文字符串内部若含双引号，用单引号定义该字符串，或用「」代替内部双引号。绝对禁止在 "..." 字符串内部出现未转义的双引号——会导致 SyntaxError。生成代码后自查所有字符串字面量的引号配对。
- 导出 Excel：直接用 df.to_excel(filename, index=False)（沙箱已注入纯 Python 的 xlsx 写入器，无需 openpyxl，不要 import openpyxl）。filename 用 write_analysis 的 filename 参数（默认 analysis.xlsx）。支持多工作表：with pd.ExcelWriter(filename) as w: df1.to_excel(w, sheet_name='表1', index=False); df2.to_excel(w, sheet_name='表2', index=False)。若用户要 CSV，用 df.to_csv(filename, index=False)。
- 若要画图（柱状图、折线图、饼图等）：用 matplotlib，并 plt.savefig("plot.png", dpi=120, bbox_inches="tight") 保存为 PNG。无需 plt.show()（浏览器环境无显示）。中文标签用英文或加 plt.rcParams['font.family']='sans-serif' 避免缺字。生成的 PNG 会自动在页面渲染并可下载。
- 代码末尾可用 print() 输出关键结果，便于用户在页面看到。
- 调用 write_analysis 后，简要说明你生成了什么、如何查看/下载。
"""

    def list_knowledge_bases() -> str:
        """列出所有可用的知识库（层级结构），供判断该检索哪个文档库。

        返回文件夹→文档库的树形结构，含每个文档库的 slug、描述、文档名、向量块数。
        用文档库的 slug 作为 kb_search 的 kb_slug 参数来检索该文档库；
        用文件夹的 slug 可跨其下所有文档库合并检索。
        """
        return _kb_tree_text(_dept_filter)

    def kb_search(query: str, k: int = 0, kb_slug: str = "") -> str:
        """检索知识库，返回带来源标注的相关片段（多模态：含文本与图片）。

        结果里可能混有 [图片] 条目 = 文档里的图纸/示意图（其文本为章节与图注）。
        站点开启视觉模式（LLM 支持图片输入）时，最相关的命中图片会以图片本体
        随结果附上——请结合图片内容本身回答视觉问题。
        图片命中也会自动附到回答引用区给用户看，不要在回答里复述图片链接。

        参数:
            query: 要检索的问题或关键词。
            k: 返回的条目数量（0 表示用配置默认 Top-K）。
            kb_slug: 要检索的知识库标识。不传则用当前默认范围。
                     传文档库 slug → 只搜该文档；传文件夹 slug → 跨其下所有文档库合并检索。
                     若不确定该查哪个库，先调用 list_knowledge_bases 查看可选项。
        """
        from .retriever import search as _search, search_folder as _search_folder
        target = kb_slug or kb_slug_default
        if k <= 0:
            # k=0 表示「用默认」。这里在配置默认上再抬高一点（至少 8）：
            # 游乐设施手册里一张表常被切成十几个块，默认 top_k=5 只能取到少数片段，
            # 抬到 8 可让一次检索覆盖更多相关行，减少「表格不完整」的情况。
            k = max(top_k, 8)

        # 判断 target 是文件夹还是文档库：文件夹 → 扇出搜索所有子库
        from .models import KnowledgeBase
        try:
            target_kb = KnowledgeBase.objects.get(slug=target)
        except KnowledgeBase.DoesNotExist:
            return f"知识库「{target}」不存在，请用 list_knowledge_bases 查看可选项。"
        if not _dept_allowed(target_kb):
            # 部门不可访问：与不存在同样处理，不泄露存在性
            return f"知识库「{target}」不存在，请用 list_knowledge_bases 查看可选项。"

        try:
            if target_kb.is_folder:
                child_slugs = target_kb.child_doc_slugs()
                if not child_slugs:
                    return f"文件夹「{target}」下暂无文档库。"
                results = _search_folder(child_slugs, query, k=k)
            else:
                results = _search(target, query, k=k)
        except Exception as e:
            return f"检索失败（库 {target}）：{e}"
        if not results:
            return f"在库「{target}」中未检索到相关内容。"
        # 溯源标注：带 chunk_id 的结果补页码/表格行号（ChunkProvenance，
        # 入库时从 MinerU 版面解析；老文档无溯源则静默跳过）
        try:
            from . import provenance as _prov
            _prov.annotate_results(results)
        except Exception:
            pass
        # 保存完整来源快照；核对预算不足时拒绝发布，不静默截断条件。
        from .publication import bind_evidence
        for item in bind_evidence(results, target):
            if item not in evidence_sink:
                evidence_sink.append(item)
        blocks = []
        img_hits: list[tuple[str, str]] = []  # (doc_id, 图片名)——视觉模式用
        # 记录来源出处（供前端渲染可点击链接 → 文档查看页高亮）。
        # 按【文档】去重：同一文档只出一个 chip，但收集该文档所有命中片段，
        # 点击后查看页一次性高亮全部命中块。
        import re as _re
        by_doc: dict[str, dict] = {}
        seen_snippets: set[str] = set()
        for r in results:
            doc_id = r.get("doc_id", "")
            if not doc_id:
                continue
            # 图片命中：不做文本高亮锚点，挂到该文档引用条目的 images 列表
            if r.get("type") == "image" and r.get("image"):
                entry = by_doc.get(doc_id)
                if r.get("chunk_id") and str(r.get("image", "")).startswith("page"):
                    # 整页视觉块：缩略图走证据接口按需渲染原 PDF 页
                    img_url = f"/kb/evidence/{r['chunk_id']}/page.png"
                else:
                    img_url = f"/kb/doc/{doc_id}/img/{r['image']}"
                if entry is None:
                    by_doc[doc_id] = {
                        "doc_id": doc_id, "source": r.get("source", ""),
                        "highlights": [], "images": [img_url],
                        "best_score": r.get("score", 0),
                    }
                elif img_url not in entry.get("images", []):
                    entry.setdefault("images", []).append(img_url)
                    if r.get("score", 0) > entry["best_score"]:
                        entry["best_score"] = r.get("score", 0)
                continue
            # 清洗 chunk 文本为可匹配的纯文本（去切块器加的【section】前缀、
            # Markdown 标记、HTML 残片），再取一个连续字符锚点用于高亮定位。
            txt = r.get("text") or ""
            clean = _re.sub(r"^【[^】]*】\s*\n?", "", txt)
            clean = _re.sub(r"^\s*#{1,6}\s*", "", clean)
            clean = _re.sub(r"^\s*[-*+]\s*", "", clean)
            clean = _re.sub(r"<[^>]+>", "", clean).strip()
            snippet = _pick_anchor(clean)
            # 过滤掉 HTML 属性词（rowspan/colspan 等，是表格残片非正文）
            if snippet.lower() in ("rowspan", "colspan", "cellspacing", "cellpadding", "valign"):
                snippet = ""
            if not snippet or snippet in seen_snippets:
                continue
            seen_snippets.add(snippet)

            # chunk 级引用（有溯源时）：前端据此开证据面板（原 PDF 页 + 红圈）
            chunk_meta = None
            if r.get("chunk_id") and r.get("page_label"):
                chunk_meta = {
                    "chunk_id": r["chunk_id"],
                    "page": r["page_label"],
                    "is_image": False,
                }

            entry = by_doc.get(doc_id)
            if entry is None:
                by_doc[doc_id] = {
                    "doc_id": doc_id,
                    "source": r.get("source", ""),
                    "highlights": [snippet],
                    "best_score": r.get("score", 0),
                    "chunks": [chunk_meta] if chunk_meta else [],
                }
            else:
                entry["highlights"].append(snippet)
                if chunk_meta and len(entry.get("chunks", [])) < 8:
                    entry["chunks"].append(chunk_meta)
                if r.get("score", 0) > entry["best_score"]:
                    entry["best_score"] = r.get("score", 0)
        # 按最高相关度排序，合并进 cite_sink（跨多次 kb_search 调用按 doc_id 去重：
        # 已在 sink 里的文档，只追加新的 highlights，不再新增条目）
        for entry in sorted(by_doc.values(), key=lambda c: c["best_score"], reverse=True):
            existing = next((c for c in cite_sink if c.get("doc_id") == entry["doc_id"]), None)
            if existing is None:
                cite_sink.append({
                    "doc_id": entry["doc_id"],
                    "source": entry["source"],
                    "highlights": list(entry["highlights"]),
                    "images": list(entry.get("images", [])),
                    "chunks": [c for c in entry.get("chunks", []) if c],
                })
            else:
                # 合并 highlights / images / chunks（去重）
                for h in entry["highlights"]:
                    if h not in existing["highlights"]:
                        existing["highlights"].append(h)
                for u in entry.get("images", []):
                    existing.setdefault("images", [])
                    if u not in existing["images"]:
                        existing["images"].append(u)
                for ch in entry.get("chunks", []):
                    if ch and ch not in existing.get("chunks", []):
                        existing.setdefault("chunks", []).append(ch)
        for i, r in enumerate(results, 1):
            # 来源是【文档文件名】；section（所属章节）作为补充上下文，不是来源本身。
            src = r.get("source") or "未知来源"
            section = r.get("section") or ""
            src_full = f"{src}（章节: {section}）" if section else src
            # 版面溯源：页码 + 表格行区间（有溯源时才有，模型直接引用不要推算）
            if r.get("page_label"):
                src_full += f" · {r['page_label']}"
            rows_txt = _rows_label(r.get("prov_blocks"))
            if rows_txt:
                src_full += f" · {rows_txt}"
            if r.get("type") == "image" and r.get("doc_id") and r.get("image"):
                # 不带图片 URL：图片命中已自动附到回答引用区，给 LLM 看 URL 只会被
                # 原样抄进答案正文（实测如此）
                body = f"[图片] {r.get('text', '')}"
                img_hits.append((r["doc_id"], r["image"]))
            else:
                body = r["text"]
            blocks.append(f"[{i}] (来源文档: {src_full})\n{body}")
        header = (
            f"已从知识库「{target}」检索到 {len(results)} 条相关片段"
            f"（向量相似度检索，非原文直读；片段可能不完整或含 OCR 噪声）。"
        )
        text_out = header + "\n\n" + "\n\n".join(blocks)
        # 视觉模式补一道图片专用召回：图片块与文本查询存在模态鸿沟，混排时
        # 几乎进不了 top-k（img_hits 常年为空）——单独按 type=image 拉最近的图。
        # 多取一倍备用：下面的「同库约束」会滤掉其它库的图
        if llm_cfg.get("vision") and not img_hits:
            try:
                from .pipeline import _embeddings
                from .retriever import _image_lane
                q_emb = _embeddings().embed_query(query)
                lane_slugs = (target_kb.child_doc_slugs() if target_kb.is_folder
                              else [target])
                img_hits = [(it["doc_id"], it["image"])
                            for it in _image_lane(lane_slugs, q_emb,
                                                  limit=_VISION_MAX_IMAGES * 2)
                            if it.get("doc_id") and it.get("image")]
            except Exception:  # noqa: BLE001 —— 图片道失败不拖累文本检索
                img_hits = []
        # 同库约束：同一次进入 LLM 上下文的图片必须在同一个知识库下——
        # 跨库图纸混附会让模型张冠李戴（文件夹扇出检索天然跨子库）。
        # 以第一张（最相关）图片所属库为锚，其余库的一律滤掉
        if llm_cfg.get("vision") and len(img_hits) > 1:
            try:
                from .models import Document
                kb_of = dict(Document.objects.filter(
                    id__in=[d for d, _ in img_hits]).values_list("id", "kb__slug"))
                anchor = kb_of.get(img_hits[0][0])
                img_hits = [h for h in img_hits if kb_of.get(h[0]) == anchor]
            except Exception:  # noqa: BLE001 —— 解析失败退化为不附图
                img_hits = []
        # 视觉模式：站点 LLM 支持图片输入时，把最相关的命中图片本体随工具结果
        # 返回（多模态内容块），模型可直接依据图片内容回答——不再只看图注文字
        if llm_cfg.get("vision") and img_hits:
            parts = [{"type": "text",
                      "text": text_out + "\n\n（下方附本轮检索最相关的命中图片本体，请结合图片内容本身作答）"}]
            n, seen_img = 0, set()
            for doc_id, img in img_hits:
                if img in seen_img:
                    continue
                blk = _image_content_block(doc_id, img)
                if blk:
                    parts.append(blk)
                    seen_img.add(img)
                    n += 1
                if n >= _VISION_MAX_IMAGES:
                    break
            if n:
                return parts
        return text_out

    def kb_fetch_doc(
        kb_slug: str = "", source: str = "", section: str = "",
        contains: str = "", limit: int = 40,
    ) -> str:
        """按条件提取一个文档库的全部匹配片段（不做相似度检索，按文档顺序返回）。

        用途：当用户想要【完整的】某张表/某个清单/导出全部行时，kb_search 的相似度检索
        只返回 top_k 片段，会漏掉同一张表被切到其它块里的行。本工具按 文档名/章节/关键词
        一次性取出全部命中片段，按入库（原文）顺序拼接，便于拼回完整内容。

        与 kb_search 的区别：
        - kb_search：按问题语义检索最相关的少数片段（适合「问某个点」）。
        - kb_fetch_doc：按条件提取全部匹配片段（适合「要完整表格/列表」「导出全部」，
          或 kb_search 返回的表明显不完整时改用它补全）。

        参数:
            kb_slug: 文档库标识（必须是文档库，不能是文件夹）。不传则用当前默认范围。
            source: 限定文档文件名（不传则该库下所有文档）。
            section: 限定章节名（切块时按 ## 标题提取；不传则所有章节）。
            contains: 只保留正文含该子串的片段。例如 contains='受力部件' 可抓全一张
                      散落多块的表（即便某些块的 section 元数据缺失也能命中）。
            limit: 最多返回片段数（控制 token 用量，默认 40）。
        """
        from .retriever import fetch_doc as _fetch_doc
        target = kb_slug or kb_slug_default

        from .models import KnowledgeBase
        try:
            target_kb = KnowledgeBase.objects.get(slug=target)
        except KnowledgeBase.DoesNotExist:
            return f"知识库「{target}」不存在，请用 list_knowledge_bases 查看可选项。"
        if not _dept_allowed(target_kb):
            return f"知识库「{target}」不存在，请用 list_knowledge_bases 查看可选项。"
        if target_kb.is_folder:
            return (f"「{target}」是文件夹，本工具只能提取单个文档库的片段。"
                    f"请先用 list_knowledge_bases 找到其下的文档库 slug，再用该 slug 调用本工具。")

        try:
            results = _fetch_doc(target, source=source, section=section,
                                 contains=contains, limit=limit)
        except Exception as e:
            return f"提取失败（库 {target}）：{e}"
        if not results:
            cond = []
            if source: cond.append(f"文档={source}")
            if section: cond.append(f"章节={section}")
            if contains: cond.append(f"含关键词={contains}")
            return f"在文档库「{target}」中未找到匹配片段（{', '.join(cond) or '无条件'}）。"
        # 溯源标注（页码/行号）+ 证据收集（供核实步）
        try:
            from . import provenance as _prov
            _prov.annotate_results(results)
        except Exception:
            pass
        from .publication import bind_evidence
        for item in bind_evidence(results, target):
            if item not in evidence_sink:
                evidence_sink.append(item)

        # 记录来源出处（与 kb_search 同一 cite_sink 逻辑：按文档去重，合并 highlights）
        import re as _re
        # 批量解析 文件名 → doc_id（循环外一次查询，避免每片段一次的 N+1）
        try:
            from .models import Document
            name_to_id = {
                d["original_name"]: str(d["id"])
                for d in Document.objects.filter(kb__slug=target)
                .values("original_name", "id")
            }
        except Exception:
            name_to_id = {}
        by_doc: dict[str, dict] = {}
        for r in results:
            doc_id = name_to_id.get(r.get("source", ""), "")
            if not doc_id:
                continue
            txt = r.get("text") or ""
            clean = _re.sub(r"^【[^】]*】\s*\n?", "", txt)
            clean = _re.sub(r"<[^>]+>", "", clean).strip()
            snippet = _pick_anchor(clean)
            if snippet.lower() in ("rowspan", "colspan", "cellspacing", "cellpadding", "valign"):
                snippet = ""
            chunk_meta = None
            if r.get("chunk_id") and r.get("page_label"):
                chunk_meta = {"chunk_id": r["chunk_id"], "page": r["page_label"],
                              "is_image": False}
            entry = by_doc.get(doc_id)
            if entry is None:
                by_doc[doc_id] = {"doc_id": doc_id, "source": r.get("source", ""),
                                  "highlights": [snippet] if snippet else [],
                                  "best_score": 1.0,
                                  "chunks": [chunk_meta] if chunk_meta else []}
            else:
                if snippet and snippet not in entry["highlights"]:
                    entry["highlights"].append(snippet)
                if chunk_meta and len(entry.get("chunks", [])) < 8:
                    entry["chunks"].append(chunk_meta)
        for entry in by_doc.values():
            existing = next((c for c in cite_sink if c.get("doc_id") == entry["doc_id"]), None)
            if existing is None:
                cite_sink.append({"doc_id": entry["doc_id"], "source": entry["source"],
                                  "highlights": list(entry["highlights"]),
                                  "chunks": [c for c in entry.get("chunks", []) if c]})
            else:
                for h in entry["highlights"]:
                    if h not in existing["highlights"]:
                        existing["highlights"].append(h)
                for ch in entry.get("chunks", []):
                    if ch and ch not in existing.get("chunks", []):
                        existing.setdefault("chunks", []).append(ch)

        blocks = []
        for i, r in enumerate(results, 1):
            src = r.get("source") or "未知来源"
            sec = r.get("section") or ""
            src_full = f"{src}（章节: {sec}）" if sec else src
            if r.get("page_label"):
                src_full += f" · {r['page_label']}"
            rows_txt = _rows_label(r.get("prov_blocks"))
            if rows_txt:
                src_full += f" · {rows_txt}"
            blocks.append(f"[{i}] (来源文档: {src_full})\n{r['text']}")
        cond = []
        if source: cond.append(f"文档={source}")
        if section: cond.append(f"章节={section}")
        if contains: cond.append(f"含关键词={contains}")
        header = (
            f"已从文档库「{target}」按条件取出 {len(results)} 条片段"
            f"（整段提取，非相似度排序；按文档原序拼接，可能含 OCR 噪声）。"
            f"\n筛选: {', '.join(cond) or '全部'}"
        )
        return header + "\n\n" + "\n\n".join(blocks)

    def tracker_lookup(query: str = "", kb_slug: str = "") -> str:
        """查询追踪表：文档入库时 AI 自动抽取登记的结构化关键信息表（登记台账）。

        追踪字段由**各库管理员自行配置，不同库字段不同**（如检测报告库可能是
        检验结论类字段，设备手册库可能是型号参数类字段）。不确定某库登记了
        哪些信息时，先把 query 留空调用一次：会列出各库的字段配置与最近登记，
        再按字段值查。用户问「某文档登记的某项信息是多少」这类台账问题时
        优先用本工具——比 kb_search 检索原文片段更直接、更准确。

        参数:
            query: 关键词，匹配文档名或任意字段值（不区分大小写）。留空 = 各库字段配置 + 最近登记。
            kb_slug: 只查该知识库（或其父文件夹）的追踪表。不传 = 可见范围内全部追踪表。
        """
        from .models import KbTracker, KnowledgeBase, TrackerRow

        q = (query or "").strip().lower()
        cands: list = []
        if kb_slug:
            try:
                target_kb = KnowledgeBase.objects.get(slug=kb_slug)
            except KnowledgeBase.DoesNotExist:
                return f"知识库「{kb_slug}」不存在，请用 list_knowledge_bases 查看可选项。"
            if not _dept_allowed(target_kb):
                # 部门不可访问：与不存在同样处理，不泄露存在性
                return f"知识库「{kb_slug}」不存在，请用 list_knowledge_bases 查看可选项。"
            # 抽取归属是「就近」的：本库优先，其次父文件夹
            cands = [getattr(target_kb, "tracker", None),
                     getattr(target_kb.parent, "tracker", None)]
        else:
            cands = list(KbTracker.objects.select_related("kb"))

        tracker_ids, labels = [], {}
        for tr in cands:
            if tr is None or not tr.fields or tr.id in tracker_ids:
                continue
            if not _dept_allowed(tr.kb):
                continue
            tracker_ids.append(tr.id)
            labels[tr.id] = [f["label"] for f in tr.fields]
        if not tracker_ids:
            return ("可见范围内没有配置追踪表的知识库；"
                    "这类问题请改用 kb_search 检索文档原文。")

        rows = (TrackerRow.objects.filter(tracker_id__in=tracker_ids)
                .select_related("document", "tracker__kb")
                .order_by("-updated_at")[:200])
        status_map = dict(TrackerRow.Status.choices)
        matched = []
        for r in rows:
            doc_l = (r.document.original_name or "").lower() if r.document else ""
            if q and doc_l.find(q) < 0:
                hit = any(q in str(v).lower()
                          for v in list(r.values.values()) + list(r.proposed_values.values()))
                if not hit:
                    continue
            matched.append(r)
            if len(matched) >= 20:
                break
        if not matched and q:
            return (f"追踪表中没有匹配「{query}」的记录。"
                    "可换关键词、留空 query 查看各库字段配置，或改用 kb_search 检索文档原文。")

        blocks = []
        # 空查询 = 浏览模式：先给各库的字段配置（模型据此判断该查什么词）
        if not q:
            for tid in tracker_ids:
                tr = next(t for t in cands if t is not None and t.id == tid)
                blocks.append(f"◆ 库「{tr.kb.name}」追踪字段：{' / '.join(labels[tid])}")
            blocks.append("")
        if not matched:
            return "\n".join(blocks) + "（各库均无登记记录）"
        for i, r in enumerate(matched, 1):
            fl = labels.get(r.tracker_id, [])
            parts = []
            for k in fl:
                v = str(r.values.get(k, "") or "").strip()
                pv = str(r.proposed_values.get(k, "") or "").strip()
                shown = v or pv
                if not shown:
                    continue
                if r.status == TrackerRow.Status.PROPOSED and pv and pv != v:
                    parts.append(f"{k}: {shown}（待管理员确认）")
                else:
                    parts.append(f"{k}: {shown}")
            st = status_map.get(r.status, r.status)
            doc_name = r.document.original_name if r.document else "（文档已删除）"
            updated = r.updated_at.strftime("%Y-%m-%d")
            body = " | ".join(parts) if parts else "（登记字段均为空）"
            blocks.append(f"[{i}] 库「{r.tracker.kb.name}」· {doc_name}"
                          f"（{updated} 登记 · {st}）\n    {body}")
        head = (f"追踪表查询结果（关键词「{query or '浏览：字段配置 + 最近记录'}」，"
                f"共 {len(matched)} 条，最多展示 20 条）：")
        return head + "\n" + "\n".join(blocks)

    def write_analysis(code: str, filename: str = "analysis.xlsx") -> str:
        """生成一段 Python 数据分析/导出脚本，交由用户浏览器的 Pyodide 沙箱执行。

        可用库：pandas、numpy、matplotlib（无需、也不可 import openpyxl）。
        - 数据作为字面量写入代码（如 df = pd.DataFrame([...])）。
        - **字符串引号安全（重要）**：中文字符串里若含双引号（如「主要受力结构部件」被引号包裹），
          必须用单引号定义字符串，或用「」代替内部双引号。禁止在 "..." 字符串内部出现未转义的 "。
          例：正确 '汇总表（含"分组"）'  或  "汇总表（含「分组」）"；错误 "汇总表（含"分组"）"。
        - 导出 Excel：直接 df.to_excel(filename, index=False)（沙箱已注入 xlsx 写入器，支持多工作表）。
        - 导出 CSV：df.to_csv(filename, index=False)。
        - 画图：用 matplotlib，plt.savefig("plot.png", dpi=120, bbox_inches="tight") 保存为 PNG（PNG 会在页面渲染并可下载，无需 plt.show()）。
        - 可用 print() 输出关键结果。

        参数:
            code: 完整可运行的 Python 代码字符串。
            filename: 若代码生成文件，使用的文件名（默认 analysis.xlsx）。
        """
        return f"已生成分析脚本（{filename}），将在浏览器沙箱中运行。"

    def request_verified_export(filename: str = "verified.csv") -> str:
        """请求导出已核对表格为 CSV。先在最终回答中写一个完整 Markdown 表格。

        只登记导出意图，核对通过后由固定模板生成文件。无需也不接受 Python 代码。
        """
        return "已登记导出请求。请在最终回答给出一个完整表格；仅核对通过后才能生成 CSV。"

    # 复用进程级持久化 checkpointer；thread_id（在 thread_config 里）区分不同会话
    return create_agent(
        model=_get_llm(llm_cfg),
        tools=([list_knowledge_bases, kb_search, kb_fetch_doc, tracker_lookup, request_verified_export]
               if llm_cfg.get("qa_enhance") else
               [list_knowledge_bases, kb_search, kb_fetch_doc, tracker_lookup, write_analysis]),
        system_prompt=system_prompt + ("\n增强模式：必须检索原文；历史回答只帮助理解，不是事实依据。展示有出处的文本或 Markdown 表格。需导出时调用 request_verified_export 并在最终回答给出一个完整表格。仅在核对通过后生成 CSV，不得声称文件已经生成。此模式不执行自由生成脚本。台账建议和视觉推断须回原文检索核对。" if llm_cfg.get("qa_enhance") else ""),
        checkpointer=checkpointer,
        name=f"kb_agent_{kb_slug}",
    )


async def run_agent_stream(
    message: str, thread_id: str, kb_slug: str, config: dict | None = None,
) -> AsyncGenerator[tuple[str, dict], None]:
    """运行 Agent 并以 SSE 事件流形式产出。

    config: 已在同步上下文解析好的配置 {"llm": {...}, "top_k": int}。
    若为 None，则回退到同步读取（仅适用于同步调用场景）。
    """
    try:
        if config is None:
            cfg = llm_settings()
            top_k = retrieval_settings()["top_k"]
            department = ""
        else:
            cfg = config["llm"]
            top_k = config["top_k"]
            department = config.get("department") or ""
        enhance = bool(cfg.get("qa_enhance"))
        # Rebuild each turn from bounded published history. Old checkpoints can
        # retain tens of thousands of tool tokens and resume interrupted work.
        checkpointer = None
        # 本轮检索的来源出处累积器（kb_search 往里追加；流结束发出 citations 事件）
        citations: list[dict] = []
        # 本轮检索证据累积器（带页码的命中片段；答案核实步对照用）
        evidence: list[dict] = []
        # 本轮 token 用量累计（每次 LLM 调用的 usage_metadata 相加）
        usage_in = 0
        usage_out = 0
        # 本轮单次 LLM 调用的最大 input/output token（用于估测模型所需上下文窗口）
        max_in = 0
        max_out = 0

        # ---- 管线增强 · 问题规划（query_plan）----
        # 改写/拆解问题注入 agent 输入；闲聊或规划失败 → 跳过，不影响主流程
        agent_input = message
        enhance = bool(cfg.get("qa_enhance"))
        if enhance:
            yield SSE_STEP, {"stage": "query_plan", "status": "start"}
            from .qa_steps import plan_query
            plan_text, p_usage = await plan_query(cfg, message, history=(config or {}).get("published_history", []))
            usage_in += p_usage["input_tokens"]
            usage_out += p_usage["output_tokens"]
            max_in = max(max_in, p_usage["input_tokens"])
            max_out = max(max_out, p_usage["output_tokens"])
            if plan_text:
                agent_input = (f"【问题理解与检索规划】\n{plan_text}\n\n"
                               f"【用户问题】\n{message}")
                yield SSE_STEP, {"stage": "query_plan", "status": "done",
                                 "detail": "问题规划完成"}
            else:
                yield SSE_STEP, {"stage": "query_plan", "status": "skip",
                                 "detail": "无需规划（闲聊）或规划不可用"}

        agent = _build_agent(kb_slug, thread_id, cfg, top_k, checkpointer,
                             citations=citations, department=department,
                             evidence=evidence)
        # recursion_limit 是顶层 key（不在 configurable 内）。
        # 每次工具调用 ≈ 2 个节点（agent + tool）。取完整表格时可能用到
        # list_kb + kb_search×2 + kb_fetch_doc×4 + write_analysis ≈ 8 次调用，
        # 故设 25（≈12 次调用）留余量。工具结果有 limit 上限，不会无限堆积。
        thread_config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 25,
        }

        pending_reasoning: list[str] = []
        emitted_reasoning = False
        # run_id -> {"code":..., "filename":...}，捕获 write_analysis 工具的入参
        pending_code: dict[str, dict] = {}
        export_filename = None
        # 完整回答文本（核实步用）与管线阶段翻转标记
        full_text: list[str] = []
        final_complete = False
        search_step_on = False
        answer_step_on = False

        async for event in agent.astream_events(
            {"messages": (config or {}).get("published_history", []) + [{"role": "user", "content": agent_input}]},
            config=thread_config,
            version="v2",
        ):
            etype = event.get("event")
            data = event.get("data", {})
            run_id = event.get("run_id", "")
            name = event.get("name", "")

            if etype == "on_chat_model_start":
                pending_reasoning = []

            elif etype == "on_chat_model_stream":
                chunk = data.get("chunk")
                if chunk:
                    content = getattr(chunk, "content", None)
                    if isinstance(content, str) and content:
                        pending_reasoning.append(content)
                        if not enhance:
                            full_text.append(content)
                        if not answer_step_on:
                            answer_step_on = True
                            if search_step_on:
                                yield SSE_STEP, {"stage": "hybrid_search", "status": "done"}
                            yield SSE_STEP, {"stage": "answer_generation", "status": "start"}
                        if not enhance:
                            yield SSE_TOKEN, {"text": content}

            elif etype == "on_chat_model_end":
                output = data.get("output")
                # 累计本轮 LLM token 用量（每次模型调用的 usage_metadata）
                um = getattr(output, "usage_metadata", None) or {}
                call_in = int(um.get("input_tokens") or 0)
                call_out = int(um.get("output_tokens") or 0)
                usage_in += call_in
                usage_out += call_out
                if call_in > max_in:
                    max_in = call_in
                if call_out > max_out:
                    max_out = call_out
                tool_calls = getattr(output, "tool_calls", None) or []
                finish_reason = (getattr(output, "response_metadata", None) or {}).get("finish_reason")
                if finish_reason in ("length", "content_filter"):
                    if enhance:
                        yield SSE_VERIFY, {"ok": False, "issues": ["输出截断，未发布草稿。"]}
                    yield SSE_ERROR, {"message": "模型输出被截断，回答未完成；已显示的内容不能作为完整表格。请缩小范围后重试。", "code": "incomplete_output"}
                    return
                if enhance:
                    full_text = [] if tool_calls else [getattr(output, "content", "") or "" ]
                    final_complete = not tool_calls and (getattr(output, "response_metadata", None) or {}).get("finish_reason") not in ("length", "content_filter")
                if tool_calls and not emitted_reasoning and not enhance:
                    emitted_reasoning = True
                    reason = _reason_from_tool_calls(tool_calls)
                    if pending_reasoning:
                        preface = "".join(pending_reasoning).strip()
                        if preface:
                            reason = preface + ("\n\n" + reason if reason else "")
                    if reason:
                        yield SSE_REASONING, {"text": reason}
                pending_reasoning = []
                emitted_reasoning = False

            elif etype == "on_tool_start":
                if not search_step_on:
                    search_step_on = True
                    yield SSE_STEP, {"stage": "hybrid_search", "status": "start"}
                if enhance and name == "request_verified_export":
                    export_args = data.get("input") or {}
                    export_filename = export_args.get("filename", "verified.csv") if isinstance(export_args, dict) else "verified.csv"
                # 捕获 write_analysis 的入参，供 on_tool_end 发出 code_run
                if name == "write_analysis":
                    serial = data.get("serializable_input") or {}
                    args = serial.get("args") or data.get("input") or {}
                    if isinstance(args, str):
                        try:
                            import json as _json
                            args = _json.loads(args)
                        except Exception:
                            args = {}
                    pending_code[run_id] = {
                        "code": args.get("code", ""),
                        "filename": args.get("filename", "analysis.xlsx"),
                    }
                yield SSE_TOOL_START, {
                    "tool": name,
                    "label": _tool_label(name),
                    "run_id": run_id,
                }

            elif etype == "on_tool_end":
                output = data.get("output")
                # write_analysis 结束 → 发出 code_run，前端在 Pyodide 沙箱执行
                if name == "write_analysis" and run_id in pending_code:
                    pc = pending_code.pop(run_id)
                    if pc.get("code") and not enhance:
                        yield SSE_CODE_RUN, {
                            "code": pc["code"],
                            "filename": pc["filename"],
                        }
                yield SSE_TOOL_END, {
                    "tool": name,
                    "run_id": run_id,
                    "status": "done",
                    "output_preview": "" if enhance else _preview(output),
                }

        # 生成阶段收尾（无 token 输出的异常路径不补 done）
        if answer_step_on:
            yield SSE_STEP, {"stage": "answer_generation", "status": "done"}

        # Enhanced answers are private drafts until verification AND source checks pass.
        if enhance:
            from .qa_steps import verify_answer
            from .publication import validate_sources
            from asgiref.sync import sync_to_async
            answer_text = "".join(t for t in full_text if isinstance(t, str)).strip()
            yield SSE_STEP, {"stage": "answer_verification", "status": "start"}
            verdict = None
            source_ok = await sync_to_async(validate_sources)(evidence, (config or {}).get("user_id"))
            if final_complete and source_ok and answer_text:
                verdict, v_usage = await verify_answer(cfg, message, answer_text, evidence)
                usage_in += v_usage["input_tokens"]
                usage_out += v_usage["output_tokens"]
                max_in = max(max_in, v_usage["input_tokens"])
                max_out = max(max_out, v_usage["output_tokens"])
            source_ok = source_ok and await sync_to_async(validate_sources)(evidence, (config or {}).get("user_id"))
            ok = source_ok and verdict is not None and verdict.get("verdict") == "pass"
            yield SSE_STEP, {"stage": "answer_verification", "status": "done", "ok": ok}
            # Never return verifier prose: it may repeat the rejected parameter.
            yield SSE_VERIFY, {"ok": ok, "issues": [] if ok else ["证据不足、来源已变化或核对未通过，未发布草稿。"]}
            if ok:
                # SSE progress events yield control; recheck immediately before text release.
                source_ok = await sync_to_async(validate_sources)(evidence, (config or {}).get("user_id"))
                ok = source_ok
                if not ok:
                    yield SSE_VERIFY, {"ok": False, "issues": ["来源权限或内容已变化，请重新检索。"]}
            if ok:
                for citation in citations:
                    source = next((ev for ev in evidence if ev.get("doc_id") == citation.get("doc_id")), None)
                    if source:
                        citation["document_digest"] = source["document_digest"]
                yield SSE_TOKEN, {"text": answer_text}
                if export_filename:
                    from .publication import verified_table_export
                    export = verified_table_export(answer_text, str(export_filename))
                    if export and await sync_to_async(validate_sources)(evidence, (config or {}).get("user_id")):
                        yield SSE_CODE_RUN, export
            if not source_ok:
                citations.clear()

        # 流结束：发出本轮来源出处（前端据此渲染可点击的文档链接）
        if citations:
            yield SSE_CITATIONS, {"citations": citations}

        # 流结束：发出本轮 token 用量统计（累计 + 单次最大，后者用于估测模型上下文窗口）
        yield SSE_USAGE, {
            "input_tokens": usage_in,
            "output_tokens": usage_out,
            "total_tokens": usage_in + usage_out,
            "max_input_tokens": max_in,
            "max_output_tokens": max_out,
        }

    except Exception as e:
        # 递归超限：agent 在工具间反复调用未收敛。给出可理解的提示。
        try:
            from langgraph.errors import GraphRecursionError
            if isinstance(e, GraphRecursionError):
                yield SSE_ERROR, {
                    "message": "Agent 反复调用工具未能在步数上限内完成（可能检索/分析步骤过多）。"
                               "请把问题拆小一些，或换个说法再试一次。"
                }
                return
        except Exception:
            pass
        traceback.print_exc()
        yield SSE_ERROR, {"message": str(e)}


def _reason_from_tool_calls(tool_calls: list) -> str:
    parts = []
    for tc in tool_calls:
        args = tc.get("args", {}) or {}
        name = tc.get("name", "")
        if name == "write_analysis":
            parts.append("我生成了一段分析脚本，将在浏览器沙箱中运行。")
        elif name == "kb_fetch_doc":
            c = args.get("contains") or args.get("section") or ""
            parts.append(f"我从文档中提取完整的相关片段{('（'+c+'）') if c else ''}。")
        else:
            q = args.get("query", "")
            parts.append(f"我先检索知识库，查找：{q}" if q else "我先检索知识库。")
    return "\n".join(parts)


def _tool_label(name: str) -> str:
    """工具的可读标签，供前端展示「正在做什么」。"""
    return {
        "list_knowledge_bases": "查看可用知识库…",
        "kb_search": "检索知识库…",
        "kb_fetch_doc": "提取文档片段…",
        "write_analysis": "生成分析脚本…",
    }.get(name, f"调用工具 {name}…")


def _preview(output, limit: int = 200) -> str:
    try:
        s = output if isinstance(output, str) else str(output)
    except Exception:
        s = "(不可读输出)"
    return s if len(s) <= limit else s[:limit] + "…"
