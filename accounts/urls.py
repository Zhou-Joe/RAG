"""accounts app: 登录/注册/登出/用户部门管理 URL"""
from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="accounts/login.html"), name="login"),
    # 登出必须 POST（Django 5+）；顶栏「退出」渲染成同款样式的 POST 按钮，点击即登出回首页
    path("logout/", auth_views.LogoutView.as_view(next_page="/"), name="logout"),
    path("register/", views.register_view, name="register"),
    path("profile/", views.profile, name="profile"),
    path("password/change/", views.password_change, name="password_change"),
    path("users/", views.user_list, name="user_list"),
]
