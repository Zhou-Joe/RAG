"""accounts context processors：向所有模板注入用户部门（可多个，且逐部门有级别）。"""
from __future__ import annotations


def user_department(request):
    """模板里可用的账号信息：

    - `user_department`：主部门（新建知识库落在它身上）
    - `user_departments`：全部部门（「通用」永远第一，人人都有）
    - `user_department_roles`：逐部门 [{name, label, role_label, is_admin, is_primary}]
    - `user_department_label`：短标签（侧栏那种窄位置）
    - `user_is_manager` / `user_is_staff`
    """
    empty = {"user_department": "", "user_departments": [], "user_department_roles": [],
             "user_department_label": "", "user_is_manager": False, "user_is_staff": False}
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return empty
    try:
        from .models import (Role, user_department as _ud, user_departments as _uds,
                             profile_roles)
        from kb.models import DEPARTMENT_GENERAL
        dept = _ud(user)
        depts = _uds(user)
        is_staff = bool(user.is_staff or user.is_superuser)
        is_manager = is_staff
        if not is_manager:
            from .models import UserProfile, user_role
            is_manager = user_role(user) == UserProfile.Role.DEPT_ADMIN
        labels = dict(Role.choices)
        roles = profile_roles(user)
        rows = [{
            "name": name,
            "role": roles.get(name, Role.USER),
            "role_label": labels.get(roles.get(name, Role.USER), ""),
            "is_admin": roles.get(name) == Role.DEPT_ADMIN,
            "is_primary": name == dept,
            "label": f"{name}（{labels.get(roles.get(name, Role.USER), '')}）",
        } for name in depts]
        others = [d for d in depts if d != DEPARTMENT_GENERAL]
        return {"user_department": dept, "user_departments": depts,
                "user_department_roles": rows,
                # 顶栏等窄位置用：除了「通用」还有部门就只列那些（「通用」人人都有，不占地方）
                "user_department_label": "、".join(others or depts),
                "user_is_manager": is_manager, "user_is_staff": is_staff}
    except Exception:
        return empty
