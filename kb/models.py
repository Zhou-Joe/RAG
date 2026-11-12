"""数据模型：KnowledgeBase（向量库）+ Document（上传文档）。

每个 KnowledgeBase 对应一个独立的 Chroma collection（persist_directory = data/chroma/<slug>/）。
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models, transaction

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
    category = models.CharField('资料分类', max_length=16,
        choices=[('manual','手册库'),('business','业务数据')], default='manual')
    # None follows the complete schema; an explicit list is presentation-only.
    active_fields = models.JSONField("展示字段", null=True, blank=True, default=None)
    # 部门可见性：「通用」= 所有部门可见；其它值 = 仅该部门可见。
    # 文件夹与其子库保持同值（改文件夹部门时级联更新子库，见 kb/access.py）。
    department = models.CharField(
        "部门", max_length=50, default=DEPARTMENT_GENERAL,
    )
    visibility = models.CharField('访问范围',max_length=16,choices=[('shared','通用/部门'),('individual','个人（仅本人）')],default='shared',db_index=True)
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

    @property
    def access_level(self):
        root=self.parent if self.parent_id else self
        return 'individual' if root.visibility=='individual' else 'general' if root.department==DEPARTMENT_GENERAL else 'department'

    @property
    def access_label(self):
        return '个人（仅本人）' if self.access_level=='individual' else self.department

    @transaction.atomic
    def save(self,*args,**kwargs):
        name_update=kwargs.get('update_fields') is None or 'name' in kwargs['update_fields']
        previous_name=(type(self).objects.filter(pk=self.pk).values_list('name',flat=True).first()
                       if name_update and not self._state.adding else self.name)
        old=None
        scope_update=kwargs.get('update_fields') is None or bool(set(kwargs['update_fields'])&{'visibility','department'})
        if self.is_folder and not self._state.adding and scope_update:
            old=type(self).objects.filter(pk=self.pk).values_list('visibility','department').first()
        if self.parent_id:
            parent=self.parent;self.visibility=parent.visibility
            if parent.visibility=='individual':self.department=parent.department
            if kwargs.get('update_fields') is not None:kwargs['update_fields']=set(kwargs['update_fields'])|{'visibility'}|({'department'} if parent.visibility=='individual' else set())
        super().save(*args,**kwargs)
        if old is not None and old!=(self.visibility,self.department):
            self.children.update(visibility=self.visibility,department=self.department)
        if previous_name!=self.name:
            from .feed_names import sync_related_names
            sync_related_names(self)

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


class KnowledgeBasePin(models.Model):
    """A user's private shortcut to a visible top-level knowledge base."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="knowledge_base_pins")
    kb = models.ForeignKey(
        KnowledgeBase, on_delete=models.CASCADE, related_name="user_pins")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "kb"], name="unique_user_kb_pin")]


class Document(models.Model):
    """用户上传到某个知识库的一份文档。"""

    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        OCR = "ocr", "OCR 中"
        INDEXING = "indexing", "向量化中"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失败"
        SUPERSEDED = "superseded", "历史版本"

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
        "eml": "mail", "msg": "mail",
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
                "img": "IMG", "mail": "MAIL"}.get(self.badge_kind, "FILE")


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


class KnowledgeBaseLink(models.Model):
    """顶层知识库之间的无向关联；关系说明仅供选库，不视为业务事实证据。"""

    left = models.ForeignKey(KnowledgeBase, on_delete=models.CASCADE, related_name='links_as_left')
    right = models.ForeignKey(KnowledgeBase, on_delete=models.CASCADE, related_name='links_as_right')
    summary = models.CharField('关系说明', max_length=1000, blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['pk']
        constraints = [
            models.UniqueConstraint(fields=['left', 'right'], name='unique_kb_link_pair'),
            models.CheckConstraint(condition=models.Q(left__lt=models.F('right')), name='ordered_distinct_kb_link'),
        ]
        verbose_name = '知识库关联'
        verbose_name_plural = '知识库关联'


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
        Document, on_delete=models.CASCADE, related_name="tracker_rows", null=True, blank=True,
    )
    feed = models.ForeignKey('KnowledgeFeed',null=True,blank=True,on_delete=models.CASCADE,related_name='tracker_rows')
    feed_run = models.ForeignKey('FeedRun',null=True,blank=True,on_delete=models.RESTRICT,related_name='tracker_rows')
    external_id = models.CharField(max_length=200,blank=True,default='')
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
        constraints = [
            models.UniqueConstraint(fields=['tracker','feed','external_id'],condition=models.Q(feed_run__isnull=True),name='unique_tracker_feed_current'),
            models.UniqueConstraint(fields=['tracker','feed','external_id','feed_run'],name='unique_tracker_feed_snapshot'),
        ]
        verbose_name = "追踪记录"
        verbose_name_plural = "追踪记录"

    def __str__(self):
        return f"{self.record_name} → {self.get_status_display()}"

    @property
    def record_name(self):
        return self.document.original_name if self.document_id else str(self.values.get('标题') or self.external_id or '数据记录')


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
    all_remote_apis_enabled = models.BooleanField("允许全部远程 API", default=False)
    llm_remote_enabled = models.BooleanField("允许远程回答 API", default=False)
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
        if "llm_remote_enabled" in fl and "llm_remote_enabled" not in data:
            self.llm_remote_enabled = False
        for f in fl:
            if f in data:
                setattr(self, f, data[f])


# SiteConfig 的 (字段名, 类型) 列表，供 snapshot/apply 与预设共用
_SITECONFIG_FIELDS = [
    ("llm_remote_enabled", "bool"),
    ("llm_base_url", "text"), ("llm_api_key", "text"), ("llm_model", "text"), ("llm_temperature", "float"),
    ("embedding_base_url", "text"), ("embedding_api_key", "text"), ("embedding_model", "text"), ("embedding_dimensions", "int"),
    ("kb_chunk_size", "int"), ("kb_chunk_overlap", "int"), ("kb_top_k", "int"),
    ("mineru_api_base", "text"), ("mineru_api_key", "text"), ("mineru_backend", "text"), ("mineru_lang", "text"),
]

# 分类 → 该分类包含的 SiteConfig 字段名。预设按分类独立保存/加载。
PRESET_CATEGORIES = {
    "llm": ["llm_remote_enabled", "llm_base_url", "llm_api_key", "llm_model", "llm_temperature", "llm_vision", "qa_enhance"],
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

    thread_id 标识会话；agent_context 保存最近完成的完整模型消息和工具上下文。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations",
    )
    # Legacy access restriction only; new general chats leave this unset.
    kb = models.ForeignKey(KnowledgeBase, on_delete=models.CASCADE, related_name="conversations",null=True,blank=True)
    title = models.CharField("标题", max_length=120, default="新对话")
    thread_id = models.CharField("会话线程", max_length=80, unique=True)
    last_read_answer_id = models.PositiveBigIntegerField(default=0)
    relationship_origin = models.UUIDField('关联检索起点', null=True, blank=True, editable=False)
    agent_context = models.JSONField("模型会话上下文", default=dict, blank=True, editable=False)
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
    完成的模型消息和工具结果保存在 Conversation.agent_context；这里保留界面消息。
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
    completion_status = models.CharField("回答状态", max_length=12, default="complete", choices=[("complete", "完整"), ("incomplete", "未完成"), ("queued", "排队中"), ("running", "生成中")])
    failure_reason = models.CharField("未完成原因", max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["conversation"], condition=models.Q(completion_status__in=["queued", "running"]), name="one_active_answer_per_conversation")]
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
    kb = models.ForeignKey(
        KnowledgeBase, on_delete=models.CASCADE, null=True, blank=True,
        related_name="structured_datasets", verbose_name="所属知识库",
    )
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


class GraphExtraction(models.Model):
    """文档自动抽取的进度与版本；失败可重试，已有审核结论保留。"""

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="graph_extractions")
    focus = models.TextField("本次关注方面", blank=True, default="")
    source_digest = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, default="pending")
    done = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    created_relations = models.PositiveIntegerField(default=0)
    rejected = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    coverage = models.JSONField("正文扫描覆盖情况", default=dict, blank=True)
    schema_version = models.PositiveSmallIntegerField("知识结构版本", default=1)
    target_object = models.ForeignKey('KnowledgeObject', on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class GraphExtractionEvent(models.Model):
    """自动抽取的可重放活动记录；模型文本仅作未审核输出展示。"""

    extraction = models.ForeignKey(GraphExtraction, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField("事件类型", max_length=24)
    payload = models.JSONField("事件内容", default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["extraction", "id"], name="graph_event_cursor")]
        verbose_name = "图谱抽取活动"
        verbose_name_plural = "图谱抽取活动"


class GraphEntity(models.Model):
    """领域无关实体；知识库、类型和业务作用域共同限定身份。"""

    kb = models.ForeignKey(KnowledgeBase, on_delete=models.CASCADE, related_name="graph_entities")
    kind = models.CharField("实体类型", max_length=60)
    namespace = models.CharField("业务作用域", max_length=255, default="")
    key = models.CharField("规范编号", max_length=255)
    label = models.CharField("显示名称", max_length=255)
    aliases = models.JSONField("别名", default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["kb", "kind", "namespace", "key"], name="graph_entity_identity")]
        verbose_name = "图谱实体"
        verbose_name_plural = "图谱实体"

    def __str__(self):
        return self.label


class GraphRelation(models.Model):
    """带审核状态的有向关系；独立于 embedding 和 chunk 身份。"""

    class Status(models.TextChoices):
        CANDIDATE = "candidate", "待核对"
        VERIFIED = "verified", "已核对"
        REJECTED = "rejected", "已拒绝"

    subject = models.ForeignKey(GraphEntity, on_delete=models.CASCADE, related_name="outgoing")
    predicate = models.CharField("关系类型", max_length=60)
    object = models.ForeignKey(GraphEntity, on_delete=models.CASCADE, related_name="incoming")
    identity = models.CharField("来源身份", max_length=64, unique=True)
    extraction = models.ForeignKey(GraphExtraction, on_delete=models.SET_NULL, null=True, blank=True)
    qualifiers = models.JSONField("适用条件", default=dict, blank=True)
    origin_record = models.ForeignKey(StructuredRecord, on_delete=models.CASCADE, null=True, blank=True)
    status = models.CharField("审核状态", max_length=20, choices=Status.choices, default=Status.CANDIDATE)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["status", "subject"])]
        verbose_name = "图谱关系"
        verbose_name_plural = "图谱关系"


class GraphEvidence(models.Model):
    """关系的原始证据；重建向量无需重建事实，来源变更则失效。"""

    relation = models.ForeignKey(GraphRelation, on_delete=models.CASCADE, related_name="evidence")
    record = models.ForeignKey(StructuredRecord, on_delete=models.CASCADE, null=True, blank=True)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, null=True, blank=True)
    quote = models.TextField("原文证据")
    chunks = models.JSONField("原文定位", default=list, blank=True)
    source_digest = models.CharField("来源版本", max_length=64)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=(models.Q(record__isnull=False, document__isnull=True)
                       | models.Q(record__isnull=True, document__isnull=False)),
            name="graph_evidence_one_source")]
        verbose_name = "图谱证据"
        verbose_name_plural = "图谱证据"


class KnowledgeObject(models.Model):
    """集合内稳定的对象身份；断言仍按原始知识库权限独立隔离。"""
    space = models.ForeignKey(KnowledgeBase, on_delete=models.SET_NULL, null=True, related_name='knowledge_objects')
    kind = models.CharField('对象类型', max_length=60)
    key = models.CharField('规范身份', max_length=255)
    label = models.CharField('对象名称', max_length=255)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['space', 'kind', 'key'], name='knowledge_object_identity')]


class KnowledgeNode(models.Model):
    """可递归的属性、分类项和组织分组，节点也可同时承载内容。"""
    object = models.ForeignKey(KnowledgeObject, on_delete=models.CASCADE, related_name='nodes')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    kind = models.CharField('节点角色', max_length=16, choices=[('property','属性'),('entry','分类项'),('group','分组')])
    label = models.CharField('节点名称', max_length=255)
    key = models.CharField('规范名称', max_length=255)
    scope = models.JSONField('适用上下文', default=dict)
    identity = models.CharField('路径身份', max_length=64, unique=True)


class KnowledgeAssertion(models.Model):
    """某一对象路径下的内容断言；新来源和新解释始终需要独立审核。"""
    node = models.ForeignKey(KnowledgeNode, on_delete=models.CASCADE, related_name='assertions')
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='knowledge_assertions')
    extraction = models.ForeignKey(GraphExtraction, on_delete=models.SET_NULL, null=True, blank=True)
    origin_relation = models.ForeignKey(GraphRelation, on_delete=models.SET_NULL, null=True, blank=True)
    identity = models.CharField(max_length=64, unique=True)
    subject_label = models.CharField('原文对象名称', max_length=255)
    path = models.JSONField('原文属性路径', default=list)
    value = models.JSONField('内容或取值')
    conditions = models.JSONField('条件与逻辑', default=dict)
    status = models.CharField(max_length=20, choices=GraphRelation.Status.choices, default=GraphRelation.Status.CANDIDATE)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['status','document'], name='knowledge_status_document')]


class KnowledgeEvidence(models.Model):
    assertion = models.ForeignKey(KnowledgeAssertion, on_delete=models.CASCADE, related_name='evidence')
    document = models.ForeignKey(Document, on_delete=models.CASCADE)
    quote = models.TextField('逐字证据')
    chunks = models.JSONField('原文定位', default=list)
    source_digest = models.CharField('来源版本', max_length=64)


class FeedCredential(models.Model):
    """页面录入的 API 凭据；数据库只留密文，密钥由部署自动保管。"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    kind = models.CharField('认证方式', max_length=20)
    api_origin = models.CharField('凭据接收主机', max_length=2000)
    header_names = models.JSONField('请求头名称', default=list)
    encrypted_value = models.TextField('加密凭据')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class KnowledgeFeed(models.Model):
    """向资料集合持续更新报告的数据源；调度与写入权限随创建者重验。"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kb = models.ForeignKey(KnowledgeBase, on_delete=models.CASCADE, related_name='feeds')
    name = models.CharField('数据源名称', max_length=100)
    mode = models.CharField('接入方式', max_length=10, choices=[('pull','定时拉取 API'),('push','脚本推送')], default='pull')
    write_mode = models.CharField('写入模式', max_length=10,
        choices=[('append','追加新记录（保留历史）'),('upsert','覆盖更新相同 ID')], default='append')
    url = models.URLField('API 地址', max_length=2000, blank=True)
    header_env = models.JSONField('请求头环境变量', default=dict, blank=True)
    credential = models.ForeignKey(FeedCredential, null=True, blank=True, on_delete=models.SET_NULL, related_name='feeds')
    adapter_spec = models.JSONField('API 转换配置', default=dict, blank=True)
    allow_private = models.BooleanField('允许内网 API', default=False)
    cron = models.CharField('Cron 表达式', max_length=100, default='0 * * * *')
    timezone = models.CharField('时区', max_length=100, default='Asia/Shanghai')
    enabled = models.BooleanField('启用', default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    token_digest = models.CharField('推送令牌摘要', max_length=64, blank=True)
    cursor = models.CharField('已提交游标', max_length=2048, blank=True)
    etag = models.CharField('HTTP 缓存标识', max_length=500, blank=True)
    next_run_at = models.DateTimeField('下次运行', null=True, blank=True, db_index=True)
    lease_token = models.CharField('执行锁', max_length=64, blank=True)
    lease_until = models.DateTimeField('锁有效期', null=True, blank=True)
    last_run_at = models.DateTimeField('最近运行', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self,*args,**kwargs):
        from .feed_names import current_name,named_spec
        fields=kwargs.get('update_fields')
        if fields is None or set(fields)&{'name','adapter_spec','kb','kb_id'}:
            self.name=current_name(self);self.adapter_spec=named_spec(self.adapter_spec,self.name)
            if fields is not None:kwargs['update_fields']=set(fields)|{'name','adapter_spec'}
        super().save(*args,**kwargs)


class FeedRecord(models.Model):
    """上游稳定 ID 的最近状态；追踪表可按采集批次保留多个历史快照。"""
    feed = models.ForeignKey(KnowledgeFeed, on_delete=models.CASCADE, related_name='records')
    external_id = models.CharField('上游 ID', max_length=200)
    kb = models.OneToOneField(KnowledgeBase, null=True, on_delete=models.SET_NULL, related_name='feed_record')
    document = models.ForeignKey(Document, null=True, on_delete=models.SET_NULL, related_name='+')
    pending_document = models.ForeignKey(Document, null=True, on_delete=models.SET_NULL, related_name='+')
    pending_digest = models.CharField(max_length=64, blank=True)
    content_digest = models.CharField(max_length=64, blank=True)
    title = models.CharField(max_length=200, blank=True)
    source_url = models.URLField(max_length=2000, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    upstream_updated_at = models.DateTimeField(null=True, blank=True)
    deleted = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['feed','external_id'], name='unique_feed_external_id')]


class FeedRun(models.Model):
    """可重放运行记录；只在整批发布成功后提交游标。"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    feed = models.ForeignKey(KnowledgeFeed, on_delete=models.CASCADE, related_name='runs')
    trigger = models.CharField(max_length=12, default='manual')
    status = models.CharField(max_length=15, default='running')
    request_key = models.CharField(max_length=180, blank=True)
    payload_digest = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    lease_token = models.CharField(max_length=64)
    counts = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=1000, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        constraints = [models.UniqueConstraint(fields=['feed','request_key'], condition=~models.Q(request_key=''), name='unique_feed_request_key')]


class FeedSchedulerState(models.Model):
    """调度器心跳，只用于管理页展示是否正在执行到期扫描。"""
    key = models.CharField(primary_key=True, max_length=50, default='default')
    heartbeat_at = models.DateTimeField()


class ConnectorDraft(models.Model):
    """有工具和调用预算的连接器生成草稿；审核启用前不执行定时更新。"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    kb = models.ForeignKey(KnowledgeBase, null=True, blank=True, on_delete=models.CASCADE)
    feed = models.ForeignKey(KnowledgeFeed, null=True, blank=True, on_delete=models.SET_NULL)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='revisions')
    feedback = models.TextField('预览调整要求', blank=True)
    response_language = models.CharField('回复语言',max_length=10,choices=[('zh-hans','简体中文'),('en','English')],default='zh-hans')
    prompt = models.TextField()
    api_url = models.URLField(max_length=2000, blank=True)
    header_env = models.JSONField(default=dict, blank=True)
    credential = models.ForeignKey(FeedCredential, null=True, blank=True, on_delete=models.SET_NULL, related_name='drafts')
    allow_private = models.BooleanField(default=False)
    sample = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, default='running')
    spec = models.JSONField(default=dict, blank=True)
    preview = models.JSONField(default=dict, blank=True)
    events = models.JSONField(default=list, blank=True)
    sources = models.JSONField(default=list, blank=True)
    questions = models.JSONField(default=list, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    browser_state = models.JSONField('浏览器取数状态', default=dict, blank=True)
    retain_context = models.BooleanField('随定时任务保留对话', default=False)
    created_via = models.CharField('创建入口', max_length=10, default='web', choices=[
        ('web', '采集助手页面'), ('chat', 'AI 助手对话'), ('api', '外部接口'), ('cli', '命令行')])
    context_snapshot = models.TextField('加密代理上下文', blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ConnectorEvent(models.Model):
    """采集活动的追加式日志，保留工具参数、耗时与结果，支持游标增量读取。"""
    job = models.ForeignKey(ConnectorDraft,on_delete=models.CASCADE,related_name='activity')
    kind = models.CharField(max_length=40)
    turn = models.PositiveIntegerField(default=0)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class FeedReportPlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    feed = models.OneToOneField(KnowledgeFeed, on_delete=models.CASCADE, related_name='report_plan')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    spec = models.JSONField(default=dict, blank=True)
    trigger_state = models.JSONField(default=dict, blank=True)
    destination = models.TextField(blank=True)
    schedule_mode = models.CharField(max_length=20, default='manual', choices=[('cron','独立定时'),('manual','按需发送'),('after_collection','采集后发送（兼容）')])
    cron = models.CharField(max_length=100, blank=True)
    timezone = models.CharField(max_length=64, default='Asia/Shanghai')
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self,*args,**kwargs):
        fields=kwargs.get('update_fields')
        if fields is None or 'spec' in fields:
            if isinstance(self.spec,dict) and self.spec.get('kind')=='report':
                from .feed_names import current_name,named_spec
                self.spec=named_spec(self.spec,current_name(self.feed))
        super().save(*args,**kwargs)


class FeedReportDelivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(FeedReportPlan, on_delete=models.CASCADE, related_name='deliveries')
    run = models.ForeignKey(FeedRun, null=True, blank=True, on_delete=models.SET_NULL)
    request_key = models.CharField(max_length=180)
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=20, default='pending')
    error = models.CharField(max_length=400, blank=True)
    filename = models.CharField(max_length=100, blank=True)
    content = models.BinaryField(default=bytes)
    token_digest = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['plan', 'request_key'], name='unique_feed_report_delivery')]
