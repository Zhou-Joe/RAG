"""数据模型：KnowledgeBase（向量库）+ Document（上传文档）。

每个 KnowledgeBase 对应一个独立的 Chroma collection（persist_directory = data/chroma/<slug>/）。
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


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
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="knowledge_bases",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # 缓存字段：已成功处理的文档数与向量数（仅文档库有实际值；文件夹聚合子库计算）
    doc_count = models.PositiveIntegerField("文档数", default=0)
    chunk_count = models.PositiveIntegerField("向量数", default=0)

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
    file = models.FileField("文件", upload_to="documents/")
    file_type = models.CharField("类型", max_length=10, default="pdf")
    md_content = models.TextField("OCR/提取的 Markdown", blank=True, default="")
    html_content = models.TextField("HTML 正文", blank=True, default="")
    html_built_at = models.DateTimeField("HTML 构建时间", null=True, blank=True)
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


class SiteConfig(models.Model):
    """站点服务配置（单行）。空字段回退到 .env / settings 默认值。

    通过前端设置页编辑；agent/检索/OCR 流水线通过 kb.config 读取「有效值」。
    """

    # ---- LLM ----
    llm_base_url = models.CharField("LLM Base URL", max_length=255, blank=True, default="")
    llm_api_key = models.CharField("LLM API Key", max_length=255, blank=True, default="")
    llm_model = models.CharField("LLM 模型", max_length=120, blank=True, default="")
    llm_temperature = models.FloatField("LLM 温度", null=True, blank=True)

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
        """从 dict 批量写入指定字段（默认全部）（用于从预设加载）。

        数值字段收到空串时规范化为 None，避免把 "" 写进 FloatField/IntegerField
        导致保存时报 'expected a number but got ""'。
        """
        fl = fields or [f for f, _t in _SITECONFIG_FIELDS]
        num_fields = {f for f, t in _SITECONFIG_FIELDS if t in ("int", "float")}
        for f in fl:
            if f in data:
                val = data[f]
                if f in num_fields and (val == "" or val is None):
                    val = None
                setattr(self, f, val)


# SiteConfig 的 (字段名, 类型) 列表，供 snapshot/apply 与预设共用
_SITECONFIG_FIELDS = [
    ("llm_base_url", "text"), ("llm_api_key", "text"), ("llm_model", "text"), ("llm_temperature", "float"),
    ("embedding_base_url", "text"), ("embedding_api_key", "text"), ("embedding_model", "text"), ("embedding_dimensions", "int"),
    ("kb_chunk_size", "int"), ("kb_chunk_overlap", "int"), ("kb_top_k", "int"),
    ("mineru_api_base", "text"), ("mineru_api_key", "text"), ("mineru_backend", "text"), ("mineru_lang", "text"),
]

# 分类 → 该分类包含的 SiteConfig 字段名。预设按分类独立保存/加载。
PRESET_CATEGORIES = {
    "llm": ["llm_base_url", "llm_api_key", "llm_model", "llm_temperature"],
    "embedding": ["embedding_base_url", "embedding_api_key", "embedding_model", "embedding_dimensions"],
    "retrieval": ["kb_chunk_size", "kb_chunk_overlap", "kb_top_k"],
    "mineru": ["mineru_api_base", "mineru_api_key", "mineru_backend", "mineru_lang"],
}


class ConfigPreset(models.Model):
    """分类配置预设：按服务分类（LLM/Embedding/检索/MinerU）各自保存命名预设。

    加载预设时只覆盖该分类的字段，不影响其它服务。
    """

    class Category(models.TextChoices):
        LLM = "llm", "LLM"
        EMBEDDING = "embedding", "向量模型"
        RETRIEVAL = "retrieval", "检索参数"
        MINERU = "mineru", "MinerU OCR"

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
    agent 的上下文记忆由 checkpointer 维护；这里仅为前端展示。
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "消息"
        verbose_name_plural = "消息"

    def __str__(self):
        return f"[{self.role}] {self.content[:30]}"
