# accounts models：在 Django auth User 之外扩展 UserProfile（部门等字段）。
from __future__ import annotations

from django.conf import settings
from django.db import models

from kb.models import DEPARTMENT_GENERAL


class UserProfile(models.Model):
    """用户的扩展属性。OneToOne 挂在 auth User 上，避免替换自定义 User 模型。"""

    class Role(models.TextChoices):
        USER = "user", "普通用户"
        DEPT_ADMIN = "dept_admin", "部门管理员"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile",
        verbose_name="用户",
    )
    department = models.CharField(
        "部门", max_length=50, default=DEPARTMENT_GENERAL,
        help_text="「通用」可见所有标为通用的知识库；其它值额外可见同部门知识库。",
    )
    role = models.CharField(
        "账号级别", max_length=20, choices=Role.choices, default=Role.USER,
        help_text="部门管理员可管理本部门的共享知识库；所有用户可创建和管理仅本人可访问的个人知识库。",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "用户资料"
        verbose_name_plural = "用户资料"

    def __str__(self):
        return f"{self.user.username}（{self.department}·{self.get_role_display()}）"


class Department(models.Model):
    """显式登记的部门（管理员可提前创建/重命名，不必先挂到某个用户上）。

    部门池 = Department 表 ∪ 用户档案里的部门 ∪ 知识库的部门（见 kb.access.all_departments），
    旧数据（只有用户/库带部门、无登记行）继续有效。
    """

    name = models.CharField("部门名", max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "部门"
        verbose_name_plural = "部门"
        ordering = ["name"]

    def __str__(self):
        return self.name


def user_department(user) -> str:
    """取用户部门；无 Profile（老用户/未落库）时惰性补建并返回「通用」。"""
    if user is None or not getattr(user, "is_authenticated", False):
        return DEPARTMENT_GENERAL
    profile = getattr(user, "profile", None)
    if profile is None:
        from .models import UserProfile  # noqa: PLC0415  局部 import 防御
        profile = UserProfile.objects.create(user=user, department=DEPARTMENT_GENERAL)
    return profile.department or DEPARTMENT_GENERAL


def user_role(user) -> str:
    """取用户账号级别（user / dept_admin）；无 Profile 视为普通用户。"""
    if user is None or not getattr(user, "is_authenticated", False):
        return UserProfile.Role.USER
    profile = getattr(user, "profile", None)
    return profile.role if profile else UserProfile.Role.USER
