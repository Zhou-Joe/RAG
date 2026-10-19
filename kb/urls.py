"""kb app URL 配置。"""
from django.contrib.auth.decorators import login_required
from django.urls import path

from . import views

app_name = "kb"

urlpatterns = [
    # 管理页面（仅 staff）
    path("manage/", views.manage_list, name="manage_list"),
    path("manage/<slug:slug>/", views.manage_detail, name="manage_detail"),
    path("manage/<slug:slug>/tracker/", views.tracker_view, name="tracker"),
    path("manage/<slug:slug>/delete/", views.manage_delete, name="manage_delete"),
    path("manage/<slug:slug>/rename/", views.kb_rename, name="kb_rename"),
    path("manage/<slug:slug>/doc/<uuid:doc_id>/desc/", views.doc_desc_update, name="doc_desc_update"),
    path("manage/<slug:slug>/doc/<uuid:doc_id>/page-embed/", views.doc_page_embed, name="doc_page_embed"),
    path("manage/<slug:slug>/doc/<uuid:doc_id>/retry/", views.doc_retry, name="doc_retry"),
    path("manage/<slug:slug>/doc/<uuid:doc_id>/delete/", views.doc_delete, name="doc_delete"),
    path("manage/<slug:slug>/status/", views.doc_status_api, name="doc_status"),

    # 文档查看（所有登录用户）
    path("doc/<uuid:doc_id>/html/", views.document_html, name="document_html"),
    path("doc/<uuid:doc_id>/slices/", views.document_slices, name="document_slices"),
    path("doc/<uuid:doc_id>/img/<str:name>", views.doc_image, name="doc_image"),

    # 证据面板（chunk 级定位：预览 JSON + 原 PDF 页渲染 PNG）
    path("evidence/<str:chunk_id>/preview/", views.evidence_preview, name="evidence_preview"),
    path("evidence/<str:chunk_id>/page.png", views.evidence_page_png, name="evidence_page"),

    # 站点配置（仅 staff）
    path("settings/", views.site_settings, name="settings"),
    path("settings/test/", views.settings_test, name="settings_test"),
    path("eval/", views.eval_panel, name="eval_panel"),

    path("conv/<str:thread_id>/stop/", views.conversation_stop, name="conv_stop"),

    path("conv/states/", views.conversation_states, name="conv_states"),
    path("conv/<str:thread_id>/read/", views.conversation_read, name="conv_read"),

    # 会话历史
    path("conv/<str:thread_id>/messages/", views.conversation_messages, name="conv_messages"),
    path("conv/<str:thread_id>/delete/", views.conversation_delete, name="conv_delete"),

    # 综合搜索（结构化跨表关联 + 手册全文）
    path("search/", views.search_view, name="search"),

    # 旧图纸关联地址（跳转至综合搜索）
    path("assets/", views.asset_lookup, name="asset_lookup"),

    # 检查项提取
    path("inspection/", views.inspection_list, name="inspection"),

    # 问答页面（所有登录用户）
    path("ask/", views.ask, name="ask"),
    path("stream/", views.chat_stream, name="stream"),

    # 默认入口 → 问答页
    path("", views.index, name="index"),
]
