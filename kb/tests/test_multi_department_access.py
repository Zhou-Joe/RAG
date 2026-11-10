"""一个账号可属多个部门：读范围取并集，「通用」是人人默认的部门但仍不可写。

旧模型只有 `UserProfile.department` 一个字符串，跨部门的工程师要么看不到自己
另一半资料，要么被迫把库做成「通用」。现在 `UserProfile.departments` 可多选，
`user_departments()` 永远包含「通用」，而写权限仍按 `user_write_departments()`
（默认不含「通用」，主部门就是「通用」的账号除外）判定。
"""
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import (Department, UserProfile, assign_departments,
                             user_department, user_departments,
                             user_write_departments)
from kb import access
from kb.models import DEPARTMENT_GENERAL, KnowledgeBase

RSE, AFE, FOS = 'RSE', 'AFE', 'FOS'


class MultiDepartmentFixture(TestCase):
    def setUp(self):
        User = get_user_model()
        self.gadmin = User.objects.create_superuser('multi-admin', 'ga@example.test', 'pw')
        assign_departments(self.gadmin, [DEPARTMENT_GENERAL])
        self.rse = User.objects.create_user('rse-only')
        UserProfile.objects.create(user=self.rse, department=RSE,
                                   role=UserProfile.Role.DEPT_ADMIN)
        self.both = User.objects.create_user('rse-and-afe')
        UserProfile.objects.create(user=self.both, department=RSE,
                                   role=UserProfile.Role.DEPT_ADMIN)
        assign_departments(self.both, [RSE, AFE])
        self.keeper = User.objects.create_user('general-keeper')  # 主部门＝通用
        UserProfile.objects.create(user=self.keeper, department=DEPARTMENT_GENERAL,
                                   role=UserProfile.Role.DEPT_ADMIN)
        assign_departments(self.keeper, [DEPARTMENT_GENERAL])

        self.general = KnowledgeBase.objects.create(
            name='通用手册库', slug='multi-general', is_folder=True, visibility='shared',
            department=DEPARTMENT_GENERAL, created_by=self.gadmin)
        self.rse_lib = KnowledgeBase.objects.create(
            name='RSE 库', slug='multi-rse', is_folder=True, visibility='shared',
            department=RSE, created_by=self.rse)
        self.afe_lib = KnowledgeBase.objects.create(
            name='AFE 库', slug='multi-afe', is_folder=True, visibility='shared',
            department=AFE, created_by=self.rse)
        self.fos_lib = KnowledgeBase.objects.create(
            name='FOS 库', slug='multi-fos', is_folder=True, visibility='shared',
            department=FOS, created_by=self.rse)


class DepartmentMembershipTests(MultiDepartmentFixture):
    def test_general_is_a_department_assigned_to_everyone(self):
        """「通用」本身是登记的部门，并且出现在每个账号的部门集合里。"""
        self.assertTrue(Department.objects.filter(name=DEPARTMENT_GENERAL).exists())
        for user in (self.gadmin, self.rse, self.both, self.keeper):
            with self.subTest(user=user.username):
                self.assertEqual(user_departments(user)[0], DEPARTMENT_GENERAL)

    def test_membership_union_and_order(self):
        self.assertEqual(user_departments(self.rse), [DEPARTMENT_GENERAL, RSE])
        self.assertEqual(set(user_departments(self.both)), {DEPARTMENT_GENERAL, RSE, AFE})

    def test_primary_department_counts_even_without_registry_row(self):
        """旧数据：只有主部门字符串、没有多选也没有登记行 → 照样算进可见范围。"""
        Department.objects.filter(name='legacy-dept').delete()
        profile = self.rse.profile          # 用缓存里的那个实例，改完整个人跟着变
        profile.department = 'legacy-dept'
        profile.save(update_fields=['department', 'updated_at'])
        self.rse._fos_departments = None
        self.assertIn('legacy-dept', user_departments(self.rse))

    def test_general_cannot_be_unassigned(self):
        """越权也拿不掉「通用」：取消勾选后仍然挂在档案上。"""
        assign_departments(self.both, [RSE])
        self.assertIn(DEPARTMENT_GENERAL, user_departments(self.both))
        self.assertTrue(self.both.profile.departments.filter(name=DEPARTMENT_GENERAL).exists())

    def test_registered_user_gets_general_row(self):
        """注册不受理部门自选，但必须拿到默认的「通用」部门。"""
        r = self.client.post(reverse('accounts:register'), {
            'username': 'fresh-one', 'password1': 's3cure-pass-9', 'password2': 's3cure-pass-9',
            'departments': [AFE],  # 伪造多选也要被忽略
        })
        self.assertEqual(r.status_code, 302)
        profile = UserProfile.objects.get(user__username='fresh-one')
        self.assertEqual(profile.department, DEPARTMENT_GENERAL)
        self.assertEqual(list(profile.departments.values_list('name', flat=True)),
                         [DEPARTMENT_GENERAL])


class WriteScopeTests(MultiDepartmentFixture):
    def test_read_scope_unions_all_departments(self):
        slugs = set(KnowledgeBase.objects.filter(access.kb_q(self.both))
                    .values_list('slug', flat=True))
        self.assertEqual(slugs, {'multi-general', 'multi-rse', 'multi-afe'})
        self.assertNotIn('multi-fos', slugs)

    def test_manage_scope_covers_every_owned_department(self):
        managed = set(KnowledgeBase.objects.filter(access.managed_q(self.both))
                      .values_list('slug', flat=True))
        self.assertEqual(managed, {'multi-rse', 'multi-afe'})

    def test_general_is_readable_but_not_writable_for_multi_dept_user(self):
        self.assertTrue(access.kb_accessible(self.both, self.general))
        self.assertFalse(access.can_manage_kb(self.both, self.general))
        rows = {kb.slug: kb for kb in access.visible_kbs(self.both)}
        self.assertIn('multi-general', rows)
        self.assertFalse(rows['multi-general'].can_manage)

    def test_general_primary_account_still_maintains_general(self):
        """主部门＝「通用」的部门管理员保留旧行为：能维护公共库。"""
        self.assertEqual(user_department(self.keeper), DEPARTMENT_GENERAL)
        self.assertIn(DEPARTMENT_GENERAL, user_write_departments(self.keeper))
        self.assertTrue(access.can_manage_kb(self.keeper, self.general))

    def test_extra_general_membership_grants_no_write(self):
        """只有「通用」+RSE 的账号不能因为默认挂着通用就写公共库。"""
        self.assertNotIn(DEPARTMENT_GENERAL, user_write_departments(self.both))
        self.assertFalse(access.can_manage_kb(self.both, self.general))


class CreateLibraryTests(MultiDepartmentFixture):
    def _create(self, user, name, level, department):
        self.client.force_login(user)
        return self.client.post(reverse('kb:manage_list'), {
            'action': 'create_kb', 'name': name, 'access_level': level,
            'department': department,
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_department_library_in_second_department(self):
        r = self._create(self.both, 'AFE 新库', 'department', AFE)
        self.assertTrue(r.json()['ok'], r.json())
        self.assertEqual(KnowledgeBase.objects.get(name='AFE 新库').department, AFE)

    def test_foreign_department_falls_back_to_own(self):
        """部门管理员伪造别的部门 → 落到自己的主部门，不越权。"""
        r = self._create(self.both, '越权库', 'department', FOS)
        self.assertTrue(r.json()['ok'], r.json())
        self.assertEqual(KnowledgeBase.objects.get(name='越权库').department, RSE)

    def test_general_library_needs_general_primary(self):
        self.assertFalse(self._create(self.both, '越权通用库', 'general', DEPARTMENT_GENERAL).json()['ok'])
        self.assertFalse(KnowledgeBase.objects.filter(name='越权通用库').exists())
        self.assertTrue(self._create(self.keeper, '公共新库', 'general', DEPARTMENT_GENERAL).json()['ok'])

    def test_general_primary_does_not_grant_general_write_without_general_role(self):
        # An account can have 通用 as its primary and admin rights only in RSE.
        user = get_user_model().objects.create_user('general-primary-rse-admin')
        UserProfile.objects.create(user=user, department=DEPARTMENT_GENERAL)
        assign_departments(user, [DEPARTMENT_GENERAL, RSE],
                           roles={DEPARTMENT_GENERAL: UserProfile.Role.USER,
                                  RSE: UserProfile.Role.DEPT_ADMIN})
        self.assertFalse(self._create(user, 'Unauthorized public library',
                                      'general', DEPARTMENT_GENERAL).json()['ok'])

    def test_individual_library_uses_primary_department(self):
        r = self._create(self.both, '我的草稿', 'individual', '')
        self.assertTrue(r.json()['ok'], r.json())
        kb = KnowledgeBase.objects.get(name='我的草稿')
        self.assertEqual((kb.visibility, kb.department), ('individual', RSE))


class AgentScopeTests(SimpleTestCase):
    """不访库：只验部门参数归一化与「只有通用」的范围判定。"""

    def test_only_general_is_still_a_scope(self):
        """department=['通用'] 仍是「只有通用」的范围，不能退化成不过滤。"""
        from kb.agent import _dept_scope

        scoped, allowed = _dept_scope([DEPARTMENT_GENERAL])
        self.assertTrue(scoped)
        self.assertEqual(allowed, [DEPARTMENT_GENERAL])
        # 完全没传（内部调用）才不限
        self.assertEqual(_dept_scope(''), (False, []))
        self.assertEqual(_dept_scope([DEPARTMENT_GENERAL, RSE]),
                         (True, sorted([DEPARTMENT_GENERAL, RSE])))

    def test_agent_accepts_department_list(self):
        from kb.agent import _dept_names

        self.assertEqual(_dept_names(RSE), [RSE])
        self.assertEqual(_dept_names([RSE, AFE, RSE]), [RSE, AFE])
        self.assertEqual(_dept_names(''), [])
        self.assertEqual(_dept_names(None), [])


class PageAndPoolTests(MultiDepartmentFixture):
    def test_manage_page_offers_every_own_department(self):
        """建库表单的下拉列出全部所属部门，而不是只有主部门。"""
        self.client.force_login(self.both)
        r = self.client.get(reverse('kb:manage_list'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(r.context['my_departments']), sorted([RSE, AFE]))
        r2 = self.client.get(reverse('accounts:profile'))
        self.assertIn(AFE, r2.context['user_departments'])

    def test_pool_leads_with_general_and_lists_assigned(self):
        pool = access.all_departments()
        self.assertEqual(pool[0], DEPARTMENT_GENERAL)
        self.assertIn(RSE, pool)
        self.assertIn(AFE, pool)
