"""数据模型：KnowledgeBase（向量库）+ Document（上传文档）。

每个 KnowledgeBase 对应一个独立的 Chroma collection（persist_directory = data/chroma/<slug>/）。
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

# 「通用」部门 = 所有部门可见。部门为自由文本，其余部门名由用户自定。
DEPARTMENT_GENERAL = "通用"


class KnowledgeBase(models.Model):
    """知识库节点。分两种：
    - 文件夹（is_folder=True, parent=None）：分组容器，不直接挂文档，无自身向量；
      搜索时扇出到所有子文档库。
    - 文档库（is_folder=False, parent=<folder>）：对应一个 Chroma collection，
      直接挂 Document，每份文档独占一个 collection，杜绝跨文档混杂。
    顶层无 parent 的文档库（旧数据）视为独立文档库。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("名称", max_length=100)
    slug = models.SlugField("标识", max_length=100, unique=True, allow_unicode=True)
    description = models.TextField("描述", blank=True, default="")
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True,
        related_name="children", verbose_name="父知识库",
    )
    is_folder = models.BooleanField("是否文件夹", default=False)
    # 部门可见性：「通用」= 所有部门可见；其它值 = 仅该部门可见。
    # 文件夹与其子库保持同值（改文件夹部门时级联更新子库，见 kb/access.py）。
    department = models.CharField(
        "部门", max_length=50, default=DEPARTMENT_GENERAL,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="knowledge_bases",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # 缓存字段：已成功处理的文档数与向量数（仅文档库有实际值；文件夹聚合子库计算）
    doc_count = models.PositiveIntegerField("文档数", default=0)
    chunk_count = models.PositiveIntegerField("向量数", default=0)

    # 向量来源标记：该库当前向量是用哪个 embedding 模型灌的（run_indexing 成功后自动盖章）。
    # 换模型后未重建的库，检索质量会劣化——这个标签让管理员一眼看出哪个库需要 reindex_clean。
    embedding_model = models.CharField("向量模型", max_length=200, blank=True, default="")
    embedding_dimensions = models.PositiveIntegerField("向量维度", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "知识库"
        verbose_name_plural = "知识库"

    def __str__(self):
        return self.name

    def aggregate_counts(self) -> tuple[int, int]:
        """文件夹：聚合所有子文档库的 doc_count/chunk_count。文档库：返回自身值。"""
        if not self.is_folder:
            return self.doc_count, self.chunk_count
        children = self.children.filter(is_folder=False)
        docs = sum(c.doc_count for c in children)
        chunks = sum(c.chunk_count for c in children)
        return docs, chunks

    def effective_embedding_model(self) -> str:
        """该节点向量的 embedding 模型名。文档库=自身；文件夹=子库一致时该模型，
        不一致=「多个模型」，无向量=空串。用于判断哪些库与当前配置脱节。"""
        if not self.is_folder:
            return self.embedding_model
        models = set(
            self.children.filter(is_folder=False).exclude(embedding_model="")
            .values_list("embedding_model", flat=True)
        )
        if len(models) == 1:
            return models.pop()
        return "多个模型" if models else ""

    def embedding_label(self) -> str:
        """管理页徽章文本：「模型名 · 维度」。"""
        if not self.is_folder:
            m, d = self.embedding_model, self.embedding_dimensions
        else:
            m = self.effective_embedding_model()
            d = None
            if m and m != "多个模型":
                d = self.children.filter(is_folder=False, embedding_model=m).values_list(
                    "embedding_dimensions", flat=True).first()
        if not m:
            return ""
        return f"{m} · {d}维" if d else m

    def child_doc_slugs(self) -> list[str]:
        """文件夹下所有文档库的 slug（用于检索扇出）。文档库返回 [self.slug]。"""
        if not self.is_folder:
            return [self.slug]
        return list(self.children.filter(is_folder=False).values_list("slug", flat=True))


class Document(models.Model):
    """用户上传到某个知识库的一份文档。"""

    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        OCR = "ocr", "OCR 中"
        INDEXING = "indexing", "向量化中"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kb = models.ForeignKey(KnowledgeBase, on_delete=models.CASCADE, related_name="documents")
    original_name = models.CharField("原始文件名", max_length=255)
    description = models.TextField(
        "描述", blank=True, default="",
        help_text="可选；会展示给 AI 帮其判断该文档与问题的相关性")
    file = models.FileField("文件", upload_to="documents/")
    file_type = models.CharField("类型", max_length=10, default="pdf")
    md_content = models.TextField("OCR/提取的 Markdown", blank=True, default="")
    html_content = models.TextField("HTML 正文", blank=True, default="")
    html_built_at = models.DateTimeField("HTML 构建时间", null=True, blank=True)
    # 视觉文档模式（用户按文档勾选，图纸/扫描件用）：PDF 每页渲染成图 +
    # 页面文本锚定作为多模态块入库（WeMM VisDoc 强项）。纯 CAD 图纸页 OCR
    # 文本弱，整页视觉表示才能被文本查询命中；普通文本手册无需开启。
    page_embed = models.BooleanField("整页视觉入库", default=False)
    status = models.CharField(
        "状态", max_length=20, choices=Status.choices, default=Status.PENDING,
    )
    stage_detail = models.CharField("阶段详情", max_length=200, blank=True, default="")
    error_msg = models.TextField("错误信息", blank=True, default="")
    chunk_count = models.PositiveIntegerField("向量数", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "文档"
        verbose_name_plural = "文档"

    def __str__(self):
        return f"{self.original_name} ({self.get_status_display()})"

    # 文件类型徽章：按扩展名归类（pdf 红 / doc 蓝 / xls 绿 / txt 灰 / img 紫 / 其它 琥珀）
    _BADGE_KINDS = {
        "pdf": "pdf", "doc": "doc", "docx": "doc",
        "xls": "xls", "xlsx": "xls", "xlsm": "xls", "csv": "xls", "tsv": "xls",
        "md": "txt", "markdown": "txt", "txt": "txt",
        "jpg": "img", "jpeg": "img", "png": "img", "webp": "img",
    }

    @property
    def badge_kind(self) -> str:
        name = self.original_name or ""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else (self.file_type or "").lower()
        return self._BADGE_KINDS.get(ext, "file")

    @property
    def badge_label(self) -> str:
        return {"pdf": "PDF", "doc": "DOC", "xls": "XLS", "txt": "TXT",
                "img": "IMG"}.get(self.badge_kind, "FILE")


class ChunkProvenance(models.Model):
    """chunk 溯源：入库时从 MinerU content_list 解析出的版面定位（页码 + bbox）。

    每个向量块一行，chunk_id = Chroma 里的向量 id（文本块为 uuid，图片块为
    img-<doc_id>-<hash>）。引用点击后据此打开证据面板：渲染原 PDF 页并把
    bbox 画成红圈，实现「定位到第几页」。

    blocks 结构：[{"page": 43, "bbox": [x0, y0, x1, y1], "kind": "text|table|image",
                   "rows": [起行, 止行]  (仅表格)}]
    bbox 为 MinerU content_list 原生归一化坐标（0-1000，左上原点，整数），
    前端按渲染尺寸等比缩放即可，无需知道 PDF 页面物理大小。
    文档重跑入库时 chunk_id 全部重新生成 → 先删该文档旧行再写新行。
    """

    chunk_id = models.CharField("Chroma 向量 ID", max_length=160, unique=True)
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="provenance",
    )
    kb_slug = models.CharField("所属库 slug", max_length=100, blank=True, default="",
                               db_index=True)
    page_start = models.PositiveIntegerField("起始页（0 基）", default=0)
    page_end = models.PositiveIntegerField("结束页（0 基）", default=0)
    blocks = models.JSONField("定位块列表", default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "chunk 溯源"
        verbose_name_plural = "chunk 溯源"
        indexes = [models.Index(fields=["document", "page_start"])]

    def __str__(self):
        return f"{self.document.original_name} p{self.page_start}-{self.page_end}"

    def page_label(self) -> str:
        """人读页码（1 基；跨页显示区间）。"""
        if self.page_end > self.page_start:
            return f"第 {self.page_start + 1}-{self.page_end + 1} 页"
        return f"第 {self.page_start + 1} 页"


class KbTracker(models.Model):
    """知识库追踪表配置（一库一表）：文档入库完成后由 AI 按字段抽取关键信息登记。

    适用场景：定检报告、月度走账等同构工作流文档的持续登记与追溯。
    fields = [{"label": "检验日期"}, ...]（label 即列名即 JSON 键，所见即所得）。
    """

    kb = models.OneToOneField(
        KnowledgeBase, on_delete=models.CASCADE, related_name="tracker",
        verbose_name="知识库",
    )
    enabled = models.BooleanField("启用", default=False)
    fields = models.JSONField("追踪字段", default=list, blank=True)
    instruction = models.TextField(
        "补充提示", max_length=500, blank=True, default="",
        help_text="追加到抽取提示词，例如：结论只填 合格/不合格",
    )
    # 字段结构版本：配置里字段有任何增删改 → +1；行记录抽取时的版本，
    # 落后即说明该行是旧结构抽的（页面打「字段已变更」标，提示可重试补齐）
    schema_version = models.PositiveIntegerField("字段结构版本", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "追踪表"
        verbose_name_plural = "追踪表"

    def __str__(self):
        return f"{self.kb.name} 追踪表（{len(self.fields)} 字段）"


class TrackerRow(models.Model):
    """追踪表的一行 = 一份文档的抽取结果（同库同文档唯一，重试原地更新）。

    重试带旧值重抽（提示词要求保旧）：结果有变化 → 状态 proposed 挂起，
    proposed_values 存新值，等管理员在改前/改后对比弹窗里逐字段采纳；
    首次抽取或结果与旧值一致 → 直接 done。
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待抽取"
        RUNNING = "running", "抽取中"
        PROPOSED = "proposed", "待确认"
        DONE = "done", "已完成"
        FAILED = "failed", "失败"

    tracker = models.ForeignKey(
        KbTracker, on_delete=models.CASCADE, related_name="rows",
    )
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="tracker_rows",
    )
    values = models.JSONField("抽取值", default=dict, blank=True)
    proposed_values = models.JSONField("待确认的新值", default=dict, blank=True)
    schema_version = models.PositiveIntegerField("抽取时的字段结构版本", default=0)
    status = models.CharField(
        "状态", max_length=12, choices=Status.choices, default=Status.PENDING,
    )
    error = models.CharField("错误", max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-document__created_at"]
        unique_together = [("tracker", "document")]
        verbose_name = "追踪记录"
        verbose_name_plural = "追踪记录"

    def __str__(self):
        return f"{self.document.original_name} → {self.get_status_display()}"


class SiteConfig(models.Model):
    """站点服务配置（单行）。空字段回退到 .env / settings 默认值。

    通过前端设置页编辑；agent/检索/OCR 流水线通过 kb.config 读取「有效值」。
    """

    # ---- LLM ----
    llm_base_url = models.CharField("LLM Base URL", max_length=255, blank=True, default="")
    llm_api_key = models.CharField("LLM API Key", max_length=255, blank=True, default="")
    llm_model = models.CharField("LLM 模型", max_length=120, blank=True, default="")
    llm_temperature = models.FloatField("LLM 温度", null=True, blank=True)
    # 模型是否支持图片输入（视觉）。开启后 kb_search 命中图片时把图以
    # LangChain 多模态内容块随工具结果返回，模型可真正「看图」回答。
    llm_vision = models.BooleanField("LLM 支持图片输入", default=False)
    # 问答管线增强：回答前先做「问题理解/改写」（query_plan），回答后用
    # 第二次 LLM 调用对照检索证据核实结论（answer_verification），核实不过
    # 拒答。代价 = 每个问题多两次 LLM 调用。
    qa_enhance = models.BooleanField("问答管线增强（规划 + 核实）", default=False)

    # ---- Embedding ----
    embedding_base_url = models.CharField("Embedding Base URL", max_length=255, blank=True, default="")
    embedding_api_key = models.CharField("Embedding API Key", max_length=255, blank=True, default="")
    embedding_model = models.CharField("Embedding 模型", max_length=120, blank=True, default="")
    embedding_dimensions = models.PositiveIntegerField("Embedding 维度", null=True, blank=True)

    # ---- 检索参数 ----
    kb_chunk_size = models.PositiveIntegerField("分块大小", null=True, blank=True)
    kb_chunk_overlap = models.PositiveIntegerField("分块重叠", null=True, blank=True)
    kb_top_k = models.PositiveIntegerField("Top K", null=True, blank=True)

    # ---- MinerU OCR ----
    mineru_api_base = models.CharField("MinerU API Base", max_length=255, blank=True, default="")
    mineru_api_key = models.CharField("MinerU API Key", max_length=255, blank=True, default="")
    mineru_backend = models.CharField("MinerU Backend", max_length=60, blank=True, default="")
    mineru_lang = models.CharField("MinerU 语言", max_length=30, blank=True, default="")

    # ---- 重排序（混合召回后的精排，OpenAI/Jina 兼容 /v1/rerank 端点）----
    rerank_enabled = models.BooleanField("启用重排序", default=False)
    rerank_base_url = models.CharField("Rerank Base URL", max_length=255, blank=True, default="")
    rerank_api_key = models.CharField("Rerank API Key", max_length=255, blank=True, default="")
    rerank_model = models.CharField("Rerank 模型", max_length=200, blank=True, default="")

    # ---- 当前激活的预设名（展示用：配置页摘要卡显示「是哪个保存的配置」；
    #      手动编辑后清空，表示「自定义」；不属于预设快照字段） ----
    active_preset_llm = models.CharField(max_length=100, blank=True, default="")
    active_preset_embedding = models.CharField(max_length=100, blank=True, default="")
    active_preset_retrieval = models.CharField(max_length=100, blank=True, default="")
    active_preset_mineru = models.CharField(max_length=100, blank=True, default="")
    active_preset_rerank = models.CharField(max_length=100, blank=True, default="")

    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "站点配置"
        verbose_name_plural = "站点配置"

    def __str__(self):
        return "站点配置"

    def save(self, *args, **kwargs):
        """强制单行：保存前删除其它所有行。"""
        SiteConfig.objects.exclude(pk=self.pk).delete()
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "SiteConfig":
        obj = cls.objects.first()
        return obj if obj else cls.objects.create()

    def snapshot(self, fields: list[str] | None = None) -> dict:
        """把指定字段（默认全部）导出为 dict（用于存入预设）。"""
        fl = fields or [f for f, _t in _SITECONFIG_FIELDS]
        return {f: getattr(self, f) for f in fl}

    def apply(self, data: dict, fields: list[str] | None = None) -> None:
        """从 dict 批量写入指定字段（默认全部）（用于从预设加载）。"""
        fl = fields or [f for f, _t in _SITECONFIG_FIELDS]
        for f in fl:
            if f in data:
                setattr(self, f, data[f])


# SiteConfig 的 (字段名, 类型) 列表，供 snapshot/apply 与预设共用
_SITECONFIG_FIELDS = [
    ("llm_base_url", "text"), ("llm_api_key", "text"), ("llm_model", "text"), ("llm_temperature", "float"),
    ("embedding_base_url", "text"), ("embedding_api_key", "text"), ("embedding_model", "text"), ("embedding_dimensions", "int"),
    ("kb_chunk_size", "int"), ("kb_chunk_overlap", "int"), ("kb_top_k", "int"),
    ("mineru_api_base", "text"), ("mineru_api_key", "text"), ("mineru_backend", "text"), ("mineru_lang", "text"),
]

# 分类 → 该分类包含的 SiteConfig 字段名。预设按分类独立保存/加载。
PRESET_CATEGORIES = {
    "llm": ["llm_base_url", "llm_api_key", "llm_model", "llm_temperature", "llm_vision", "qa_enhance"],
    "embedding": ["embedding_base_url", "embedding_api_key", "embedding_model", "embedding_dimensions"],
    "retrieval": ["kb_chunk_size", "kb_chunk_overlap", "kb_top_k"],
    "mineru": ["mineru_api_base", "mineru_api_key", "mineru_backend", "mineru_lang"],
    "rerank": ["rerank_enabled", "rerank_base_url", "rerank_api_key", "rerank_model"],
}


class EvalQuestion(models.Model):
    """检索质量回归问题集（评估面板用）。

    换 embedding 模型 / 改分块 / 调检索参数前后各跑一次评估，对比命中情况，
    防止「改了配置检索悄悄变差」（2026-09 换 WeMM 时只能手工抽查的教训）。
    """

    question = models.TextField("问题", help_text="模拟真实检索的问法")
    # 期望命中的文档文件名（可选；命中 = 结果片段的 source 含此子串）
    expected_source = models.CharField("期望文档", max_length=255, blank=True, default="")
    # 期望命中片段正文需包含的关键词（可选）
    expected_keyword = models.CharField("期望关键词", max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "评估问题"
        verbose_name_plural = "评估问题"
        ordering = ["-created_at"]

    def __str__(self):
        return self.question[:40]


class ConfigPreset(models.Model):
    """分类配置预设：按服务分类（LLM/Embedding/检索/MinerU）各自保存命名预设。

    加载预设时只覆盖该分类的字段，不影响其它服务。
    """

    class Category(models.TextChoices):
        LLM = "llm", "LLM"
        EMBEDDING = "embedding", "向量模型"
        RETRIEVAL = "retrieval", "检索参数"
        MINERU = "mineru", "MinerU OCR"
        RERANK = "rerank", "重排序"

    name = models.CharField("名称", max_length=80)
    category = models.CharField("分类", max_length=20, choices=Category.choices, default=Category.LLM)
    # 仅存该分类的字段快照
    data = models.JSONField("配置快照", default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "name"]
        verbose_name = "配置预设"
        verbose_name_plural = "配置预设"

    def __str__(self):
        return f"[{self.get_category_display()}] {self.name}"


class Conversation(models.Model):
    """一次问答会话（多轮上下文）。

    thread_id 同时是 langgraph checkpointer 的 thread_id，
    选中同一 thread_id 即接续同一会话的上下文（持久化于 checkpoints.sqlite3）。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations",
    )
    kb = models.ForeignKey(KnowledgeBase, on_delete=models.CASCADE, related_name="conversations")
    title = models.CharField("标题", max_length=120, default="新对话")
    thread_id = models.CharField("会话线程", max_length=80, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "会话"
        verbose_name_plural = "会话"

    def __str__(self):
        return f"{self.title} ({self.user.username})"


class Message(models.Model):
    """会话内的一条消息（用户/助手），用于 UI 渲染历史。
    仅已完成回复保存为 AI 消息；下轮从此处取有界历史，不重放旧工具状态。
    """

    class Role(models.TextChoices):
        USER = "user", "用户"
        AI = "ai", "助手"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages",
    )
    role = models.CharField("角色", max_length=10, choices=Role.choices)
    content = models.TextField("内容")
    # AI 消息引用的来源出处（每条含 doc_id/source/highlights），供前端渲染可点击链接
    citations = models.JSONField("来源出处", default=list, blank=True)
    verified = models.BooleanField("已核对发布", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "消息"
        verbose_name_plural = "消息"

    def __str__(self):
        return f"[{self.role}] {self.content[:30]}"


class StructuredDataset(models.Model):
    """一次 CSV/XLSX 导入。

    同一类型、同一文件名的新导入会把旧版本标为非活动，保留历史但只查询最新版。
    """

    class Kind(models.TextChoices):
        DOWNTIME = "downtime", "Downtime 停机"
        APEX = "apex", "APEX"
        PARTS = "parts", "部件信息"
        DRAWING = "drawing", "图纸 / G-code / SCP"
        OTHER = "other", "其它关联表"

    name = models.CharField("数据集名称", max_length=160)
    kind = models.CharField("数据类型", max_length=20, choices=Kind.choices)
    source_name = models.CharField("来源文件", max_length=255)
    checksum = models.CharField("文件校验值", max_length=64, db_index=True)
    row_count = models.PositiveIntegerField("数据行数", default=0)
    mapping = models.JSONField("识别到的字段映射", default=dict, blank=True)
    active = models.BooleanField("当前版本", default=True, db_index=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="structured_datasets",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["kind", "active"])]
        verbose_name = "结构化数据集"
        verbose_name_plural = "结构化数据集"

    def __str__(self):
        return f"{self.get_kind_display()} · {self.source_name}"


class StructuredRecord(models.Model):
    """结构化表格中的一行；常用关联键单独索引，其余原列完整保存在 raw_data。"""

    dataset = models.ForeignKey(
        StructuredDataset, on_delete=models.CASCADE, related_name="records",
    )
    sheet_name = models.CharField("工作表", max_length=120, blank=True, default="")
    row_number = models.PositiveIntegerField("原始行号", default=0)
    drawing_no = models.CharField("图纸号", max_length=160, blank=True, default="")
    drawing_no_norm = models.CharField(max_length=160, blank=True, default="", db_index=True)
    part_no = models.CharField("部件号", max_length=160, blank=True, default="")
    part_no_norm = models.CharField(max_length=160, blank=True, default="", db_index=True)
    part_name = models.CharField("部件名称", max_length=255, blank=True, default="")
    part_name_norm = models.CharField(max_length=255, blank=True, default="", db_index=True)
    g_code = models.CharField("G-code", max_length=160, blank=True, default="")
    g_code_norm = models.CharField(max_length=160, blank=True, default="", db_index=True)
    scp_level = models.CharField("SCP 等级", max_length=80, blank=True, default="")
    equipment = models.CharField("设备/资产", max_length=200, blank=True, default="")
    equipment_norm = models.CharField(max_length=200, blank=True, default="", db_index=True)
    apex_no = models.CharField("APEX 编号", max_length=160, blank=True, default="")
    apex_no_norm = models.CharField(max_length=160, blank=True, default="", db_index=True)
    raw_data = models.JSONField("原始行数据", default=dict)

    class Meta:
        ordering = ["dataset_id", "sheet_name", "row_number"]
        indexes = [models.Index(fields=["dataset", "row_number"])]
        verbose_name = "结构化数据记录"
        verbose_name_plural = "结构化数据记录"

    def __str__(self):
        key = self.drawing_no or self.part_no or self.g_code or self.equipment
        return key or f"{self.dataset.source_name}:{self.row_number}"
