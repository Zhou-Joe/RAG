"""Django admin 注册。"""
from django.contrib import admin

from .models import Document, KnowledgeBase


@admin.register(KnowledgeBase)
class KnowledgeBaseAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_folder", "parent", "doc_count", "chunk_count", "created_at")
    list_filter = ("is_folder",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ("is_folder",)


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("original_name", "kb", "status", "chunk_count", "created_at")
    list_filter = ("status", "kb")
    search_fields = ("original_name",)
