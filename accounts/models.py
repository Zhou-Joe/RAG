# accounts models：在 Django auth User 之外扩展 UserProfile（部门等字段）。
from __future__ import annotations

from django.conf import settings
from django.db import models

from kb.models import DEPARTMENT_GENERAL


class Department(models.Model):
    """显式登记的部门（管理员可提前创建/重命名，不必先挂到某个用户上）。

    部门池 = Department 表 ∪ 用户档案的部门（主部门 + 多选部门）∪ 知识库的部门
    （见 kb.access.all_departments），旧数据（只有用户/库带部门、无登记行）继续有效。
    「通用」也是一个部门，默认分配给所有账号（见 user_departments）。
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


class Role(models.TextChoices):
    """账号级别。挂在**部门**上（见 UserDepartment）：同一账号在 A 部门可以是管理员、
    在 B 部门只是普通成员；`UserProfile.role` 是汇总（任一部门是管理员即管理员）。"""

    USER = "user", "普通用户"
    DEPT_ADMIN = "dept_admin", "部门管理员"


class UserProfile(models.Model):
    """用户的扩展属性。OneToOne 挂在 auth User 上，避免替换自定义 User 模型。"""

    Role = Role  # 旧调用写法继续可用：UserProfile.Role.DEPT_ADMIN

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile",
        verbose_name="用户",
    )
    department = models.CharField(
        "主部门", max_length=50, default=DEPARTMENT_GENERAL,
        help_text="默认归属部门：新建知识库与个人库落在它身上；"
                  "可见范围看部门集合，管理权看逐部门级别。",
    )
    departments = models.ManyToManyField(
        Department, through="UserDepartment", blank=True, related_name="user_profiles",
        verbose_name="部门（可多选）",
        help_text="一个账号可属于多个部门，可见范围＝「通用」∪ 全部部门。"
                  "「通用」默认在内且不可取消；每个部门里的级别单独设置。",
    )
    role = models.CharField(
        "账号级别", max_length=20, choices=Role.choices, default=Role.USER,
        help_text="汇总值：任一部门是部门管理员即部门管理员。新挂部门默认普通用户。"
                  "逐部门的级别在「部门身份」里设置；所有用户都可管理仅本人可见的个人知识库。",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "用户资料"
        verbose_name_plural = "用户资料"

    def __str__(self):
        return f"{self.user.username}（{self.department}·{self.get_role_display()}）"


class UserDepartment(models.Model):
    """账号在**某个部门**里的身份：可见范围按部门算，管理权按「部门 + 级别」算。

    M2M 的 through 模型（`UserProfile.departments`）：0005 那张纯关联表只能记「在不在」，
    这里多加一列 role 记「在这个部门是什么级别」。同一个 (账号, 部门) 只允许一行。
    """

    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE,
                                related_name="memberships", verbose_name="用户")
    department = models.ForeignKey(Department, on_delete=models.CASCADE,
                                   related_name="memberships", verbose_name="部门")
    role = models.CharField("该部门级别", max_length=20, choices=Role.choices,
                            default=Role.USER)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "部门身份"
        verbose_name_plural = "部门身份"
        ordering = ["department__name"]
        constraints = [models.UniqueConstraint(fields=["profile", "department"],
                                               name="unique_user_department")]

    def __str__(self):
        return f"{self.profile.user.username}·{self.department.name}（{self.get_role_display()}）"


def _dedupe(names) -> list[str]:
    """去重去空，保持传入顺序（「通用」总是排在最前，由调用方决定顺序）。"""
    out: list[str] = []
    for name in names:
        name = (name or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def ensure_profile(user) -> "UserProfile | None":
    """取用户档案；无 Profile（老用户/未落库）时惰性补建，避免各处重复写这段。"""
    profile = getattr(user, "profile", None)
    if profile is None:
        profile, _ = UserProfile.objects.get_or_create(
            user=user, defaults={"department": DEPARTMENT_GENERAL},
        )
    return profile


def user_department(user) -> str:
    """取用户主部门；无 Profile（老用户/未落库）时惰性补建并返回「通用」。"""
    if user is None or not getattr(user, "is_authenticated", False):
        return DEPARTMENT_GENERAL
    profile = ensure_profile(user)
    return (profile.department if profile else "") or DEPARTMENT_GENERAL


def profile_departments(owner) -> list[str]:
    """生效的部门集合（「通用」永远第一）：主部门 ∪ 部门身份行。传 UserProfile 或 User 都行。

    「通用」是分配给所有账号的默认部门（未勾选也生效）；主部门即使还没登记成
    Department 行（旧数据）也照样计入，避免迁移遗漏造成权限倒退。
    """
    if owner is None:
        return [DEPARTMENT_GENERAL]
    if not isinstance(owner, UserProfile):
        owner = getattr(owner, "profile", None)  # 传的是 User（无档案 → 只剩「通用」）
    if owner is None:
        return [DEPARTMENT_GENERAL]
    names = [DEPARTMENT_GENERAL, owner.department or ""]
    names.extend(owner.memberships.values_list("department__name", flat=True))
    return _dedupe(names)


def profile_roles(owner) -> dict[str, str]:
    """每个部门里的级别：{部门名: role}，一定包含「通用」。

    关联行缺失的旧账号（直接 `UserProfile.objects.create()` 建的）按主部门推断：
    主部门拿档案上的级别，其它部门按普通成员，「通用」按 `_general_role` 规则。
    """
    profile = owner if isinstance(owner, UserProfile) else (
        getattr(owner, "profile", None) if owner is not None else None)
    if profile is None:
        return {DEPARTMENT_GENERAL: Role.USER}
    rows = list(profile.memberships.select_related("department"))
    roles = {r.department.name: r.role for r in rows}
    if not rows:  # 旧数据兜底：没有关联行就按「一个主部门 + 通用」推断
        roles[profile.department or DEPARTMENT_GENERAL] = profile.role or Role.USER
    roles.setdefault(DEPARTMENT_GENERAL, _general_role(profile) if not rows else Role.USER)
    return roles


def _general_role(profile) -> str:
    """「通用」部门里的级别。

    「通用」人人都有，所以「在通用部门里是管理员」＝能维护公共库，只能来自
    旧数据里那批主部门就是「通用」的部门管理员；新账号想维护公共库要在部门身份里
    显式把「通用」设成部门管理员。
    """
    if ((profile.department or DEPARTMENT_GENERAL) == DEPARTMENT_GENERAL
            and (profile.role or Role.USER) == Role.DEPT_ADMIN):
        return Role.DEPT_ADMIN
    return Role.USER


def user_departments(user) -> list[str]:
    """用户所属部门（可见范围）：「通用」永远在最前，其后是主部门与全部部门。"""
    if user is None or not getattr(user, "is_authenticated", False):
        return [DEPARTMENT_GENERAL]
    cached = getattr(user, "_fos_departments", None)
    if cached is not None:
        return list(cached)
    result = profile_departments(getattr(user, "profile", None))
    try:
        user._fos_departments = result  # 同一请求内多次权限判断只查一次
    except Exception:  # noqa: BLE001  不可写属性的用户对象（如代理模型）直接忽略缓存
        pass
    return list(result)


def user_role_in(user, department) -> str:
    """本人在某个部门里的级别（「通用」也算一个部门）。"""
    profile = getattr(user, "profile", None) if user is not None else None
    return profile_roles(profile).get((department or "").strip(), Role.USER)


def user_admin_departments(user) -> list[str]:
    """本人在其中担任**部门管理员**的部门——管理权的唯一来源。"""
    profile = getattr(user, "profile", None) if user is not None else None
    if profile is None:
        return []
    return [name for name, role in profile_roles(profile).items() if role == Role.DEPT_ADMIN]


def user_write_departments(user) -> list[str]:
    """可维护（写）的部门集合 = 本人当部门管理员的那些部门。

    与可见范围分开：可见 ≠ 能改。一个人可以在 A 部门当管理员、在 B 部门只是成员，
    那么他只改得动 A 部门的知识库。「通用」按部门身份里的级别算（默认人人只是成员，
    所以公共库对多数人只读）。
    """
    return user_admin_departments(user)


def department_members(name):
    """把该部门算进可见范围的档案（主部门 ∪ 部门身份）：删部门前的占用检查。"""
    from django.db.models import Q

    return UserProfile.objects.filter(Q(department=name) | Q(memberships__department__name=name)).distinct()


def department_admins(name):
    """该部门里担任部门管理员的档案。"""
    return UserProfile.objects.filter(memberships__department__name=name,
                                      memberships__role=Role.DEPT_ADMIN).distinct()


def assign_departments(user, names, primary=None, roles=None, *, register=True,
                       sync_role=True) -> list[str]:
    """写入用户的部门与逐部门级别：自动补「通用」、按需登记新部门名，并同步主部门。

    `roles` = {部门名: role}；没给的部门保留它原有的级别。首次转换旧档案时
    沿用原级别；之后新挂部门默认普通用户。返回最终生效的部门列表（「通用」在最前）。
    `register=False` 时不创建 Department 登记行（用于部门池之外的临时值）。
    """
    profile = ensure_profile(user)
    if profile is None:
        return []
    wanted = _dedupe([DEPARTMENT_GENERAL, *(names or [])])
    primary = (primary or profile.department or DEPARTMENT_GENERAL).strip() or DEPARTMENT_GENERAL
    if primary not in wanted:
        wanted.append(primary)
    if register:
        for name in wanted:
            Department.objects.get_or_create(name=name)
    rows = {d.name: d for d in Department.objects.filter(name__in=wanted)}
    current = profile_roles(profile)
    had_memberships = profile.memberships.exists()
    UserDepartment.objects.filter(profile=profile).exclude(
        department__in=[d.pk for d in rows.values()]).delete()
    for name in wanted:
        row = rows.get(name)
        if row is None:
            continue
        if roles and name in roles:
            role = roles[name] if roles[name] in Role.values else Role.USER
        elif name in current:
            role = current[name]
        elif not had_memberships:
            # Preserve the legacy account role when converting an old profile
            # for the first time. Subsequent additions default to a member.
            role = profile.role if profile.role in Role.values else Role.USER
        else:
            role = Role.USER
        UserDepartment.objects.update_or_create(
            profile=profile, department=row, defaults={"role": role},
        )
    if profile.department != primary:
        profile.department = primary
        profile.save(update_fields=["department", "updated_at"])
    else:
        profile.save(update_fields=["updated_at"])
    if sync_role:
        sync_profile_role(profile)
    try:
        del user._fos_departments  # 让下一次权限判断重新查库
    except AttributeError:
        pass
    return _dedupe(wanted)


def sync_profile_role(profile) -> str:
    """把档案上的「账号级别」同步成汇总值（任一部门是管理员即管理员）。"""
    highest = (Role.DEPT_ADMIN if profile.memberships.filter(role=Role.DEPT_ADMIN).exists()
               else Role.USER)
    if profile.role != highest:
        profile.role = highest
        profile.save(update_fields=["role", "updated_at"])
    return highest


def user_role(user) -> str:
    """账号级别（汇总）：任一部门是部门管理员就是部门管理员；无档案视为普通用户。"""
    if user is None or not getattr(user, "is_authenticated", False):
        return Role.USER
    profile = getattr(user, "profile", None)
    if profile is None:
        return Role.USER
    if profile.memberships.filter(role=Role.DEPT_ADMIN).exists():
        return Role.DEPT_ADMIN
    if not profile.memberships.exists():
        return profile.role if profile.role in Role.values else Role.USER
    return Role.USER
