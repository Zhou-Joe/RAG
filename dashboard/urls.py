"""dashboard app URL"""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.urls import path

from . import views

# 与 kb 应用一致：登录 + staff 双重校验
_is_staff = user_passes_test(lambda u: u.is_staff)

app_name = "dashboard"

urlpatterns = [
    path("", login_required(_is_staff(views.index)), name="index"),
]
