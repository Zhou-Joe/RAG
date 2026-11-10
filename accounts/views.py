"""accounts views: 注册（登录/登出用 Django 内建）+ 用户部门管理（staff）+ 自助改密"""
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import UserCreationForm
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .models import (DEPARTMENT_GENERAL, UserProfile, assign_departments,
                     profile_departments, profile_roles)


def _department_pool() -> list[str]:
    """可选部门池（登记行 ∪ 用户 ∪ 知识库；新部门只能由管理员在用户管理页创建）。"""
    from kb.access import all_departments
    return all_departments()


def _requested_departments(request, allowed: list[str]) -> list[str]:
    """从表单取多选部门（checkbox 名为 departments），只接受部门池内的名字。

    「通用」人人默认分配，不靠表单传（传了也会重新补上）。
    """
    picked = [(v or "").strip() for v in request.POST.getlist("departments")]
    return [d for d in dict.fromkeys(picked) if d and d != DEPARTMENT_GENERAL and d in allowed]


def _requested_roles(request, allowed: list[str]) -> dict[str, str]:
    """每个勾上的部门自己的级别（select 名为 dept_role__<部门名>）。

    部门名可以含中文，POST 键名前缀取后就是部门名。没勾的部门即使发了级别也忽略；
    未知级别一律按普通用户。「通用」也收（在通用部门里当管理员＝能维护公共库）。
    """
    prefix = "dept_role__"
    sent = {(k[len(prefix):]).strip(): (v or "").strip()
            for k, v in request.POST.items() if k.startswith(prefix)}
    pool = set(allowed) | {DEPARTMENT_GENERAL}
    out = {}
    for name in _requested_departments(request, allowed):
        out[name] = sent.get(name) if sent.get(name) in UserProfile.Role.values else UserProfile.Role.USER
    general = sent.get(DEPARTMENT_GENERAL)
    if general:
        out[DEPARTMENT_GENERAL] = general if general in UserProfile.Role.values else UserProfile.Role.USER
    return out


def register_view(request):
    """用户注册。新账号一律「通用」部门——注册不受理部门自选
    （自选即越权获得该部门全部知识库的访问），归属由管理员在用户管理页指派。"""
    if request.user.is_authenticated:
        return redirect("/")
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            UserProfile.objects.update_or_create(
                user=user, defaults={"department": DEPARTMENT_GENERAL},
            )
            # 「通用」也是部门，默认分配给每一个新账号（见 models.user_departments）
            assign_departments(user, [DEPARTMENT_GENERAL], primary=DEPARTMENT_GENERAL)
            login(request, user)
            return redirect("/")
    else:
        form = UserCreationForm()
    return render(request, "accounts/register.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def profile(request):
    """View and edit the signed-in user's contact details, never access rights."""
    from django.contrib import messages
    from django.utils.cache import add_never_cache_headers
    from .forms import PersonalInfoForm
    from .models import user_department, user_role

    form = PersonalInfoForm(request.POST if request.method == "POST" else None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        user = form.save(commit=False)
        # Avoid overwriting a password, login timestamp or permissions changed concurrently.
        user.save(update_fields=['first_name', 'last_name', 'email'])
        messages.success(request, "个人信息已保存。")
        response = redirect('accounts:profile')
    else:
        role = "全局管理员" if request.user.is_staff or request.user.is_superuser else dict(UserProfile.Role.choices).get(user_role(request.user), UserProfile.Role.USER.label)
        response = render(request, "accounts/profile.html", {
            "form": form, "account_role": role, "user_department": user_department(request.user),
        })
    add_never_cache_headers(response)
    return response


@login_required
@require_http_methods(["POST"])
def password_change(request):
    """用户自助修改自己的密码（AJAX，全站统一弹窗）。

    校验：当前密码正确、两次新密码一致、至少 6 位（与添加账号的规则相同）、
    不得与当前密码相同。update_session_auth_hash 保证改完不掉线。
    """
    from django.contrib.auth import update_session_auth_hash

    old = request.POST.get("old_password") or ""
    new1 = request.POST.get("new_password1") or ""
    new2 = request.POST.get("new_password2") or ""

    if not request.user.check_password(old):
        ok, msg = False, "当前密码不正确。"
    elif new1 != new2:
        ok, msg = False, "两次输入的新密码不一致。"
    elif len(new1) < 6:
        ok, msg = False, "新密码至少 6 位。"
    elif new1 == old:
        ok, msg = False, "新密码不能与当前密码相同。"
    else:
        request.user.set_password(new1)
        request.user.save()
        update_session_auth_hash(request, request.user)
        ok, msg = True, "密码已修改。"
    return JsonResponse({"ok": ok, "message": msg})


def _is_dept_admin(user) -> bool:
    from .models import UserProfile, user_role
    return user_role(user) == UserProfile.Role.DEPT_ADMIN


def _handle_department_action(request, action: str) -> None:
    """部门管理动作（仅全局管理员调用）：

    - dept_add：登记新部门（可先建部门再进人）
    - dept_rename：重命名 → 级联更新所有用户档案和知识库（含文件夹/子库同步）
    - dept_delete：仅在无用户、无知识库使用时允许
    """
    from django.contrib import messages as _messages

    from .models import Department, UserProfile, department_members
    from kb.access import all_departments
    from kb.models import DEPARTMENT_GENERAL, KnowledgeBase

    if action == "dept_add":
        name = (request.POST.get("name") or "").strip()
        if not name or name == DEPARTMENT_GENERAL:
            _messages.error(request, "部门名不能为空，且不能叫「通用」。")
        elif name in all_departments():
            _messages.error(request, f"部门「{name}」已存在。")
        else:
            Department.objects.create(name=name)
            _messages.success(request, f"部门「{name}」已创建。")

    elif action == "dept_rename":
        old = (request.POST.get("old") or "").strip()
        new = (request.POST.get("new") or "").strip()
        if not old or old == DEPARTMENT_GENERAL:
            _messages.error(request, "「通用」不可重命名。")
        elif old not in all_departments():
            _messages.error(request, f"部门「{old}」不存在。")
        elif not new or new == DEPARTMENT_GENERAL:
            _messages.error(request, "新部门名不能为空，且不能叫「通用」。")
        elif new != old and new in all_departments():
            _messages.error(request, f"部门「{new}」已存在。")
        else:
            # 登记行（旧数据可能只有名字、没有行，先补一行）+ 级联更新用户和知识库。
            # 多选部门关联的就是这一行，改名自动生效；主部门是字符串，要单独更新。
            dept, _ = Department.objects.get_or_create(name=old)
            dept.name = new
            dept.save(update_fields=["name", "updated_at"])
            UserProfile.objects.filter(department=old).update(department=new)
            KnowledgeBase.objects.filter(department=old).update(department=new)
            _messages.success(request, f"部门「{old}」已重命名为「{new}」"
                                        f"（用户与知识库已同步更新）。")

    elif action == "dept_delete":
        name = (request.POST.get("name") or "").strip()
        if not name or name == DEPARTMENT_GENERAL:
            _messages.error(request, "「通用」不可删除（它默认分配给所有账号）。")
        else:
            # 主部门与多选部门都算「在用」（旧档案可能只有主部门字符串、没有关联行）
            user_n = department_members(name).count()
            kb_n = KnowledgeBase.objects.filter(department=name).count()
            if user_n or kb_n:
                _messages.error(
                    request,
                    f"部门「{name}」仍有 {user_n} 个用户、{kb_n} 个知识库在使用，"
                    f"不能删除（先把它们从部门里去掉）。")
            else:
                Department.objects.filter(name=name).delete()
                _messages.success(request, f"部门「{name}」已删除。")


@login_required
@user_passes_test(
    lambda u: (u.is_staff or u.is_superuser) or _is_dept_admin(u),
    login_url="/accounts/login/",
)
def user_list(request):
    """用户管理 tab（全局管理员 或 部门管理员）。

    一个账号可属于**多个**部门（「通用」人人默认在内）；下面所说的「本部门成员」
    指与当前账号有共同部门的成员。

    - 全局管理员：看到全部用户，可改部门（主部门 + 多选）+ 账号级别，可删除非超级管理员账号。
    - 部门管理员：只看到自己任一部门的成员，只能改账号级别（提升/撤销部门管理员）、
      删除这些部门的非管理员成员；不能改成员的部门归属，不能动全局管理员账号。
    """
    User = get_user_model()
    from .models import ensure_profile, user_departments, user_department
    viewer_global = request.user.is_staff or request.user.is_superuser
    viewer_dept = user_department(request.user)
    # 部门管理员能碰的部门：他在这些部门里是**部门管理员**的那些
    # （只是成员的部门不管；「通用」也不算，否则能随意拉人进公共库）
    from kb.access import manageable_departments
    viewer_depts = set(manageable_departments(request.user))

    def _member_depts(target) -> list[str]:
        return profile_departments(target)  # 传 User 即可，内部会取它的档案

    def _same_dept_member(target) -> bool:
        return bool(viewer_depts & set(_member_depts(target)))

    if request.method == "POST":
        action = request.POST.get("action") or "save"

        # ---- 部门管理（仅全局管理员）----
        if action in ("dept_add", "dept_rename", "dept_delete"):
            if viewer_global:
                _handle_department_action(request, action)
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                # 弹窗 AJAX：从 messages 取回执行结果转 JSON（成功/失败都带提示）
                from django.contrib.messages import get_messages
                msgs = list(get_messages(request))
                last = msgs[-1] if msgs else None
                level = (getattr(last, "level_tag", "") or "").split()[0] if last else ""
                return JsonResponse({
                    "ok": bool(last) and level != "error",
                    "message": str(last) if last else "操作完成。",
                })
            return redirect("accounts:user_list")

        # ---- 添加账号（全局管理员 或 部门管理员；支持 AJAX 弹窗）----
        if action == "user_add":
            from django.contrib import messages as _messages
            from kb.access import all_departments
            username = (request.POST.get("username") or "").strip()
            password = request.POST.get("password") or ""
            role = request.POST.get("role") or UserProfile.Role.USER
            if role not in UserProfile.Role.values:
                role = UserProfile.Role.USER
            if viewer_global:
                department = (request.POST.get("department") or "").strip() or DEPARTMENT_GENERAL
                if department not in all_departments():
                    department = DEPARTMENT_GENERAL
                extra = _requested_departments(request, all_departments())
            else:
                # 部门管理员添加的账号只能落在自己所辖的部门里（可多选，「通用」送不了）
                extra = _requested_departments(request, sorted(viewer_depts))
                # 主部门取勾到的第一个；没勾就按自己的主部门（旧行为）
                department = extra[0] if extra else (
                    viewer_dept if viewer_dept in viewer_depts else DEPARTMENT_GENERAL)
            if not username:
                ok, msg = False, "用户名不能为空。"
            elif User.objects.filter(username=username).exists():
                ok, msg = False, f"用户名「{username}」已存在。"
            elif len(password) < 6:
                ok, msg = False, "密码至少 6 位。"
            else:
                user = User.objects.create_user(username=username, password=password)
                UserProfile.objects.update_or_create(
                    user=user, defaults={"department": department, "role": role})
                # 弹窗里的「账号级别」对勾到的每个部门生效（要逐部门区分建完再改）
                names = assign_departments(
                    user, [department, *extra], primary=department,
                    roles={d: role for d in [department, *extra] if d != DEPARTMENT_GENERAL})
                ok, msg = True, (f"账号「{username}」已创建（部门："
                                 f"{'、'.join(names)}，初始密码已生效）。")
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"ok": ok, "message": msg})
            (getattr(_messages, "success" if ok else "error"))(request, msg)
            return redirect("accounts:user_list")

        target = User.objects.filter(pk=request.POST.get("user_id")).first()
        if target is not None:
            role = request.POST.get("role") or UserProfile.Role.USER
            if role not in UserProfile.Role.values:
                role = UserProfile.Role.USER

            if action == "delete":
                can = False
                if target != request.user and not target.is_superuser:
                    if viewer_global:
                        can = True
                    else:  # 部门管理员：仅所辖部门的非管理员成员
                        can = (not target.is_staff
                               and _same_dept_member(target))
                if can:
                    target.delete()  # CASCADE：其会话/消息一并清理
            else:  # save：编辑
                if viewer_global:
                    if not target.is_superuser:
                        from kb.access import all_departments
                        pool = all_departments()
                        department = (request.POST.get("department") or "").strip() or DEPARTMENT_GENERAL
                        if department not in pool:
                            department = DEPARTMENT_GENERAL
                        UserProfile.objects.update_or_create(
                            user=target, defaults={"department": department})
                        # 主部门从下拉选；每个部门勾不勾 + 各自级别都在 picker 里
                        assign_departments(target, [department, *_requested_departments(request, pool)],
                                           primary=department,
                                           roles=_requested_roles(request, pool))
                else:  # 部门管理员：仅所辖部门非管理员成员，且只能改那些部门里的级别
                    if (not target.is_staff and not target.is_superuser
                            and _same_dept_member(target)):
                        member = ensure_profile(target)
                        allowed = sorted(viewer_depts)
                        shared = viewer_depts & set(profile_departments(target))
                        given = {d: r for d, r in _requested_roles(request, allowed).items()
                                 if d in shared}
                        assign_departments(target, profile_departments(target),
                                           primary=member.department, roles=given)
        return redirect("accounts:user_list")

    qs = User.objects.select_related("profile").prefetch_related("profile__memberships__department")
    qs = qs.order_by("-date_joined")
    if not viewer_global:
        # 共享任意一个部门的都算我的成员（多选后不再只有一个「本部门」）
        qs = [u for u in qs.filter(profile__isnull=False) if _same_dept_member(u)]

    pool = _department_pool()
    role_labels = dict(UserProfile.Role.choices)
    rows = []
    for u in qs:
        profile = getattr(u, "profile", None)
        depts = _member_depts(u)
        dept = (profile.department if profile else "") or ""
        roles = profile_roles(u)
        role = profile.role if profile else UserProfile.Role.USER
        if viewer_global:
            can_edit = not u.is_superuser
            can_delete = (not u.is_superuser) and u.pk != request.user.pk
        else:
            member_ok = (not u.is_staff and not u.is_superuser
                         and bool(viewer_depts & set(depts)))
            can_edit = member_ok
            can_delete = member_ok and u.pk != request.user.pk
        # 逐部门一行（勾选框 + 该部门级别）：全局管理员看全部部门，
        # 部门管理员只看自己当管理员的部门（+人人都在的「通用」，但只读）
        picker = []
        for d in pool:
            is_general = d == DEPARTMENT_GENERAL
            if not viewer_global and not is_general and d not in viewer_depts:
                continue
            current = roles.get(d, UserProfile.Role.USER)
            picker.append({
                "name": d,
                "checked": is_general or d in depts,
                "role": current,
                "role_label": role_labels.get(current, current),
                "is_general": is_general,
                "is_primary": d == dept,
                # 「通用」的级别只能全局管理员改（在通用当管理员＝能改公共库）
                "role_editable": bool(viewer_global or not is_general),
            })
        rows.append({
            "id": u.pk,
            "username": u.username,
            "department": dept,
            "departments": depts,
            "extra_departments": [d for d in depts if d != DEPARTMENT_GENERAL],
            "picker": picker,
            "admin_of": [d for d in depts if roles.get(d) == UserProfile.Role.DEPT_ADMIN],
            "member_of": [d for d in depts if roles.get(d) != UserProfile.Role.DEPT_ADMIN],
            "role": role,
            "is_staff": u.is_staff,
            "is_superuser": u.is_superuser,
            "is_active": u.is_active,
            "date_joined": u.date_joined,
            "can_edit": can_edit,
            "can_delete": can_delete,
            "is_self": u.pk == request.user.pk,
        })
    dept_rows = []
    if viewer_global:
        from django.db.models import Count
        from kb.access import all_departments
        from kb.models import KnowledgeBase
        # 按“生效部门集合”数人：一个账号可能在多选里挂了多个部门，不能只数主部门
        u_counts: dict[str, int] = {}
        a_counts: dict[str, int] = {}
        for p in UserProfile.objects.prefetch_related("memberships__department"):
            roles = profile_roles(p)
            for name in profile_departments(p):
                u_counts[name] = u_counts.get(name, 0) + 1
                if roles.get(name) == UserProfile.Role.DEPT_ADMIN:
                    a_counts[name] = a_counts.get(name, 0) + 1
        k_counts = dict(
            KnowledgeBase.objects.values("department")
            .annotate(n=Count("id")).values_list("department", "n"))
        for d in all_departments():
            dept_rows.append({
                "name": d,
                "locked": d == DEPARTMENT_GENERAL,  # 「通用」不可改名/删除，人人都有
                "users": u_counts.get(d, 0),
                "admins": a_counts.get(d, 0),
                "kbs": k_counts.get(d, 0),
            })

    return render(request, "accounts/user_list.html", {
        "rows": rows,
        "departments": pool,
        "my_departments": sorted(viewer_depts),
        "roles": UserProfile.Role.choices,
        "viewer_global": viewer_global,
        "viewer_department": viewer_dept,
        "viewer_departments": user_departments(request.user),
        "dept_rows": dept_rows,
    })
