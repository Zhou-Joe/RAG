"""知识库追踪表：文档入库完成后由 AI 抽取关键信息登记成行。

设计要点：
- 一库一表（KbTracker），字段由管理员自定义（label 即列名即 JSON 键）。
- 抽取走现有 LLM 配置（llm_settings → ChatOpenAI 非流式 invoke），
  提示词要求只输出 JSON；解析容错（剥 ```json 围栏 / 截取首尾大括号）。
- 行状态机 pending → running → done/failed；用条件 UPDATE 原子认领，
  补录与自动钩子并发时同一文档不会被抽两次。
- 失败只标记行（error 保留原因），不影响文档本身；页面可一键重试。
"""
from __future__ import annotations

import json
import logging
import re
import threading

log = logging.getLogger(__name__)

# 送入 LLM 的正文上限：定检/财务类报告的关键信息集中在头部与表格，
# 12k 字符足够覆盖；超出部分截断（也可在字段提示里要求只看某章节）。
_MAX_CONTENT_CHARS = 24000
_MAX_CELL_CHARS = 2000


def _md_head_tail(md: str, limit: int = _MAX_CONTENT_CHARS) -> str:
    """正文进提示词的截断策略：limit 内发全文；超长才保头 + 保尾。

    检验/审批类文档的签名表（检验员、日期、盖章）几乎总在文末，纯头部
    截断会系统性丢掉落款信息（实测 14k 字符文档的检验员姓名在第 13.8k
    字符处被切掉）。尾部保底让落款表始终可见；上限仍防超长手册把本地
    小模型的上下文打爆。
    """
    md = md or ""
    if len(md) <= limit:
        return md
    head = int(limit * 0.58)
    tail = limit - head
    return md[:head] + "\n\n……（中间内容过长已省略）……\n\n" + md[-tail:]
_LLM_TIMEOUT = 600  # 本地 LLM（LM Studio）prefill 慢，超时给足


def build_extraction_prompt(kb_name: str, fields: list[dict], md_content: str,
                            instruction: str = "",
                            prior_values: dict | None = None) -> str:
    """构建抽取提示词（模块函数，便于单测）。

    prior_values：该文档此前已登记的旧值（重试时传入）。LLM 被要求
    保旧优先——只有文档内容明确支持不同的值时才改动，防止弱模型
    把原本正确的值改错。
    """
    field_lines = "\n".join(f"- {f['label']}：{f.get('hint', '')}".rstrip("：")
                            for f in fields)
    tpl = f"""你是资料登记助手。下面是知识库「{kb_name}」中新入库的一份文档全文。
请从文档中提取以下追踪字段，并只输出一个 JSON 对象（不要输出任何其它文字）：

{field_lines}

规则：
1. 每个字段的值必须是文档中事实信息的简明摘录，长度不超过 {_MAX_CELL_CHARS} 字符；要求完整 JSON 或元素数据时，逐项保留原文中的数据，可返回嵌套 JSON 对象或数组。
2. 文档中没有提到的字段，值填空字符串 ""；严禁编造或推测。
3. 日期统一写成 YYYY-MM-DD；金额写数字（可带单位说明）。
"""
    if prior_values:
        import json as _json
        shown = {k: (prior_values.get(k) or "") for k in (f["label"] for f in fields)}
        tpl += ("4. 该文档此前已登记过（旧值）：" +
                _json.dumps(shown, ensure_ascii=False) +
                "\n   除非文档内容明确表明旧值有误，否则保持旧值原样输出；只有文档确实"
                "支持不同值时才改动，不确定时一律沿用旧值。\n")
    if instruction.strip():
        tpl += f"5. 补充要求：{instruction.strip()}\n"
    tpl += f"""

只输出 JSON，形如：{json.dumps({f["label"]: "" for f in fields}, ensure_ascii=False)}

文档全文（超长时中部省略、头尾保留）：
{_md_head_tail(md_content)}"""
    return tpl


def parse_llm_json(text: str) -> dict:
    """解析 LLM 输出的 JSON：剥代码围栏 → 截取首尾大括号 → json.loads。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.IGNORECASE)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("输出中没有 JSON 对象")
    data = json.loads(t[i:j + 1])
    if not isinstance(data, dict):
        raise ValueError("输出不是 JSON 对象")
    return data


def _llm_invoke(prompt: str) -> str:
    """走站点 LLM 配置做一次非流式补全（服务层可 patch 此函数做单测）。

    信号量限 2 并发：批量入库的自动钩子 + 补录 + 手动重试可能同时压上来，
    本地小模型（LM Studio）并行承受力有限，超发只会拖垮整体时延。
    """
    with _LLM_SEMAPHORE:
        from langchain_openai import ChatOpenAI
        from .config import llm_settings

        cfg = llm_settings()
        llm = ChatOpenAI(
            model=cfg["model"],
            api_key=cfg["api_key"] or "local-no-key",
            base_url=cfg["base_url"],
            temperature=0,
            timeout=_LLM_TIMEOUT,
            max_retries=0,  # 默认 2 次重试会把最坏时长拖到 3×600s，超过卡死回收的 20 分钟窗
        )
        resp = llm.invoke(prompt)
        return getattr(resp, "content", "") or ""


_LLM_SEMAPHORE = threading.Semaphore(2)


def run_extraction(doc_id, tracker_id=None) -> bool:
    """对一份文档执行抽取（幂等入口）。返回是否成功改到 done。

    条件 UPDATE 认领：仅当行处于 pending/failed 时置 running，
    并发调用（自动钩子 + 手动补录/重试）只有一方能继续。
    """
    import django
    django.setup()
    from .models import Document, FeedRecord, KbTracker, TrackerRow

    def extraction_allowed(current_doc, record):
        if record is None:
            return current_doc.status == Document.Status.COMPLETED
        if record.deleted:
            return False
        if current_doc.status == Document.Status.COMPLETED:
            return record.document_id == current_doc.id
        # Append mode keeps a version that was current when AI extraction began.
        return (current_doc.status == Document.Status.SUPERSEDED
                and record.feed.write_mode == "append")

    doc = Document.objects.select_related("kb", "kb__parent").filter(id=doc_id).first()
    if doc is None:
        return False
    from .spreadsheet_rows import is_table_query_kb
    if is_table_query_kb(doc.kb):
        return False
    record = FeedRecord.objects.select_related("feed").filter(kb_id=doc.kb_id).first()
    if not extraction_allowed(doc, record):
        return False
    if tracker_id is not None:
        tracker = KbTracker.objects.filter(id=tracker_id).first()
    else:
        # 自动钩子：按“就近且启用”解析归属表——子库启用中的 tracker 优先，
        # 停用的不遮蔽（父文件夹表可覆盖其文档），与补录的过滤语义一致
        tracker = None
        for cand in (getattr(doc.kb, "tracker", None),
                     getattr(doc.kb.parent, "tracker", None)):
            if cand is not None and cand.enabled and cand.fields:
                tracker = cand
                break
    if tracker is None:
        return False

    row, _created = TrackerRow.objects.get_or_create(
        tracker=tracker, document=doc,
        defaults={"status": TrackerRow.Status.PENDING},
    )
    from django.utils import timezone as _tz
    # updated_at 必须随认领刷新：否则补录翻出的旧失败行（updated_at 可能是
    # 几天前）会被页面 GET 的 20 分钟回收逻辑当场误杀（在抽却被标"中断"）
    claimed = TrackerRow.objects.filter(
        id=row.id, status__in=[TrackerRow.Status.PENDING, TrackerRow.Status.FAILED],
    ).update(status=TrackerRow.Status.RUNNING, error="", updated_at=_tz.now())
    if not claimed:
        return False  # 已有进行中的抽取

    # 重试场景：把旧值带进提示词（保旧优先），差异走人工确认
    prior = dict(row.values) if row.values else None

    try:
        prompt = build_extraction_prompt(
            doc.kb.name, tracker.fields, doc.md_content or "",
            tracker.instruction, prior_values=prior)
        data = parse_llm_json(_llm_invoke(prompt))
        values = {}
        for f in tracker.fields:
            v = data.get(f["label"])
            if isinstance(v,(dict,list)):
                text=json.dumps(v,ensure_ascii=False,separators=(',',':'),allow_nan=False)
                if len(text)>_MAX_CELL_CHARS:
                    raise ValueError(f"字段「{f['label']}」的 JSON 超过 {_MAX_CELL_CHARS} 字符，请拆分字段后重试；未保存截断的 JSON。")
                values[f["label"]]=text
            else:
                values[f["label"]]=str(v).strip()[:_MAX_CELL_CHARS] if v is not None else ""

        # 终态写入均为 compare-and-set：仅当行仍处于 running（本次认领未被
        # 抢走）才落库，防止并发写入方互相覆盖（输家静默放弃）。
        from django.db import transaction
        from django.utils import timezone as _tz
        with transaction.atomic():
            # Serialize the terminal write with document-version publication.
            # Otherwise an old AI call can recreate an upsert row after its
            # document was replaced while the model was still responding.
            current_doc = Document.objects.get(pk=doc.pk)
            record = (FeedRecord.objects.select_for_update().select_related("feed")
                      .filter(kb_id=doc.kb_id).first())
            if not extraction_allowed(current_doc, record):
                TrackerRow.objects.filter(id=row.id, status=TrackerRow.Status.RUNNING).delete()
                return False
            if prior and any(values[k] != (prior.get(k) or "") for k in values):
                n = TrackerRow.objects.filter(id=row.id, status=TrackerRow.Status.RUNNING).update(
                    proposed_values=values, status=TrackerRow.Status.PROPOSED,
                    error="", updated_at=_tz.now())
                return bool(n)
            n = TrackerRow.objects.filter(id=row.id, status=TrackerRow.Status.RUNNING).update(
                values=values, proposed_values={},
                status=TrackerRow.Status.DONE,
                schema_version=tracker.schema_version,
                error="", updated_at=_tz.now())
            return bool(n)
    except Exception as e:  # noqa: BLE001 —— 失败要落到行上可见可重试
        from django.utils import timezone as _tz
        TrackerRow.objects.filter(
            id=row.id, status=TrackerRow.Status.RUNNING).update(
            status=TrackerRow.Status.FAILED, error=str(e)[:280],
            updated_at=_tz.now())
        log.warning("追踪表抽取失败 doc=%s tracker=%s: %s",
                    doc_id, tracker.id, str(e)[:160])
        return False
    finally:
        # 后台线程用独立的 DB 连接，跑完即还（长驻会占 fd / 潜在锁）
        from django.db import connection
        connection.close()


def run_extraction_async(doc_id, tracker_id=None) -> None:
    """后台线程执行抽取（与 process_document_async 同款模式）。"""
    t = threading.Thread(target=run_extraction, args=(doc_id, tracker_id),
                         daemon=True)
    t.start()


def backfill_async(tracker: "KbTracker") -> int:
    """补录：为库内所有已完成、且尚无 done/proposed 行的文档排队抽取。

    排除子库自身带 tracker 的文档（与自动钩子的遮蔽语义一致：子库
    tracker 优先于父文件夹 tracker，避免同一文档在两张表重复登记）。
    返回排队的文档数；后台逐份执行（顺序，避免打爆本地 LLM）。
    """
    import django
    django.setup()
    from django.db import connection
    from .models import Document, KbTracker, TrackerRow

    # 遮蔽规则与自动钩子一致：子库 tracker 仅在「启用且已配置字段」时接管
    # 自己的文档（停用/空字段的子库 tracker 不遮蔽，父文件夹表可覆盖）
    shadowed_kb_ids = set(
        KbTracker.objects.filter(enabled=True)
        .exclude(fields=[]).exclude(id=tracker.id)
        .values_list("kb_id", flat=True))
    doc_ids = [
        did for did, kb_id in
        Document.objects.filter(
            kb__in=[tracker.kb_id] + list(
                tracker.kb.children.values_list("id", flat=True)),
            status=Document.Status.COMPLETED,
        ).exclude(tracker_rows__tracker=tracker,
                  tracker_rows__status__in=[TrackerRow.Status.DONE,
                                            TrackerRow.Status.PROPOSED],
        ).values_list("id", "kb_id")
        if kb_id not in shadowed_kb_ids
    ]

    def _worker():
        try:
            for did in doc_ids:
                try:
                    run_extraction(did, tracker.id)
                except Exception:
                    # 单份失败不拖垮整个队列（database is locked 等）
                    log.exception("补录单份失败，跳过 doc=%s", did)
        finally:
            connection.close()  # 后台线程独立连接，跑完即还

    if doc_ids:
        threading.Thread(target=_worker, daemon=True).start()
    return len(doc_ids)
