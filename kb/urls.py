"""kb app URL 配置。"""
from django.contrib.auth.decorators import login_required
from django.urls import path

from . import graph_views, hierarchy_views, feed_views, connector_views, tracker_analysis_views, feed_kb_views, kb_link_views, pin_views, views

from . import report_views, analysis_exports

app_name = "kb"

urlpatterns = [
    path('analysis/files/<str:token>/', analysis_exports.download_file, name='analysis_file'),
    path('feeds/<uuid:feed_id>/report/schedule/',report_views.schedule_control,name='report_schedule_control'),
    path('feeds/<uuid:feed_id>/report/settings/',report_views.configuration,name='report_configuration'),
    path('feeds/<uuid:feed_id>/report/configure/',report_views.configure,name='report_configure'),
    path('feeds/reports/<uuid:job_id>/test/',report_views.test_send,name='report_test'),
    path('feeds/reports/<uuid:job_id>/preview.xlsx',report_views.preview_download,name='report_preview_download'),
    path('reports/download/<str:token>/',report_views.public_download,name='report_public_download'),
    path('feeds/builder/',connector_views.index,name='connector_builder'),
    path('feeds/builder/create/',connector_views.create,name='connector_create'),
    path('feeds/builder/resume/',connector_views.resume,name='connector_resume'),
    path('feeds/builder/clear-context/',connector_views.clear_context,name='connector_clear_context'),
    path('feeds/builder/forget/',connector_views.forget,name='connector_forget'),
    path('feeds/builder/test-auth/',connector_views.test_auth,name='connector_test_auth'),
    path('feeds/builder/<uuid:job_id>/',connector_views.status,name='connector_status'),
    path('feeds/builder/<uuid:job_id>/thread/',connector_views.thread,name='connector_thread'),
    path('feeds/builder/<uuid:job_id>/download/',connector_views.download,name='connector_download'),
    path('feeds/builder/<uuid:job_id>/enable/',connector_views.enable,name='connector_enable'),
    path('feeds/builder/<uuid:job_id>/credentials/',connector_views.credentials,name='connector_credentials'),
    path('feeds/builder/<uuid:job_id>/cancel/',connector_views.cancel,name='connector_cancel'),
    path('feeds/builder/<uuid:job_id>/refine/',connector_views.refine,name='connector_refine'),
    path('feeds/builder/<uuid:job_id>/browser.png',connector_views.browser_preview,name='connector_browser_preview'),
    path('feeds/',feed_views.index,name='feeds'),
    path('feeds/state/',feed_views.state,name='feed_state'),
    path('feeds/action/',feed_views.action,name='feed_action'),
    path('feeds/kit.zip',feed_views.kit,name='feed_kit'),
    path('api/feeds/<uuid:feed_id>/ingest/',feed_views.ingest,name='feed_ingest'),
    path('api/feeds/<uuid:feed_id>/runs/<uuid:run_id>/',feed_views.run_status,name='feed_run_status'),
    path("graph/", graph_views.graph_view, name="graph"),
    path("graph/action/", graph_views.graph_action, name="graph_action"),
    path("graph/extraction/<int:job_id>/events/", graph_views.graph_events, name="graph_events"),
    path("graph/fact/<str:relation_id>/", graph_views.graph_fact, name="graph_fact"),
    path("graph/structure/", hierarchy_views.structure, name="knowledge_structure"),
    path("graph/structure/action/", hierarchy_views.action, name="knowledge_action"),
    path("graph/relation/<int:relation_id>/", graph_views.graph_relation, name="graph_relation"),
    # 知识库管理（共享库管理员或个人库创建者）
    path("manage/", views.manage_list, name="manage_list"),
    path("manage/<slug:slug>/pin/", pin_views.kb_pin, name="kb_pin"),
    path("manage/connections/state/", kb_link_views.connections, name="kb_connections"),
    path("manage/<slug:slug>/", views.manage_detail, name="manage_detail"),
    path("manage/<slug:slug>/feed-data/", feed_kb_views.feed_data, name="feed_kb_data"),
    path("manage/<slug:slug>/tracker-data/", feed_kb_views.tracker_data, name="tracker_kb_data"),
    path("manage/<slug:slug>/tracker/", views.tracker_view, name="tracker"),
    path("manage/<slug:slug>/tracker/analysis/", tracker_analysis_views.tracker_analysis, name="tracker_analysis"),
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
    path("chat/attachments/<uuid:attachment_id>/", views.chat_attachment, name="chat_attachment"),

    # 浏览器沙箱执行结果回报（write_analysis 自修复回路）
    path("analysis/report/", views.analysis_report, name="analysis_report"),

    # 默认入口 → 问答页
    path("", views.index, name="index"),
]
