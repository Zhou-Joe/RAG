"""accounts admin：部门、部门身份（逐部门级别）与用户资料。"""
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (DEPARTMENT_GENERAL, Department, Role, UserDepartment, UserProfile,
                     department_admins, department_members, profile_departments,
                     profile_roles, sync_profile_role)


class UserDepartmentForm(forms.ModelForm):
    """部门身份行：「通用」那一行不许删（删了等于把该账号从公共库里摘出去）。"""

    class Meta:
        model = UserDepartment
        fields = ("department", "role")

    def clean(self):
        data = super().clean()
        # DELETE 是 formset 注入到表单里的字段（can_delete=True），从 cleaned_data 读
        if data.get("DELETE") and self.instance.pk and Department.objects.filter(
                pk=self.instance.department_id, name=DEPARTMENT_GENERAL).exists():
            raise forms.ValidationError("「通用」是人人默认的可见范围，不能删。")
        return data


class UserDepartmentInline(admin.TabularInline):
    """一个账号在各部门里的级别（管理权就按这一行一行的级别算）。"""

    model = UserDepartment
    form = UserDepartmentForm
    extra = 1
    autocomplete_fields = ("department",)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "user_count", "admin_count", "kb_count", "created_at")
    search_fields = ("name",)

    @admin.display(description="用户数")
    def user_count(self, obj):
        return department_members(obj.name).count()

    @admin.display(description="管理员数")
    def admin_count(self, obj):
        return department_admins(obj.name).count()

    @admin.display(description="知识库数")
    def kb_count(self, obj):
        from kb.models import KnowledgeBase
        return KnowledgeBase.objects.filter(department=obj.name).count()

    def has_delete_permission(self, request, obj=None):
        # 「通用」是人人默认的可见范围，删掉它等于把公共库从全站摘掉
        return bool(super().has_delete_permission(request, obj)
                    and getattr(obj, "name", None) != DEPARTMENT_GENERAL)


class UserProfileForm(forms.ModelForm):
    """档案表单：部门集合由「部门身份」行决定，这里只管主部门。"""

    class Meta:
        model = UserProfile
        fields = ("user", "department")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        primary = self.fields.get("department")
        if primary is not None:
            primary.help_text = ("主部门：新建知识库 / 个人库落在它身上。可见范围看下方"
                                 "「部门身份」，管理权看每一行里的级别。")


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = "用户资料（主部门 / 部门身份）"
    form = UserProfileForm
    readonly_fields = ("departments_overview",)
    fields = ("department", "departments_overview")
    # 逐部门级别要两跳（User → UserProfile → UserDepartment），Django 不支持嵌套
    # inline，只能在「用户资料」页或站内 /accounts/users/ 里改。
    extra = 0

    @admin.display(description="部门身份（逐部门级别）")
    def departments_overview(self, obj):
        if not obj or not obj.pk:
            return "保存后可在「用户资料」页设置"
        roles = profile_roles(obj)
        return "、".join(f"{name}（{'部门管理员' if role == Role.DEPT_ADMIN else '成员'}）"
                         for name, role in roles.items()) or DEPARTMENT_GENERAL


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    form = UserProfileForm
    inlines = [UserDepartmentInline]
    list_display = ("user", "primary_department", "all_departments", "admin_of", "role",
                    "updated_at")
    list_filter = ("department", "memberships__role", "memberships__department__name")
    search_fields = ("user__username", "department")
    # 主部门与级别都不做 list_editable：改主部门＝改公共库归属，改级别要落到具体部门，
    # 都得进表单明确做。

    @admin.display(description="主部门", ordering="department")
    def primary_department(self, obj):
        return obj.department or DEPARTMENT_GENERAL

    @admin.display(description="所属部门")
    def all_departments(self, obj):
        return "、".join(profile_departments(obj)) or DEPARTMENT_GENERAL

    @admin.display(description="当管理员的部门")
    def admin_of(self, obj):
        admin_depts = [n for n, r in profile_roles(obj).items() if r == Role.DEPT_ADMIN]
        return "、".join(admin_depts) or "—"

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        profile = form.instance
        # 兜底：任何路径存进来的档案都至少挂着「通用」（成员级别），并且汇总级别同步
        from .models import assign_departments
        if not profile.memberships.filter(department__name=DEPARTMENT_GENERAL).exists():
            assign_departments(profile.user, profile_departments(profile),
                               primary=profile.department)
        else:
            sync_profile_role(profile)


# 在 Django 自带 UserAdmin 里内联显示 Profile（改用户主部门更顺手）
UserAdmin.inlines = list(UserAdmin.inlines) + [UserProfileInline]
