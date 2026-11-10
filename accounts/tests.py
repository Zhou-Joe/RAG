"""accounts：注册部门策略测试（安全回归）。"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from kb.models import KnowledgeBase

from django.contrib import admin

from .models import (DEPARTMENT_GENERAL, Department, UserDepartment, UserProfile,
                     assign_departments, department_members, profile_departments,
                     profile_roles, user_departments, user_role)


class RegistrationDepartmentPolicyTests(TestCase):
    def test_register_forces_general_department(self):
        """注册不受理部门自选：伪造 department=机械 也必须落「通用」。"""
        KnowledgeBase.objects.create(
            name="机械库", slug="mech-lib", is_folder=True,
            created_by=get_user_model().objects.create_user("seed", password="x"),
            department="机械",
        )
        r = self.client.post(reverse("accounts:register"), {
            "username": "newbie",
            "password1": "s3cure-pass-9",
            "password2": "s3cure-pass-9",
            "department": "机械",  # 恶意/越权自选
        })
        self.assertEqual(r.status_code, 302)
        profile = UserProfile.objects.get(user__username="newbie")
        self.assertEqual(profile.department, DEPARTMENT_GENERAL)

    def test_register_page_does_not_leak_department_list(self):
        """注册页不再向匿名用户暴露内部部门名单。"""
        KnowledgeBase.objects.create(
            name="机械库", slug="mech-lib2", is_folder=True,
            created_by=get_user_model().objects.create_user("seed2", password="x"),
            department="机械",
        )
        r = self.client.get(reverse("accounts:register"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "机械")
        self.assertNotContains(r, 'name="department"')


class PasswordChangeTests(TestCase):
    """用户自助改密：顶栏/用户管理页弹窗 → POST /accounts/password/change/。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="pw-user", password="old-pass-123")
        self.client.force_login(self.user)
        self.url = reverse("accounts:password_change")

    def _post(self, old="old-pass-123", n1="new-pass-456", n2="new-pass-456"):
        return self.client.post(self.url, {
            "old_password": old, "new_password1": n1, "new_password2": n2,
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_change_ok_session_survives(self):
        r = self._post()
        data = r.json()
        self.assertTrue(data["ok"], data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-pass-456"))
        self.assertFalse(self.user.check_password("old-pass-123"))
        # update_session_auth_hash：改完不掉线
        self.assertTrue(self.client.get("/kb/search/").status_code, 200)

    def test_wrong_old_password_rejected(self):
        r = self._post(old="bad-old")
        self.assertFalse(r.json()["ok"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-pass-123"))

    def test_mismatch_and_too_short_rejected(self):
        self.assertFalse(self._post(n2="different").json()["ok"])
        self.assertFalse(self._post(n1="123", n2="123").json()["ok"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-pass-123"))

    def test_get_not_allowed_and_login_required(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        anon = Client()
        r = anon.post(self.url, {"old_password": "x", "new_password1": "y", "new_password2": "y"})
        self.assertEqual(r.status_code, 302)  # → 登录页

    def test_dialog_markup_and_entry_points(self):
        # 侧栏用户名进入个人信息；全局改密弹窗仍供个人页和管理页使用。
        r = self.client.get("/kb/search/")
        self.assertContains(r, 'id="pwdDialog"')
        self.assertContains(r, "pw-open")
        self.assertContains(r, "fosPasswordDialog")
        # 用户管理页自己那行有「改密」按钮
        self.user.is_staff = True
        self.user.save()
        r2 = self.client.get(reverse("accounts:user_list"))
        self.assertContains(r2, ">改密</button>")


class UserPageLayoutTests(TestCase):
    """用户管理页两板块结构 + 部门弹窗 AJAX 分支。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user(
            username="layout-admin", password="pw", is_staff=True)

    def test_page_split_into_two_sections(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("accounts:user_list"))
        self.assertEqual(r.status_code, 200)
        # 两个板块标题条 + 各自的主操作按钮
        self.assertContains(r, '<h2>用户</h2>')
        self.assertContains(r, 'id="openUserAdd"')
        self.assertContains(r, '<h2>部门</h2>')
        self.assertContains(r, 'id="openDeptAdd"')
        # 用户表格在部门板块之前（板块顺序）
        body = r.content.decode()
        self.assertLess(body.index("user-table-wrap"), body.index("dept-mgr"))
        # 通用部门锁定：无改名按钮、带固定徽章
        self.assertContains(r, "固定")
        self.assertNotContains(r, 'data-old="通用"')
        # 改名按钮走弹窗（委托）
        self.assertContains(r, "dept-rename-btn")

    def test_dept_add_and_rename_ajax_json(self):
        from .models import Department
        self.client.force_login(self.admin)
        # AJAX 添加 → JSON ok
        r = self.client.post(reverse("accounts:user_list"),
                             {"action": "dept_add", "name": "机械"},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        data = r.json()
        self.assertTrue(data["ok"], data)
        self.assertTrue(Department.objects.filter(name="机械").exists())
        # AJAX 改名 → JSON ok + 数据同步
        r2 = self.client.post(reverse("accounts:user_list"),
                              {"action": "dept_rename", "old": "机械", "new": "机械工程"},
                              HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r2.json()["ok"])
        self.assertFalse(Department.objects.filter(name="机械").exists())
        self.assertTrue(Department.objects.filter(name="机械工程").exists())
        # AJAX 重复添加 → JSON ok=false
        r3 = self.client.post(reverse("accounts:user_list"),
                              {"action": "dept_add", "name": "通用"},
                              HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(r3.json()["ok"])


class PersonalInfoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('personal-user', password='old-pass-123',
            first_name='Chen', last_name='Zhou', email='chen@example.test')
        self.other = get_user_model().objects.create_user('another-user', email='private-other@example.test')
        UserProfile.objects.update_or_create(user=self.user, defaults={'department': '机械'})
        self.client.force_login(self.user)
        self.url = reverse('accounts:profile')

    def values(self, **extra):
        return {'first_name': 'Updated name', 'last_name': '姓氏', 'email': 'new@example.test', **extra}

    def test_account_name_opens_personal_page_and_password_action_moves_there(self):
        page = self.client.get(reverse('kb:search'))
        self.assertContains(page, '<a class="side-user" href="'+self.url+'" title="个人信息">')
        self.assertNotContains(page, 'class="side-user pw-open"')
        profile = self.client.get(self.url)
        self.assertContains(profile, 'class="btn outline pw-open">修改密码</button>')
        self.assertContains(profile, 'id="pwdDialog"')
        self.assertContains(profile, 'name="first_name"')
        self.assertContains(profile, 'name="email"')
        self.assertContains(profile, 'chen@example.test')
        self.assertContains(profile, '机械')
        self.assertContains(profile, '普通用户')
        self.assertNotContains(profile, self.other.email)
        self.assertIn('no-store', profile.headers['Cache-Control'])

    def test_profile_is_own_account_even_when_another_user_is_requested(self):
        response = self.client.get(self.url, {'user_id': self.other.pk, 'username': self.other.username})
        self.assertContains(response, self.user.email)
        self.assertNotContains(response, self.other.email)

    def test_updates_contact_fields_and_keeps_session_and_password(self):
        original_password = self.user.password
        response = self.client.post(self.url, self.values())
        self.assertRedirects(response, self.url)
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name, self.user.email),
                         ('Updated name', '姓氏', 'new@example.test'))
        self.assertEqual(self.user.password, original_password)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_invalid_email_or_overlong_name_does_not_save_any_changes(self):
        for extra in ({'email': 'invalid email'}, {'first_name': 'x'*151}, {'last_name': 'x'*151}):
            response = self.client.post(self.url, self.values(**extra))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
            self.user.refresh_from_db()
            self.assertEqual((self.user.first_name, self.user.email), ('Chen', 'chen@example.test'))

    def test_optional_contact_fields_can_be_cleared(self):
        response = self.client.post(self.url, self.values(first_name='', last_name='', email=''))
        self.assertRedirects(response, self.url)
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name, self.user.email), ('', '', ''))

    def test_forged_account_target_role_and_credentials_are_ignored(self):
        self.client.post(self.url, self.values(user_id=self.other.pk, username='hijacked',
            department='电气', role='dept_admin', is_staff='1', is_superuser='1', password='injected'))
        self.user.refresh_from_db()
        self.user.profile.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.user.username, 'personal-user')
        self.assertEqual(self.user.profile.department, '机械')
        self.assertEqual(self.user.profile.role, UserProfile.Role.USER)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertTrue(self.user.check_password('old-pass-123'))
        self.assertEqual(self.other.email, 'private-other@example.test')

    def test_concurrent_permission_change_is_not_overwritten_by_contact_save(self):
        from unittest.mock import patch
        from accounts.forms import PersonalInfoForm
        original = PersonalInfoForm.save
        def concurrent_save(form, **kwargs):
            get_user_model().objects.filter(pk=form.instance.pk).update(is_staff=True)
            return original(form, **kwargs)
        with patch.object(PersonalInfoForm, 'save', concurrent_save):
            self.assertRedirects(self.client.post(self.url, self.values()), self.url)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertEqual(self.user.email, 'new@example.test')

    def test_password_change_remains_available_and_session_survives(self):
        response = self.client.post(reverse('accounts:password_change'), {
            'old_password': 'old-pass-123', 'new_password1': 'new-pass-456', 'new_password2': 'new-pass-456'})
        self.assertTrue(response.json()['ok'])
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_authentication_csrf_and_method_checks(self):
        from django.test import Client
        anonymous = Client()
        self.assertEqual(anonymous.get(self.url).status_code, 302)
        self.assertEqual(anonymous.post(self.url, self.values()).status_code, 302)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(self.url, self.values()).status_code, 403)
        self.assertEqual(self.client.delete(self.url).status_code, 405)


class MultiDepartmentUserManagementTests(TestCase):
    """用户管理页：一个账号可挂多个部门，「通用」人人默认且有而不可撤。"""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.gadmin = User.objects.create_user("dept-gadmin", password="pw", is_staff=True)
        assign_departments(cls.gadmin, [DEPARTMENT_GENERAL])
        cls.dept_admin = User.objects.create_user("mech-admin", password="pw")
        UserProfile.objects.create(user=cls.dept_admin, department="机械",
                                   role=UserProfile.Role.DEPT_ADMIN)
        assign_departments(cls.dept_admin, ["机械", "电气"])
        cls.mech_member = User.objects.create_user("mech-user", password="pw")
        UserProfile.objects.create(user=cls.mech_member, department="机械")
        assign_departments(cls.mech_member, ["机械"])
        cls.elec_member = User.objects.create_user("elec-user", password="pw")
        UserProfile.objects.create(user=cls.elec_member, department="电气")
        assign_departments(cls.elec_member, ["电气"])
        cls.general_user = User.objects.create_user("general-only", password="pw")
        UserProfile.objects.create(user=cls.general_user, department=DEPARTMENT_GENERAL)
        assign_departments(cls.general_user, [DEPARTMENT_GENERAL])
        cls.url = reverse("accounts:user_list")

    def _save(self, target, department, extras=(), role=None):
        """POST 用户行的「保存」表单：主部门走 select，附加部门走 checkbox。"""
        return self.client.post(self.url, {
            "action": "save", "user_id": target.pk,
            "role": role or UserProfile.Role.USER,
            "department": department, "departments": list(extras),
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_admin_assigns_several_departments(self):
        self.client.force_login(self.gadmin)
        self._save(self.mech_member, "机械", ["机械", "电气", "工艺"])
        profile = self.mech_member.profile
        profile.refresh_from_db()
        self.assertEqual(profile.department, "机械")
        self.assertEqual(profile_departments(self.mech_member),
                         [DEPARTMENT_GENERAL, "机械", "电气"])
        self.assertFalse(Department.objects.filter(name="工艺").exists())

    def test_unchecked_department_is_removed_but_general_stays(self):
        self.client.force_login(self.gadmin)
        self._save(self.dept_admin, "机械", ["机械"], role=UserProfile.Role.DEPT_ADMIN)  # 取消「电气」
        self.assertEqual(profile_departments(self.dept_admin), [DEPARTMENT_GENERAL, "机械"])

    def test_admin_can_assign_departments_they_are_not_in(self):
        """全局管理员不限自己的部门（只要部门已登记）；未登记的名字不会从表单冒出来。"""
        self.client.force_login(self.gadmin)
        self._save(self.mech_member, "机械", ["机械", "电气"])
        self.assertIn("电气", profile_departments(self.mech_member))
        self._save(self.mech_member, "机械", ["机械", "保密部"])
        self.assertNotIn("保密部", profile_departments(self.mech_member))
        self.assertFalse(Department.objects.filter(name="保密部").exists())

    def test_page_lists_every_department_as_a_badge(self):
        self.client.force_login(self.gadmin)
        r = self.client.get(self.url)
        self.assertContains(r, "电气")
        self.assertContains(r, 'name="departments" value="电气"')
        # 「通用」是默认部门：勾选、禁用、标「默认」
        self.assertContains(r, "默认")

    def test_dept_admin_sees_members_of_all_his_departments(self):
        self.client.force_login(self.dept_admin)
        rows = {r["username"] for r in self.client.get(self.url).context["rows"]}
        # 两个部门的人都算「本部门」，包括他自己那一行
        self.assertEqual(rows, {"mech-admin", "mech-user", "elec-user"})

    def test_dept_admin_cannot_grant_departments_he_does_not_own(self):
        self.client.force_login(self.dept_admin)
        # 添加账号：勾上自己不属于的部门 + 伪造「通用」，都应当被丢掉
        r = self.client.post(self.url, {
            "action": "user_add", "username": "newbie", "password": "pw12345",
            "role": UserProfile.Role.USER, "department": "保密部",
            "departments": ["电气", "保密部", DEPARTMENT_GENERAL],
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r.json()["ok"], r.json())
        newbie = get_user_model().objects.get(username="newbie")
        self.assertEqual(profile_departments(newbie), [DEPARTMENT_GENERAL, "电气"])
        self.assertEqual(newbie.profile.department, "电气")

    def test_dept_admin_cannot_edit_members_of_other_departments(self):
        self.client.force_login(self.dept_admin)
        r = self.client.post(self.url, {
            "action": "save", "user_id": self.general_user.pk,
            "role": UserProfile.Role.DEPT_ADMIN, "department": "电气",
            "departments": ["电气"],
        })
        self.assertEqual(r.status_code, 302)
        self.general_user.profile.refresh_from_db()
        self.assertEqual(self.general_user.profile.role, UserProfile.Role.USER)
        self.assertEqual(self.general_user.profile.department, DEPARTMENT_GENERAL)

    def test_deleting_a_member_with_departments_cleans_the_links(self):
        """删账号必须连带清掉多选部门关联：残留行会让 SQLite 报 FOREIGN KEY（500）。"""
        self.client.force_login(self.gadmin)
        r = self.client.post(self.url, {"action": "delete", "user_id": self.elec_member.pk})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(UserProfile.objects.filter(user__username="elec-user").exists())
        self.assertEqual([u.user.username for u in department_members("电气")], ["mech-admin"])

    def test_department_used_only_in_multi_select_cannot_be_deleted(self):
        self.client.force_login(self.gadmin)
        r = self.client.post(self.url, {"action": "dept_delete", "name": "电气"},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(r.json()["ok"])
        self.assertIn("电气", r.json()["message"])
        self.assertTrue(Department.objects.filter(name="电气").exists())

    def test_rename_updates_membership_and_primary(self):
        self.client.force_login(self.gadmin)
        r = self.client.post(self.url, {"action": "dept_rename", "old": "电气", "new": "电气维修"},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r.json()["ok"], r.json())
        self.assertIn("电气维修", profile_departments(self.elec_member))
        self.elec_member.profile.refresh_from_db()
        self.assertEqual(self.elec_member.profile.department, "电气维修")
        self.assertFalse(Department.objects.filter(name="电气").exists())

    def test_general_department_is_locked_in_registry(self):
        self.client.force_login(self.gadmin)
        for action in ("dept_rename", "dept_delete"):
            payload = {"action": action, "name": DEPARTMENT_GENERAL}
            if action == "dept_rename":
                payload["old"] = DEPARTMENT_GENERAL
                payload["new"] = "全部"
            r = self.client.post(self.url, payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            self.assertFalse(r.json()["ok"], r.json())
        self.assertTrue(Department.objects.filter(name=DEPARTMENT_GENERAL).exists())


class DepartmentMigrationTests(TestCase):
    """旧库只有主部门：迁移/首次访问都要拿到「通用」，不因缺多选行而丢权限。"""

    def test_profile_without_rows_still_visible_general(self):
        user = get_user_model().objects.create_user("legacy-no-profile")
        self.assertEqual(user_departments(user), [DEPARTMENT_GENERAL])

    def test_profile_created_without_m2m_still_sees_its_department(self):
        user = get_user_model().objects.create_user("legacy-with-dept")
        UserProfile.objects.create(user=user, department="机械")
        self.assertEqual(user_departments(user), [DEPARTMENT_GENERAL, "机械"])


class DjangoAdminDepartmentTests(TestCase):
    """Django 后台：逐部门级别能改、「通用」拿不掉也删不掉。"""

    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            "dept-super", "s@example.test", "pw")
        self.client.force_login(self.superuser)
        self.user = get_user_model().objects.create_user("admin-target", password="pw")
        self.profile = UserProfile.objects.create(user=self.user, department="机械")
        assign_departments(self.user, ["机械", "电气"])

    def _membership_post(self, roles=None, delete=()):
        """按「部门身份」inline 的表单结构提交（roles = {部门名: 级别}，delete = 勾删除的部门）。"""
        rows = list(self.profile.memberships.order_by("pk"))
        data = {"user": self.user.pk, "department": self.profile.department,
                "memberships-TOTAL_FORMS": str(len(rows)),
                "memberships-INITIAL_FORMS": str(len(rows)),
                "memberships-MIN_NUM_FORMS": "0", "memberships-MAX_NUM_FORMS": "1000"}
        for i, m in enumerate(rows):
            data[f"memberships-{i}-id"] = str(m.pk)
            data[f"memberships-{i}-department"] = str(m.department_id)
            data[f"memberships-{i}-role"] = (roles or {}).get(m.department.name, m.role)
            if m.department.name in delete:
                data[f"memberships-{i}-DELETE"] = "on"
        return self.client.post(f"/admin/accounts/userprofile/{self.profile.pk}/change/", data)

    def test_changelists_render_departments(self):
        for url in ("/admin/accounts/userprofile/", "/admin/accounts/department/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.get("/admin/accounts/userprofile/")
        self.assertContains(r, "机械、电气")  # 「通用」只用于权限，不刷屏
        self.assertContains(r, "当管理员的部门")
        form = self.client.get(f"/admin/accounts/userprofile/{self.profile.pk}/change/")
        self.assertContains(form, "部门身份")     # inline：逐部门级别

    def test_inline_sets_role_per_department(self):
        """在 RSE 当管理员、在其它部门只是成员：逐部门级别各存各的。"""
        r = self._membership_post({"电气": UserProfile.Role.DEPT_ADMIN})
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:600])
        roles = profile_roles(self.user)
        self.assertEqual(roles["电气"], UserProfile.Role.DEPT_ADMIN)
        self.assertEqual(roles["机械"], UserProfile.Role.USER)
        self.assertEqual(user_role(self.user), UserProfile.Role.DEPT_ADMIN)  # 汇总同步

    def test_saving_without_general_row_puts_it_back(self):
        """漏了「通用」那行也会被补回（可见范围不能被人删没）。"""
        self.profile.memberships.filter(department__name=DEPARTMENT_GENERAL).delete()
        self._membership_post()
        self.assertIn(DEPARTMENT_GENERAL, profile_departments(self.user))

    def test_general_department_cannot_be_deleted(self):
        general = Department.objects.get(name=DEPARTMENT_GENERAL)
        self.assertEqual(
            self.client.get(f"/admin/accounts/department/{general.pk}/delete/").status_code, 403)
        self.assertTrue(Department.objects.filter(pk=general.pk).exists())
        # inline 里「通用」那一行勾删除也会被表单挡下，别的部门能删
        row = self.profile.memberships.get(department__name=DEPARTMENT_GENERAL)
        r = self._membership_post(delete={DEPARTMENT_GENERAL})
        self.assertEqual(r.status_code, 200)          # 回渲染报错，不落库
        self.assertContains(r, "不能删")
        self.assertTrue(UserDepartment.objects.filter(pk=row.pk).exists())
        self._membership_post(delete={"电气"})
        self.assertNotIn("电气", profile_departments(self.user))
