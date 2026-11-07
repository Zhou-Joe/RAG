"""fos_rag URL Configuration."""
from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

from kb import views
from .interface_language import catalogue

urlpatterns = [
    path("i18n/catalog.js", catalogue, name="interface_catalogue"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("kb/", include("kb.urls")),
    path("dashboard/", include("dashboard.urls")),
    path("tools/operations/", include("operations_workbench.urls")),
    path("tools/", include("vehicle_tracker.urls")),
    # 首页：简单导航
    path("", views.home, name="home"),
    # 设计原型（可移除）
    path("prototype/", TemplateView.as_view(template_name="prototype.html"), name="prototype"),
]
