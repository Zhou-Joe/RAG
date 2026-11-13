"""「通用」知识库：对所有部门开放阅读，写操作仍限全局管理员。

回归的 bug：`_permission_q` 的写范围按部门精确匹配、不含「通用」，而管理页与
详情页都只用写范围（`managed_kbs` / `can_manage_kb`），于是被标成「通用」的库
对其它部门账号既不出现在列表里、直接访问还 404——与 models.py 里
「通用 = 所有部门可见」的定义相反。现在列表/详情走读范围并标 can_manage，
写端点逐个卡 can_manage_kb（含本视图内联的上传分支）。
"""
from unittest.mock import patch

import glob
import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import UserProfile
from kb import access
from kb.models import (DEPARTMENT_GENERAL, Document, FeedRun, KbTracker, KnowledgeBase,
                       KnowledgeFeed, TrackerRow)

RSE, AFE, FOS = 'RSE', 'AFE', 'FOS'


class GeneralLibraryAccessTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.gadmin = User.objects.create_superuser('gen-admin', 'ga@example.test', 'pw')
        UserProfile.objects.update_or_create(
            user=self.gadmin, defaults={'department': DEPARTMENT_GENERAL})
        self.rse = User.objects.create_user('rse-admin')
        self.afe = User.objects.create_user('afe-admin')
        self.pleb = User.objects.create_user('fos-user')
        for user, dept in ((self.rse, RSE), (self.afe, AFE), (self.pleb, FOS)):
            UserProfile.objects.update_or_create(
                user=user, defaults={'department': dept,
                                     'role': UserProfile.Role.DEPT_ADMIN
                                     if user is not self.pleb else UserProfile.Role.USER})

        # 通用库：全局管理员建的共享文件夹 + 子文档库 + 文档
        self.general = KnowledgeBase.objects.create(
            name='游乐设备维修手册', slug='general-lib', is_folder=True,
            visibility='shared', department=DEPARTMENT_GENERAL, created_by=self.gadmin)
        self.general_child = KnowledgeBase.objects.create(
            name='gearbox.pdf', slug='general-lib-child', parent=self.general,
            chunk_count=5, created_by=self.gadmin)
        self.doc = Document.objects.create(
            kb=self.general_child, original_name='gearbox-manual.pdf', status='completed',
            chunk_count=5, md_content='齿轮箱 润滑 与 油脂 更换 周期')

        self.rse_lib = KnowledgeBase.objects.create(
            name='RSE 专用库', slug='rse-lib', is_folder=True,
            visibility='shared', department=RSE, created_by=self.rse)
        self.rse_child = KnowledgeBase.objects.create(
            name='weld.pdf', slug='rse-lib-child', parent=self.rse_lib,
            chunk_count=3, created_by=self.rse)
        Document.objects.create(kb=self.rse_child, original_name='weld.pdf',
                                status='completed', chunk_count=3, md_content='焊接要求')

        self.afe_lib = KnowledgeBase.objects.create(
            name='AFE 专用库', slug='afe-lib', is_folder=True,
            visibility='shared', department=AFE, created_by=self.afe)
        self.private = KnowledgeBase.objects.create(
            name='个人草稿', slug='afe-private', is_folder=True, visibility='individual',
            department=AFE, created_by=self.afe)

    # ---------- 可见性（读） ----------

    def test_general_library_is_listed_for_every_department(self):
        for user in (self.rse, self.afe, self.pleb, self.gadmin):
            with self.subTest(user=user.username):
                rows = {kb.slug: kb for kb in access.visible_kbs(user)}
                self.assertIn(self.general.slug, rows)
                self.client.force_login(user)
                # 能管的账号：卡片网格；只读账号：底部公共库列表。两处都渲染名字。
                self.assertContains(self.client.get(reverse('kb:manage_list')), self.general.name)

    def test_public_library_is_listed_below_main_categories_not_as_a_card(self):
        for user in (self.rse, self.afe, self.pleb):
            with self.subTest(user=user.username):
                rows = {kb.slug: kb for kb in access.visible_kbs(user)}
                self.assertFalse(rows[self.general.slug].can_manage)
                self.client.force_login(user)
                page = self.client.get(reverse('kb:manage_list'))
                main = ([kb.slug for kb in page.context['kbs']]
                        + [kb.slug for kb in page.context['business_kbs']])
                self.assertNotIn(self.general.slug, main)   # 主分类不再列它
                self.assertIn(self.general.slug, [kb.slug for kb in page.context['read_kbs']])
                self.assertContains(page, '公共知识库')
                self.assertContains(page, '只读')
                # 卡片带 data-slug；只读列表是普通链接，不是卡片
                self.assertNotContains(page, f'data-slug="{self.general.slug}"')
                self.assertContains(page, f'href="/kb/manage/{self.general.slug}/"')
                self.assertNotContains(page, f'/kb/manage/{self.general.slug}/delete/')

    def test_manageable_libraries_stay_in_the_main_categories(self):
        self.client.force_login(self.rse)
        page = self.client.get(reverse('kb:manage_list'))
        self.assertIn(self.rse_lib.slug, [kb.slug for kb in page.context['kbs']])
        self.assertNotIn(self.rse_lib.slug, [kb.slug for kb in page.context['read_kbs']])
        self.client.force_login(self.afe)
        self.assertIn(self.private.slug,                 # 自己的个人库也在主分类
                      [kb.slug for kb in
                       self.client.get(reverse('kb:manage_list')).context['kbs']])
        # 全局管理员：可见即可管，底部只读列表为空
        self.client.force_login(self.gadmin)
        page = self.client.get(reverse('kb:manage_list'))
        self.assertEqual([kb.slug for kb in page.context['read_kbs']], [])
        self.assertIn(self.general.slug, [kb.slug for kb in page.context['kbs']])

    def test_global_admin_still_gets_write_controls_on_general_library(self):
        rows = {kb.slug: kb for kb in access.visible_kbs(self.gadmin)}
        self.assertTrue(rows[self.general.slug].can_manage)
        self.client.force_login(self.gadmin)
        page = self.client.get(reverse('kb:manage_list'))
        self.assertNotContains(page, f'/kb/manage/{self.general.slug}/delete/')
        detail = self.client.get(reverse('kb:manage_detail', args=[self.general.slug]))
        self.assertTrue(detail.context['can_manage'])
        self.assertContains(detail, 'id="descEditBtn"')

    def test_detail_page_opens_read_only_with_document_list(self):
        self.client.force_login(self.rse)
        page = self.client.get(reverse('kb:manage_detail', args=[self.general.slug]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, self.doc.original_name)   # 只读也要看得到内容
        self.assertContains(page, 'class="readonly-note"')
        self.assertFalse(page.context['can_manage'])
        # 探针用服务端渲染的具体 URL / data-doc-id：页面自带 JS 里也有
        # 'doc-actions'、'uploadForm' 等同名字串，只搜类名会误判。
        self.assertNotContains(page, 'id="uploadForm"')
        self.assertNotContains(page, 'id="descEditBtn"')
        self.assertNotContains(
            page, f'/kb/manage/{self.general.slug}/doc/{self.doc.pk}/retry/')

    def test_document_status_api_is_readable(self):
        self.client.force_login(self.rse)
        response = self.client.get(reverse('kb:doc_status', args=[self.general.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['docs'][0]['name'], self.doc.original_name)

    def test_general_documents_are_searchable_by_other_departments(self):
        for user in (self.rse, self.afe, self.pleb, self.gadmin):
            with self.subTest(user=user.username):
                self.assertIn(self.general_child.slug, access.accessible_doc_slugs(user))

    def test_detail_page_shows_write_controls_for_global_admin(self):
        self.client.force_login(self.gadmin)
        page = self.client.get(reverse('kb:manage_detail', args=[self.general.slug]))
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context['can_manage'])
        self.assertContains(page, 'id="uploadForm"')
        self.assertContains(page, 'id="descEditBtn"')
        self.assertContains(
            page, f'/kb/manage/{self.general.slug}/doc/{self.doc.pk}/retry/')
        self.assertNotContains(page, 'class="readonly-note"')

    # ---------- 写权限仍然关闭 ----------

    def test_uploads_to_general_library_are_rejected(self):
        self.client.force_login(self.rse)
        url = reverse('kb:manage_detail', args=[self.general.slug])
        with patch('kb.pipeline.process_document_async'):
            denied = self.client.post(
                url, {'file': SimpleUploadedFile('sneak.pdf', b'pdf-bytes', 'application/pdf')},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(denied.status_code, 403)
            self.assertFalse(denied.json()['ok'])
            plain = self.client.post(
                url, {'file': SimpleUploadedFile('sneak.pdf', b'pdf-bytes', 'application/pdf')})
            self.assertEqual(plain.status_code, 302)
        self.assertEqual(Document.objects.filter(kb=self.general_child).count(), 1)
        self.assertFalse(Document.objects.filter(original_name='sneak.pdf').exists())

    def test_other_write_endpoints_stay_closed(self):
        self.client.force_login(self.rse)
        self.assertEqual(self.client.post(
            reverse('kb:manage_delete', args=[self.general.slug])).status_code, 404)
        self.assertEqual(self.client.post(
            reverse('kb:kb_rename', args=[self.general.slug]),
            {'name': 'hijacked'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest').status_code, 404)
        self.assertEqual(self.client.get(
            reverse('kb:tracker', args=[self.general.slug])).status_code, 404)
        self.assertEqual(self.client.post(
            reverse('kb:doc_delete', args=[self.general_child.slug, self.doc.pk])).status_code, 404)
        self.assertTrue(KnowledgeBase.objects.filter(pk=self.general.pk).exists())
        self.assertTrue(Document.objects.filter(pk=self.doc.pk).exists())

    def test_general_library_is_not_manageable_by_other_departments(self):
        for user in (self.rse, self.afe, self.pleb):
            with self.subTest(user=user.username):
                self.assertFalse(access.can_manage_kb(user, self.general))
                self.assertFalse(
                    access.managed_kbs(user).filter(pk=self.general.pk).exists())

    def test_global_admin_can_upload_to_general_library(self):
        self.client.force_login(self.gadmin)
        with patch('kb.pipeline.process_document_async'):
            response = self.client.post(
                reverse('kb:manage_detail', args=[self.general.slug]),
                {'file': SimpleUploadedFile('ok.pdf', b'pdf-bytes', 'application/pdf')},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(Document.objects.filter(original_name='ok.pdf').exists())

    # ---------- 其它范围不被放宽 ----------

    def test_department_library_stays_hidden_from_other_departments(self):
        self.client.force_login(self.afe)
        page = self.client.get(reverse('kb:manage_list'))
        self.assertNotContains(page, self.rse_lib.name)
        self.assertEqual(
            self.client.get(reverse('kb:manage_detail', args=[self.rse_lib.slug])).status_code,
            404)
        self.assertNotIn(self.rse_child.slug, access.accessible_doc_slugs(self.afe))

    def test_individual_library_hidden_from_everyone_but_owner(self):
        for user in (self.rse, self.pleb, self.gadmin):
            with self.subTest(user=user.username):
                self.assertNotIn(self.private.slug,
                                 {kb.slug for kb in access.visible_kbs(user)})
                self.client.force_login(user)
                self.assertNotContains(self.client.get(reverse('kb:manage_list')),
                                       self.private.name)
                self.assertEqual(
                    self.client.get(
                        reverse('kb:manage_detail', args=[self.private.slug])).status_code, 404)
        self.assertIn(self.private.slug, {kb.slug for kb in access.visible_kbs(self.afe)})

    def test_global_admin_still_sees_and_manages_other_department_libraries(self):
        """浏览范围取「可读 ∪ 可写」：否则全局管理员会丢掉自己能管的别部门库
        （它们的部门不是「通用」，不在管理员的读范围里）。"""
        rows = {kb.slug: kb for kb in access.visible_kbs(self.gadmin)}
        self.assertIn(self.rse_lib.slug, rows)
        self.assertTrue(rows[self.rse_lib.slug].can_manage)
        self.client.force_login(self.gadmin)
        self.assertContains(self.client.get(reverse('kb:manage_list')), self.rse_lib.name)
        page = self.client.get(reverse('kb:manage_detail', args=[self.rse_lib.slug]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'weld.pdf')      # 文档列表也照常

    def test_browse_scope_equals_readable_or_manageable(self):
        """浏览页的不变式：列出来 ⇔ 能读或能管。

        全局管理员的查询与浏览都覆盖全部共享库；其他账号仍限「通用」∪ 所属部门。
        个人知识库始终仅对其拥有者可见。
        """
        nodes = list(KnowledgeBase.objects.all())
        for user in (self.rse, self.afe, self.pleb, self.gadmin):
            browsable = set(KnowledgeBase.objects.filter(access.visible_q(user))
                            .values_list('pk', flat=True))
            for node in nodes:
                with self.subTest(user=user.username, node=node.slug):
                    self.assertEqual(
                        node.pk in browsable,
                        access.kb_accessible(user, node) or access.can_manage_kb(user, node))

    def test_search_scope_still_excludes_other_department_libraries(self):
        """只读放开不应顺带放宽搜索：别部门的专用库依旧搜不到。"""
        for user in (self.rse, self.afe):
            with self.subTest(user=user.username):
                slugs = access.accessible_doc_slugs(user)
                self.assertIn(self.general_child.slug, slugs)
                other = self.rse_child if user is self.afe else self.afe_lib
                self.assertNotIn(getattr(other, 'slug', ''), slugs)

    def test_anonymous_users_get_nothing(self):
        self.assertFalse(access.visible_kbs(self._anonymous()))
        self.assertFalse(access.accessible_doc_slugs(self._anonymous()))

    @staticmethod
    def _anonymous():
        from django.contrib.auth.models import AnonymousUser
        return AnonymousUser()


class TrackerReadAccessTests(TestCase):
    """公共库的追踪表：只读账号能核对记录、能导 Excel，不能改。"""

    def setUp(self):
        User = get_user_model()
        self.gadmin = User.objects.create_superuser('trk-admin', 'ta@example.test', 'pw')
        UserProfile.objects.update_or_create(
            user=self.gadmin, defaults={'department': DEPARTMENT_GENERAL})
        self.rse = User.objects.create_user('trk-rse')
        UserProfile.objects.update_or_create(
            user=self.rse, defaults={'department': RSE, 'role': UserProfile.Role.DEPT_ADMIN})
        self.kb = KnowledgeBase.objects.create(
            name='通用追踪库', slug='trk-general', is_folder=True,
            visibility='shared', department=DEPARTMENT_GENERAL, created_by=self.gadmin)
        self.child = KnowledgeBase.objects.create(
            name='report.pdf', slug='trk-child', parent=self.kb)
        self.doc = Document.objects.create(
            kb=self.child, original_name='report.pdf', status='completed',
            chunk_count=1, md_content='定检报告')
        self.tracker = KbTracker.objects.create(
            kb=self.kb, enabled=True, fields=[{"label": "设备编号"}])
        self.row = TrackerRow.objects.create(
            tracker=self.tracker, document=self.doc, status='done',
            values={"设备编号": "P8-01"})

    def test_readonly_department_can_open_and_sees_rows(self):
        self.client.force_login(self.rse)
        page = self.client.get(reverse('kb:tracker', args=[self.kb.slug]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'P8-01')      # 只读也要能核对内容
        self.assertContains(page, '只读')
        self.assertFalse(page.context['can_manage'])
        # 分析面板（趋势洞察）随阅读权限一起开放，不另卡管理员。

    def test_readonly_can_download_xlsx(self):
        self.client.force_login(self.rse)
        response = self.client.get(reverse('kb:tracker', args=[self.kb.slug]), {'xlsx': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('spreadsheetml', response['Content-Type'])
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertGreater(len(response.content), 1000)

    def test_readonly_page_has_no_write_controls(self):
        self.client.force_login(self.rse)
        body = self.client.get(reverse('kb:tracker', args=[self.kb.slug])).content.decode()
        # 探针取“控件 markup”形式（带引号的属性），页面共用的确认弹窗与 JS 里
        # 也有 trk-confirm / trk-retry 同名字串，直搜类名会误判。
        for probe in ('id="trkEnable"', 'id="trkDisable"', 'id="trkBackfill"',
                      'id="trkConfigForm"', 'name="row_delete"',
                      'class="btn sm outline trk-retry"', 'data-payload=', 'c-acts">操作'):
            self.assertNotIn(probe, body)
        self.assertIn('?xlsx=1', body)          # 导出入口保留
        self.assertIn('id="trkConfirmDialog"', body)   # 共用的确认弹窗壳留着也无害（无触发按钮）

    def test_readonly_writes_are_rejected(self):
        self.client.force_login(self.rse)
        url = reverse('kb:tracker', args=[self.kb.slug])
        ajax = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
        for payload in ({'action': 'config_save', 'fields_text': '设备编号\n被注入'},
                        {'action': 'disable'},
                        {'action': 'enable'},
                        {'action': 'backfill'},
                        {'action': 'row_confirm', 'row_id': self.row.pk},
                        {'action': 'row_retry', 'row_id': self.row.pk},
                        {'action': 'row_delete', 'row_id': self.row.pk}):
            with self.subTest(action=payload['action']):
                denied = self.client.post(url, payload, **ajax)
                self.assertEqual(denied.status_code, 403)
                self.assertFalse(denied.json()['ok'])
        form = self.client.post(url, {'action': 'row_delete', 'row_id': self.row.pk})
        self.assertEqual(form.status_code, 302)
        self.assertTrue(TrackerRow.objects.filter(pk=self.row.pk).exists())
        self.tracker.refresh_from_db()
        self.assertTrue(self.tracker.enabled)
        self.assertEqual([f['label'] for f in self.tracker.fields], ['设备编号'])

    def test_manager_writes_still_work(self):
        self.client.force_login(self.gadmin)
        response = self.client.post(
            reverse('kb:tracker', args=[self.kb.slug]),
            {'action': 'config_save', 'fields_text': '设备编号\n检验日期'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200, response.content)
        self.tracker.refresh_from_db()
        self.assertEqual([f['label'] for f in self.tracker.fields], ['设备编号', '检验日期'])

    def test_disabled_or_missing_tracker_is_not_opened_for_readers(self):
        KbTracker.objects.filter(kb=self.kb).update(enabled=False)
        self.client.force_login(self.rse)
        self.assertEqual(
            self.client.get(reverse('kb:tracker', args=[self.kb.slug])).status_code, 404)
        other = KnowledgeBase.objects.create(
            name='通用无表库', slug='trk-none', is_folder=True,
            visibility='shared', department=DEPARTMENT_GENERAL, created_by=self.gadmin)
        before = KbTracker.objects.count()
        self.assertEqual(
            self.client.get(reverse('kb:tracker', args=[other.slug])).status_code, 404)
        self.assertEqual(KbTracker.objects.count(), before)   # 不给只读账号建空配置

    def test_library_page_links_the_tracker_for_readers(self):
        self.client.force_login(self.rse)
        self.assertContains(
            self.client.get(reverse('kb:manage_detail', args=[self.kb.slug])), 'id="trkEntry"')


class TrackerPaginationTests(TestCase):
    """追跟表分页：上千行不再整表塞给浏览器，导出仍是全量。"""

    ROWS = 120

    def setUp(self):
        User = get_user_model()
        self.gadmin = User.objects.create_superuser('page-admin', 'pa@example.test', 'pw')
        UserProfile.objects.update_or_create(
            user=self.gadmin, defaults={'department': DEPARTMENT_GENERAL})
        self.rse = User.objects.create_user('page-rse')
        UserProfile.objects.update_or_create(
            user=self.rse, defaults={'department': RSE, 'role': UserProfile.Role.DEPT_ADMIN})
        self.kb = KnowledgeBase.objects.create(
            name='上千行追跟库', slug='page-kb', is_folder=True,
            visibility='shared', department=DEPARTMENT_GENERAL, created_by=self.gadmin)
        self.child = KnowledgeBase.objects.create(name='rows.pdf', slug='page-child', parent=self.kb)
        docs = Document.objects.bulk_create([
            Document(kb=self.child, original_name=f'r{i}.pdf', status='completed',
                     chunk_count=1, md_content='x') for i in range(self.ROWS)])
        self.tracker = KbTracker.objects.create(kb=self.kb, enabled=True,
                                                fields=[{"label": "设备编号"}])
        TrackerRow.objects.bulk_create([
            TrackerRow(tracker=self.tracker, document=d, status='done',
                       values={"设备编号": f"P8-{i:03d}"}) for i, d in enumerate(docs)])
        self.url = reverse('kb:tracker', args=[self.kb.slug])

    @staticmethod
    def body(response):
        return response.content.decode('utf-8', 'ignore')

    def test_first_page_limited_and_count_is_total(self):
        self.client.force_login(self.gadmin)
        body = self.body(self.client.get(self.url))
        self.assertEqual(body.count('class="trk-row"'), 50)          # 默认每页 50
        self.assertIn(f'共 {self.ROWS} 条', body)                    # 总数而非当页数
        self.assertIn('第 1 页', body)
        self.assertIn('page=2', body)                                # 下一页链接

    def test_second_page_shows_the_rest(self):
        self.client.force_login(self.gadmin)
        body = self.body(self.client.get(self.url, {'page': 2}))
        self.assertEqual(body.count('class="trk-row"'), 50)          # 120/50 → 50+50+20
        self.assertIn('第 2 页', body)

    def test_per_page_choices_and_bad_value_falls_back(self):
        self.client.force_login(self.gadmin)
        self.assertEqual(self.body(self.client.get(self.url, {'per': 100}))
                         .count('class="trk-row"'), 100)
        # 非白名单值不得放大成无限查询
        self.assertEqual(self.body(self.client.get(self.url, {'per': 5000}))
                         .count('class="trk-row"'), 50)
        self.assertEqual(self.body(self.client.get(self.url, {'per': 'abc'}))
                         .count('class="trk-row"'), 50)

    def test_out_of_range_page_clamps(self):
        self.client.force_login(self.gadmin)
        body = self.body(self.client.get(self.url, {'page': 999}))
        self.assertEqual(body.count('class="trk-row"'), self.ROWS - 100)   # 末页只剩 20 行
        self.assertIn('第 3 页', body)

    def test_page_links_preserve_other_params(self):
        self.client.force_login(self.gadmin)
        body = self.body(self.client.get(self.url, {'per': 25}))
        self.assertEqual(body.count('class="trk-row"'), 25)
        self.assertIn('per=25', body)                                # 翻页不丢档位
        self.assertIn('class="trk-page is-cur"', body)
        self.assertIn('每页', body)

    def test_readonly_users_are_paginated_too(self):
        self.client.force_login(self.rse)
        body = self.body(self.client.get(self.url))
        self.assertEqual(body.count('class="trk-row"'), 50)
        self.assertIn(f'共 {self.ROWS} 条', body)
        self.assertIn('?xlsx=1', body)

    def test_excel_export_still_covers_every_row(self):
        from io import BytesIO
        from openpyxl import load_workbook
        self.client.force_login(self.rse)
        response = self.client.get(self.url, {'xlsx': '1'})
        sheet = load_workbook(BytesIO(response.content)).active
        self.assertEqual(sheet.max_row, self.ROWS + 1)               # 表头 + 全量 120 行

    def test_page_payload_shrinks_with_pagination(self):
        self.client.force_login(self.gadmin)
        self.assertLess(len(self.client.get(self.url).content), 400_000)


class TrendAnalysisReadAccessTests(TestCase):
    """采集参数分析（趋势洞察）：能读公共库的账号就能用。

    这个面板不跑模型（`tracker_analysis.analyze()` 只是对表内已完成采集行做
    DB 聚合 + Decimal 运算，上限 120 周期 / 200 对象），所以随阅读权限开放；
    写操作（字段配置/启停/补录/行增删）仍只限管理员。
    """

    def setUp(self):
        User = get_user_model()
        self.gadmin = User.objects.create_superuser('ta-admin', 'ta2@example.test', 'pw')
        UserProfile.objects.update_or_create(
            user=self.gadmin, defaults={'department': DEPARTMENT_GENERAL})
        self.rse = User.objects.create_user('ta-rse')
        UserProfile.objects.update_or_create(
            user=self.rse, defaults={'department': RSE, 'role': UserProfile.Role.DEPT_ADMIN})
        self.afe = User.objects.create_user('ta-afe')
        UserProfile.objects.update_or_create(
            user=self.afe, defaults={'department': AFE, 'role': UserProfile.Role.DEPT_ADMIN})
        self.kb = KnowledgeBase.objects.create(
            name='通用等待时间', slug='ta-kb', category='business', is_folder=True,
            visibility='shared', department=DEPARTMENT_GENERAL, created_by=self.gadmin)
        self.tracker = KbTracker.objects.create(
            kb=self.kb, enabled=True,
            fields=[{'label': label} for label in ('标题', '场次数', '采集时间', '采集日期')])
        self.feed = KnowledgeFeed.objects.create(kb=self.kb, name='官网采集',
                                                 timezone='Asia/Shanghai', created_by=self.gadmin)
        self.row = self._make_row('2026-01-01T08:00:00+08:00', '3')
        self._make_row('2026-01-02T08:00:00+08:00', '9')
        self.url = reverse('kb:tracker_analysis', args=[self.kb.slug])

    def _make_row(self, stamp, value):
        from datetime import datetime
        run = FeedRun.objects.create(feed=self.feed, lease_token='test', status='completed')
        FeedRun.objects.filter(pk=run.pk).update(started_at=datetime.fromisoformat(stamp))
        return TrackerRow.objects.create(
            tracker=self.tracker, feed=self.feed, feed_run=run, external_id='show-1',
            status='done', values={'标题': '海盗演出', '场次数': value,
                                   '采集时间': stamp, '采集日期': '2026-01-01'})

    def test_readonly_user_can_load_selectors_and_analysis(self):
        self.client.force_login(self.rse)
        options = self.client.get(self.url)
        self.assertEqual(options.status_code, 200)
        data = options.json()
        self.feed.refresh_from_db()          # 数据源名称会被命名规则同步，取库里的值比
        self.assertEqual(data['items'][0]['source'], self.feed.name)
        self.assertEqual(data['items'][0]['title'], '海盗演出')
        self.assertEqual(data['items'][0]['sample_count'], 2)
        self.assertIn('场次数', data['parameters'])
        result = self.client.get(self.url, {'row': self.row.pk, 'parameter': '场次数'})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['periods'])

    def test_readonly_auto_feed_tracker_page_keeps_records_without_duplicate_panel(self):
        self.client.force_login(self.rse)
        page = self.client.get(reverse('kb:tracker', args=[self.kb.slug]))
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context['show_analysis'])
        self.assertNotContains(page, 'id="taItem"')
        self.assertNotContains(page, '<button type="button" class="trk-analyze"')
        self.assertContains(page, '海盗演出')
        self.assertFalse(page.context['can_manage'])          # 但依旧不能写

    def test_disabled_tracker_closed_to_readers_open_to_admins(self):
        KbTracker.objects.filter(kb=self.kb).update(enabled=False)
        self.client.force_login(self.rse)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.gadmin)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_users_without_read_access_still_get_404(self):
        self.kb.department = RSE
        self.kb.save(update_fields=['department', 'updated_at'])
        self.client.force_login(self.afe)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(
            self.client.get(self.url, {'row': self.row.pk, 'parameter': '场次数'}).status_code, 404)
        self.client.force_login(self.rse)
        self.assertEqual(self.client.get(self.url).status_code, 200)   # 本部门仍可用

    def test_reader_hits_the_same_validation(self):
        self.client.force_login(self.rse)
        response = self.client.get(self.url, {'row': self.row.pk, 'parameter': '不存在的参数'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['ok'])


class ReadonlyUiLanguageTests(SimpleTestCase):
    """只读/公共库的新文案必须进 i18n 目录，否则英文界面会漏中文。

    页面文本由 static/js/interface_language.js 按 messages 原文命中、或按
    patterns 的 `{name}` 模板匹配翻译；动态数字/名称必须单独成节点或走 pattern。
    """

    MESSAGES = (
        '只读', '打开', '公共知识库', '还没有追踪记录。', '每页', '追踪表分页',
        '你还没有可管理的手册库；下方「公共知识库」可以直接查阅。',
        '其它部门或「通用」范围开放给你的库：可查阅、可检索、可问答；上传与修改由所属部门或全局管理员维护。',
        '此库为只读（通用库由全局管理员维护）：可正常查看文档、检索与问答；需上传或修改请联系全局管理员。',
        '此追踪表为只读：可核对记录与导出 Excel，字段配置、补录与删除由库的管理员维护。',
        '通用库对所有部门开放：可浏览、可检索、可问答；上传与修改由全局管理员维护',
        '该知识库为只读（通用库由全局管理员维护），无法上传。',
        '该知识库为只读，无法修改追踪表（可正常查看与导出）。',
    )
    PATTERNS = (
        '{count} 个只读库', '{count} 个采集库', '{count} 条',
        '· {count} 份已完成文档待补录', '第 {page} 页', '共 {count} 条',
        '全部 {count} 条采集记录 · 展开查看明细',
    )

    def setUp(self):
        self.messages, self.patterns = {}, set()
        for path in glob.glob(str(Path(settings.BASE_DIR) / 'static' / 'i18n' / '*.json')):
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
            self.messages.update(data.get('messages', {}))
            self.patterns |= {row['source'].strip() for row in data.get('patterns', [])}

    def test_new_ui_strings_have_english_entries(self):
        for text in self.MESSAGES:
            with self.subTest(text=text):
                target = self.messages.get(text)
                self.assertIsNotNone(target, '缺少英文条目')
                self.assertRegex(target, r'[A-Za-z]')

    def test_new_dynamic_strings_have_patterns(self):
        for source in self.PATTERNS:
            with self.subTest(source=source):
                self.assertIn(source.strip(), self.patterns)

    def test_catalogue_endpoint_ships_the_new_entries(self):
        response = self.client.get('/i18n/catalog.js')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8')
        for fragment in ('公共知识库', 'Shared Libraries', 'read-only libraries', 'Read-only'):
            self.assertIn(fragment, body)
