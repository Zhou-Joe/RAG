"""accounts：注册部门策略测试（安全回归）。"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from kb.models import KnowledgeBase

from .models import DEPARTMENT_GENERAL, UserProfile


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
