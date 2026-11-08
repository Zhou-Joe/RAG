"""Django admin 注册。"""
from django.contrib import admin
from django.db.models import Q
from . import access

from .models import Document, KnowledgeBase, StructuredDataset, StructuredRecord


@admin.register(KnowledgeBase)
class KnowledgeBaseAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_folder", "department", "parent", "doc_count", "chunk_count", "created_at")
    list_filter = ("is_folder", "department")
    search_fields = ("name", "slug", "department")
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ("is_folder", "department")
    readonly_fields=('created_by',)
    def get_queryset(self,request):return super().get_queryset(request).filter(access.managed_q(request.user))
    def save_model(self,request,obj,form,change):
        if not change:obj.created_by=request.user
        super().save_model(request,obj,form,change)
    def formfield_for_foreignkey(self,db_field,request,**kwargs):
        if db_field.name=='parent':kwargs['queryset']=access.managed_kbs(request.user).filter(is_folder=True)
        return super().formfield_for_foreignkey(db_field,request,**kwargs)


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    def get_queryset(self,request):return super().get_queryset(request).filter(access.managed_q(request.user,'kb__'))
    def formfield_for_foreignkey(self,db_field,request,**kwargs):
        if db_field.name=='kb':kwargs['queryset']=access.managed_kbs(request.user)
        return super().formfield_for_foreignkey(db_field,request,**kwargs)
    list_display = ("original_name", "kb", "status", "chunk_count", "created_at")
    list_filter = ("status",)
    search_fields = ("original_name",)


@admin.register(StructuredDataset)
class StructuredDatasetAdmin(admin.ModelAdmin):
    def get_queryset(self,request):return super().get_queryset(request).filter(Q(kb__isnull=True)|access.managed_q(request.user,'kb__'))
    def formfield_for_foreignkey(self,db_field,request,**kwargs):
        if db_field.name=='kb':kwargs['queryset']=access.managed_kbs(request.user)
        return super().formfield_for_foreignkey(db_field,request,**kwargs)
    list_display = ("source_name", "kind", "row_count", "active", "imported_by", "created_at")
    list_filter = ("kind", "active")
    search_fields = ("name", "source_name")


@admin.register(StructuredRecord)
class StructuredRecordAdmin(admin.ModelAdmin):
    def get_queryset(self,request):return super().get_queryset(request).filter(Q(dataset__kb__isnull=True)|access.managed_q(request.user,'dataset__kb__'))
    def formfield_for_foreignkey(self,db_field,request,**kwargs):
        if db_field.name=='dataset':kwargs['queryset']=StructuredDataset.objects.filter(Q(kb__isnull=True)|access.managed_q(request.user,'kb__'))
        return super().formfield_for_foreignkey(db_field,request,**kwargs)
    list_display = ("drawing_no", "part_no", "part_name", "g_code", "scp_level", "dataset", "row_number")
    list_filter = ("dataset__kind",)
    search_fields = ("drawing_no", "part_no", "part_name", "g_code", "equipment", "apex_no")
