"""部门级知识库访问控制。

规则：
- 知识库 department == 「通用」 → 所有登录用户可见/可查。
- 否则仅 department 相同的用户（及 staff/superuser）可见。
- 文件夹与其子库部门保持同步（改文件夹时级联更新子库）。
"""
from __future__ import annotations

from django.db.models import Q, QuerySet

from .models import DEPARTMENT_GENERAL, Document, KnowledgeBase


def _user_dept(user) -> str:
    from accounts.models import user_department
    return user_department(user)


# 公开别名：views 等处直接 access.user_department(user)
def user_department(user) -> str:
    return _user_dept(user)


def is_global_admin(user) -> bool:
    """全局管理员（is_staff / is_superuser）：不受部门限制，可管一切 + 站点设置。"""
    return getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)


def is_manager(user) -> bool:
    """能否进入资料上传中心：全局管理员 或 部门管理员。"""
    from accounts.models import user_role, UserProfile
    return is_global_admin(user) or user_role(user) == UserProfile.Role.DEPT_ADMIN


def can_manage_kb(user, kb: KnowledgeBase) -> bool:
    """能否管理（改名/删库/上传/删文档）该知识库。

    - 全局管理员：全部
    - 部门管理员：仅本部门的库（通用库只有全局管理员能管）
    - 普通用户：不能
    """
    if is_global_admin(user):
        return True
    from accounts.models import user_role, UserProfile
    if user_role(user) != UserProfile.Role.DEPT_ADMIN:
        return False
    return kb.department == _user_dept(user)


def kb_q(user) -> Q:
    """用户可见知识库的 Q 条件（通用 ∪ 本部门）。"""
    return Q(department=DEPARTMENT_GENERAL) | Q(department=_user_dept(user))


def kb_accessible(user, kb: KnowledgeBase) -> bool:
    """该用户能否访问此知识库（staff/superuser 全放行）。"""
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    dept = _user_dept(user)
    return kb.department in (DEPARTMENT_GENERAL, dept)


def accessible_kbs(user) -> QuerySet[KnowledgeBase]:
    """用户可见的顶层知识库（parent__isnull；子库对用户透明）。"""
    return KnowledgeBase.objects.filter(parent__isnull=True).filter(kb_q(user))


def all_departments() -> list[str]:
    """已知部门池 = 显式登记的部门 ∪ 用户档案 ∪ 知识库 的部门去重，「通用」固定排第一。

    新部门的创建入口：全局管理员在用户管理页添加/重命名（重命名级联所有用户和库）；
    注册页和库表单只能从本池中选择（防止随意拼写出新部门）。
    """
    from accounts.models import Department, UserProfile
    depts = set(Department.objects.values_list("name", flat=True))
    depts.update(
        UserProfile.objects.exclude(department="").values_list("department", flat=True)
    )
    depts.update(
        KnowledgeBase.objects.exclude(department="").values_list("department", flat=True)
    )
    depts.discard(DEPARTMENT_GENERAL)
    return [DEPARTMENT_GENERAL] + sorted(depts)


def accessible_doc_slugs(user) -> set[str]:
    """用户可检索的（子）库 slug 集合：可见顶层库自身 + 其全部子库。"""
    tops = list(accessible_kbs(user).values_list("slug", "id"))
    slugs = {s for s, _ in tops}
    child_rows = KnowledgeBase.objects.filter(
        parent_id__in=[i for _, i in tops], is_folder=False,
    ).values_list("slug", flat=True)
    slugs.update(child_rows)
    return slugs


def accessible_docs(user, completed_only: bool = True) -> QuerySet[Document]:
    """用户可见的文档（按所属库的部门过滤；子库与文件夹部门同步）。"""
    dept = _user_dept(user)
    qs = Document.objects.select_related("kb")
    if completed_only:
        qs = qs.filter(status=Document.Status.COMPLETED)
    return qs.filter(kb__department__in=[DEPARTMENT_GENERAL, dept])


def set_kb_department(kb: KnowledgeBase, department: str) -> None:
    """设置知识库部门；文件夹会级联更新其所有子库，保持扇出检索一致性。"""
    department = (department or "").strip() or DEPARTMENT_GENERAL
    kb.department = department
    kb.save(update_fields=["department", "updated_at"])
    if kb.is_folder:
        KnowledgeBase.objects.filter(parent=kb).update(department=department)


def can_edit_feed(user, feed) -> bool:
    """Only the original builder with current KB management rights may edit a feed."""
    return bool(user and user.is_active and user.pk == feed.created_by_id
                and is_manager(user) and can_manage_kb(user, feed.kb))


def require_feed_builder(user, feed) -> None:
    if not can_edit_feed(user, feed):
        raise ValueError('仅此自动任务的创建者可编辑配置。')
