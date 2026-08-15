"""按指定 KB 构建 LangChain Agent 并流式问答。

每个请求根据选中的 KB 动态构建 agent（工具 = 该 KB 的检索），
与父项目 MedicalAgent 的 SSE 事件协议一致。
"""
from __future__ import annotations

import traceback
from functools import lru_cache
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
SSE_CITATIONS = "citations"  # 本轮检索的来源出处（含 doc_id/text，供前端渲染链接）

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
        api_key=llm_cfg["api_key"],
        base_url=llm_cfg["base_url"],
        temperature=llm_cfg["temperature"],
        # 流式模式下显式请求 usage（stream_options.include_usage）。
        # OpenAI 兼容服务（LM Studio/本地模型）默认不在流式 chunk 里带 usage，
        # 不开这个 usage_metadata 恒为空 → 前端 token 统计显示 0。
        stream_usage=True,
    )


def _build_agent(kb_slug: str, thread_id: str, llm_cfg: dict, top_k: int, checkpointer,
                 citations: list | None = None):
    """为指定 KB + thread 构建一个 create_agent。

    llm_cfg / top_k 由调用方在同步上下文中解析后传入，避免在 async
    上下文里访问数据库（Django 默认禁止）。

    citations: 可选的可变列表；kb_search 每次检索会把来源出处追加进去，
    供调用方在流结束后发出 citations 事件。每轮应传入一个全新的空列表。
    """
    from langchain.agents import create_agent

    from .retriever import search

    kb_slug_default = kb_slug  # 避免在 kb_search 内部与参数名冲突
    cite_sink = citations if citations is not None else []

    system_prompt = f"""你是一名专业的知识助手，帮助用户查询游乐设施维护手册等知识库。

知识库结构（重要）：
知识库分两层：**文件夹**（含若干文档库）和**文档库**（每份文档独占一个向量库）。
- 用户提到具体文档名（如「P8」「Dumbo」）时，先调 list_knowledge_bases 查看有哪些文档库及其 slug，再用对应 slug 检索该文档库——这样只返回该文档的内容，不会混杂其它文档。
- 通用问题（不限定某份文档）时，可用文件夹 slug 检索，系统会跨该文件夹下所有文档库合并结果。
- 当前默认搜索范围是「{kb_slug}」。

工作准则：
1. **选库**：不确定有哪些文档库时调一次 list_knowledge_bases 查看层级与 slug，之后按需用 kb_slug 定位到具体文档库。不要每次都调。
2. **检索（两种工具，按需选择）**：
   - **kb_search**：按问题语义检索最相关的少数片段（指定 kb_slug 选库，不传则用默认范围）。适合「问某个点」「查某个指标」。单次问题内最多调 2 次。
   - **kb_fetch_doc**：按 文档/章节/关键词 提取**全部**匹配片段（非相似度排序，按文档原序）。适合用户要「完整表格/完整清单/全部条目」「导出整张表」，或当 kb_search 返回的表格/清单明显被切断（缺行缺列）时改用它补全。**取完整表格的正确策略**：一张大表常被切成很多块，且数据行往往不含表名关键词（如表名是「受力部件」但数据行是材料牌号/规格）。所以①先用表名关键词（contains=表名）取表头/说明性块；②看其中出现的材料牌号/编号/类别词（如 API 5L、CrNiMo、QT、序号等），再用这些作为 contains 各取一次，把数据行抓全；③最后把多批片段按文档原序拼回整表。单次问题内最多调 4 次（该工具不走向量、开销小）。
3. **检索经济性**：已取到的内容直接用于回答，不要用相近关键词重复检索。若第一次结果不足，换一个实质不同的关键词再查一次。生成 write_analysis 前，确保已取到足够完整的数据。
4. **如何描述你的依据（重要）**：你的知识来自 kb_search / kb_fetch_doc 取回的**知识库片段**，不是你「打开了 PDF」或「读取了整份文档」。回答时请如实表述为「根据从 XX 文档检索到的内容」「知识库中的相关片段显示」，并标注来源文档名。片段可能不完整或含 OCR 误差——不要假装你看到了完整的原文/整张表格；若已用 kb_fetch_doc 多次仍取不到某部分，明确说明「检索到的片段中未包含该部分」。
5. 回答须准确、客观，语言专业且易于理解。
6. 若知识库中无相关内容，如实说明，不得编造（禁止幻觉）。

当用户要求数据分析、统计、汇总，或导出 Excel/表格/文件时：
- 先取数据：若是要某张表/清单的【完整】内容（如「导出受力部件表」「列出全部故障代码」），优先用 kb_fetch_doc（contains=关键词）一次性抓全；若是查某个具体指标，用 kb_search。
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

        返回文件夹→文档库的树形结构，含每个文档库的 slug、文档名、向量块数。
        用文档库的 slug 作为 kb_search 的 kb_slug 参数来检索该文档库；
        用文件夹的 slug 可跨其下所有文档库合并检索。
        """
        from .models import KnowledgeBase
        lines = []
        # 文件夹 + 其子文档库
        for folder in KnowledgeBase.objects.filter(is_folder=True).order_by("name"):
            docs, chunks = folder.aggregate_counts()
            lines.append(f"📁 {folder.name}（文件夹, slug={folder.slug}, {chunks} 向量块）")
            for child in folder.children.filter(is_folder=False).order_by("name"):
                doc_names = ", ".join(
                    d.original_name for d in child.documents.all()
                )[:60]
                lines.append(
                    f"  📄 {child.name}（slug={child.slug}, {child.chunk_count} 向量块）— 文档: {doc_names or '（无）'}"
                )
        # 独立文档库（无父库的顶层文档库）
        standalone = KnowledgeBase.objects.filter(is_folder=False, parent__isnull=True).order_by("name")
        for kb in standalone:
            doc_names = ", ".join(d.original_name for d in kb.documents.all())[:60]
            lines.append(
                f"📄 {kb.name}（slug={kb.slug}, {kb.chunk_count} 向量块）— 文档: {doc_names or '（无）'}"
            )
        if not lines:
            return "（暂无知识库）"
        return "可用知识库（📁=文件夹可跨文档检索，📄=文档库搜单份文档）：\n" + "\n".join(lines)

    def kb_search(query: str, k: int = 0, kb_slug: str = "") -> str:
        """检索知识库，返回带来源标注的相关文本片段。

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
        blocks = []
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
            # 清洗 chunk 文本为可匹配的纯文本（去切块器加的【section】前缀、
            # Markdown 标记、HTML 残片），再取一个连续字符锚点用于高亮定位。
            txt = r.get("text") or ""
            clean = _re.sub(r"^【[^】]*】\s*\n?", "", txt)
            clean = _re.sub(r"^\s*#{1,6}\s*", "", clean)
            clean = _re.sub(r"^\s*[-*+]\s*", "", clean)
            clean = _re.sub(r"<[^>]+>", "", clean).strip()
            first_line = clean.split("\n", 1)[0].strip()
            m = _re.search(r"[\u4e00-\u9fffA-Za-z0-9]{6,40}", first_line)
            snippet = m.group(0) if m else first_line[:30]
            # 过滤掉 HTML 属性词（rowspan/colspan 等，是表格残片非正文）
            if snippet.lower() in ("rowspan", "colspan", "cellspacing", "cellpadding", "valign"):
                snippet = ""
            if not snippet or snippet in seen_snippets:
                continue
            seen_snippets.add(snippet)

            entry = by_doc.get(doc_id)
            if entry is None:
                by_doc[doc_id] = {
                    "doc_id": doc_id,
                    "source": r.get("source", ""),
                    "highlights": [snippet],
                    "best_score": r.get("score", 0),
                }
            else:
                entry["highlights"].append(snippet)
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
                })
            else:
                # 合并 highlights（去重）
                for h in entry["highlights"]:
                    if h not in existing["highlights"]:
                        existing["highlights"].append(h)
        for i, r in enumerate(results, 1):
            # 来源是【文档文件名】；section（所属章节）作为补充上下文，不是来源本身。
            src = r.get("source") or "未知来源"
            section = r.get("section") or ""
            src_full = f"{src}（章节: {section}）" if section else src
            blocks.append(f"[{i}] (来源文档: {src_full})\n{r['text']}")
        header = (
            f"已从知识库「{target}」检索到 {len(results)} 条相关片段"
            f"（向量相似度检索，非原文直读；片段可能不完整或含 OCR 噪声）。"
        )
        return header + "\n\n" + "\n\n".join(blocks)

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

        # 记录来源出处（与 kb_search 同一 cite_sink 逻辑：按文档去重，合并 highlights）
        import re as _re
        by_doc: dict[str, dict] = {}
        for r in results:
            doc_id = ""
            # 文件名 → doc_id（与 retriever.search 一致的解析方式）
            try:
                from .models import Document
                d = Document.objects.filter(kb__slug=target, original_name=r.get("source", "")).first()
                if d:
                    doc_id = str(d.id)
            except Exception:
                pass
            if not doc_id:
                continue
            txt = r.get("text") or ""
            clean = _re.sub(r"^【[^】]*】\s*\n?", "", txt)
            clean = _re.sub(r"<[^>]+>", "", clean).strip()
            first_line = clean.split("\n", 1)[0].strip()
            m = _re.search(r"[\u4e00-\u9fffA-Za-z0-9]{6,40}", first_line)
            snippet = m.group(0) if m else first_line[:30]
            if snippet.lower() in ("rowspan", "colspan", "cellspacing", "cellpadding", "valign"):
                snippet = ""
            entry = by_doc.get(doc_id)
            if entry is None:
                by_doc[doc_id] = {"doc_id": doc_id, "source": r.get("source", ""),
                                  "highlights": [snippet] if snippet else [],
                                  "best_score": 1.0}
            elif snippet and snippet not in entry["highlights"]:
                entry["highlights"].append(snippet)
        for entry in by_doc.values():
            existing = next((c for c in cite_sink if c.get("doc_id") == entry["doc_id"]), None)
            if existing is None:
                cite_sink.append({"doc_id": entry["doc_id"], "source": entry["source"],
                                  "highlights": list(entry["highlights"])})
            else:
                for h in entry["highlights"]:
                    if h not in existing["highlights"]:
                        existing["highlights"].append(h)

        blocks = []
        for i, r in enumerate(results, 1):
            src = r.get("source") or "未知来源"
            sec = r.get("section") or ""
            src_full = f"{src}（章节: {sec}）" if sec else src
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

    # 复用进程级持久化 checkpointer；thread_id（在 thread_config 里）区分不同会话
    return create_agent(
        model=_get_llm(llm_cfg),
        tools=[list_knowledge_bases, kb_search, kb_fetch_doc, write_analysis],
        system_prompt=system_prompt,
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
        else:
            cfg = config["llm"]
            top_k = config["top_k"]
        checkpointer = await _get_checkpointer()
        # 本轮检索的来源出处累积器（kb_search 往里追加；流结束发出 citations 事件）
        citations: list[dict] = []
        agent = _build_agent(kb_slug, thread_id, cfg, top_k, checkpointer, citations=citations)
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
        # 本轮 token 用量累计（每次 LLM 调用的 usage_metadata 相加）
        usage_in = 0
        usage_out = 0
        # 本轮单次 LLM 调用的最大 input/output token（用于估测模型所需上下文窗口）
        max_in = 0
        max_out = 0

        async for event in agent.astream_events(
            {"messages": [{"role": "user", "content": message}]},
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
                if tool_calls and not emitted_reasoning:
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
                    if pc.get("code"):
                        yield SSE_CODE_RUN, {
                            "code": pc["code"],
                            "filename": pc["filename"],
                        }
                yield SSE_TOOL_END, {
                    "tool": name,
                    "run_id": run_id,
                    "status": "done",
                    "output_preview": _preview(output),
                }

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
