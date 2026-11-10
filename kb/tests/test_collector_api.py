from __future__ import annotations

"""服务层 `kb.collector_api` 与认证形状归一的回归测试：页面、命令行共用同一实现。"""

import io
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from kb import collector_api
from kb.feed_credentials import parse_auth
from kb.models import ConnectorDraft, KnowledgeBase, KnowledgeFeed, TrackerRow

from .test_connector import spec, sample

SECRET = "meapi_AbCdEf1234567890_-xYz"


class AuthShapeTests(TestCase):
    """缺少 type 曾被静默当成「不认证」，导致凭据从未保存、之后每次请求都 401。"""

    def test_unambiguous_shapes_are_inferred(self):
        self.assertEqual(parse_auth({"token": SECRET})[0], "bearer")
        self.assertEqual(parse_auth({"token": SECRET})[1], {"Authorization": "Bearer " + SECRET})
        self.assertEqual(parse_auth({"username": "u", "password": "p"})[0], "basic")
        self.assertEqual(parse_auth({"headers": [{"name": "X-Key", "value": SECRET}]})[0], "headers")

    def test_explicit_no_auth_stays_no_auth(self):
        self.assertEqual(parse_auth({"type": "none"})[0], "none")
        self.assertEqual(parse_auth({})[0], "none")

    def test_ambiguous_or_missing_type_is_rejected_not_silently_dropped(self):
        for shape in ({"type": None}, {"api_key": SECRET}, {"anything": SECRET}):
            with self.assertRaises(ValueError) as caught:
                parse_auth(shape)
            self.assertIn("type", str(caught.exception))


class CollectorDraftTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("api-admin", is_staff=True)
        self.dept = get_user_model().objects.create_user("api-dept")
        from accounts.models import UserProfile
        UserProfile.objects.update_or_create(user=self.dept, defaults={"department": "机械", "role": UserProfile.Role.DEPT_ADMIN})
        self.kb = KnowledgeBase.objects.create(name="业务数据", slug="api-business", is_folder=True, category="business",
                                               department="机械", created_by=self.dept)
        start = patch("kb.collector_api.start_async"); self.start = start.start(); self.addCleanup(start.stop)
        publish = patch("kb.collector_api.start_feed"); self.publish = publish.start(); self.addCleanup(publish.stop)

    def create(self, user, **extra):
        data = {"user": user, "requirement": "每天13点拉接口，采集每车当前里程并保留当天快照",
                "api_url": "https://api.example.test/reports", **extra}
        return collector_api.create_draft(**data)

    def test_structured_credential_is_stored_and_prompt_is_redacted(self):
        job = self.create(self.admin, requirement=f"api key is {SECRET}，每天13点拉接口采集每车当前里程",
                          authentication={"type": "bearer", "token": SECRET}, created_via="cli")
        self.assertEqual(job.created_via, "cli")
        self.assertIsNotNone(job.credential_id)
        self.assertEqual(job.header_env, {name: f"FOS_FEED_CREDENTIAL_{job.credential_id.hex.upper()}_0"
                                          for name in job.credential.header_names})
        self.assertNotIn(SECRET, job.prompt)
        self.assertIn("[REDACTED]", job.prompt)   # 模型看不到明文，只能用已绑定的凭据取数
        self.assertEqual(self.start.call_args.args, (job.pk,))

    def test_intranet_source_open_by_default(self):
        job = self.create(self.dept)
        self.assertTrue(job.allow_private)                     # 新任务默认开放内网，不再仅限全局管理员
        ConnectorDraft.objects.filter(pk=job.pk).update(status="ready")  # 同账号同时只允许一个生成中任务
        self.assertTrue(self.create(self.admin, allow_private=True).allow_private)
        self.assertFalse(self.create(self.dept, allow_private=False).allow_private)  # 显式关闭仍有效

    def test_department_manager_can_attach_page_credential(self):
        """凭据自带的绑定引用不是「服务器环境变量」，部门管理员可以使用。"""
        from kb.feed_credentials import bindings
        job = self.create(self.admin, authentication={"type": "bearer", "token": SECRET})
        reused = self.create(self.dept, credential=job.credential, header_env=bindings(job.credential))
        self.assertEqual(reused.credential_id, job.credential_id)
        self.assertEqual(reused.header_env, job.header_env)

    def test_plain_admin_can_attach_server_environment_reference(self):
        with self.assertRaises(ValueError):
            self.create(self.dept, credential=None, header_env={"Authorization": "FOS_FEED_REMOTE_AUTH"})

    def test_short_requirement_and_concurrent_task_are_rejected(self):
        with self.assertRaises(ValueError):
            self.create(self.admin, requirement="太短")
        self.create(self.admin)
        with self.assertRaises(ValueError):
            self.create(self.admin)


class CollectorApproveTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("approve-admin", is_staff=True)
        patch("kb.collector_api.start_async").start(); self.addCleanup(patch.stopall)
        self.publish = patch("kb.collector_api.start_feed").start()

    def ready_job(self, *, status="ready", job_spec=None, **extra):
        return ConnectorDraft.objects.create(user=self.admin, prompt="API https://api.example.test/reports 采集日报",
            api_url="https://api.example.test/reports", status=status,
            spec=job_spec if job_spec is not None else spec(), preview={"valid": True, "items": 1}, **extra)

    def test_approve_creates_scheduled_source_once_and_starts_first_run(self):
        job = self.ready_job()
        feed = collector_api.approve(user=self.admin, job=job)
        again = collector_api.approve(user=self.admin, job=ConnectorDraft.objects.get(pk=job.pk))
        self.assertEqual(KnowledgeFeed.objects.count(), 1)
        self.assertEqual(feed.pk, again.pk)
        self.assertEqual(feed.cron, "0 8 * * *")
        self.assertIsNotNone(feed.next_run_at)
        self.assertTrue(feed.enabled)
        self.assertEqual(ConnectorDraft.objects.get(pk=job.pk).status, "enabled")
        self.assertEqual(ConnectorDraft.objects.get(pk=job.pk).status, "enabled")
        self.assertEqual(self.publish.call_count, 1)   # 重复批准直接返回同一数据源，不再首跑

    def test_approve_refuses_unverified_candidate(self):
        job = self.ready_job(status="needs_info")
        with self.assertRaises(ValueError):
            collector_api.approve(user=self.admin, job=job)
        self.assertEqual(KnowledgeFeed.objects.count(), 0)

    def test_browser_script_needs_full_batch_validation_before_approval(self):
        job = self.ready_job(job_spec=spec(source_type="browser", browser={"actions": [],
            "output": {"kind": "dom", "rows": ".x", "fields": {"name": {"selector": ".n"}}}}))
        with self.assertRaises(ValueError) as caught:
            collector_api.approve(user=self.admin, job=job)
        self.assertIn("完整批次", str(caught.exception))

    def test_approve_without_first_run_still_schedules(self):
        feed = collector_api.approve(user=self.admin, job=self.ready_job(), run_now=False)
        self.assertEqual(self.publish.call_count, 0)
        self.assertGreater(feed.next_run_at, timezone.now() - timezone.timedelta(hours=1))

    def test_confirmation_projects_selected_fields_and_preserves_calculation_inputs(self):
        from kb.connector_runtime import build_batch
        job = self.ready_job(job_spec=spec(body_fields=[{'label':'温度','path':'temperature'},
            {'label':'双倍温度','computed':{'op':'multiply','args':[{'field':'temperature'},2]}}]))
        self.client.force_login(self.admin)
        from django.urls import reverse
        response = self.client.post(reverse('kb:connector_enable', args=[job.pk]),
            json.dumps({'selected_fields':['双倍温度']}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        feed = KnowledgeFeed.objects.get(pk=response.json()['feed_id'])
        self.assertEqual([f['label'] for f in feed.adapter_spec['body_fields']], ['双倍温度','采集日期'])
        self.assertEqual(feed.adapter_spec['id_paths'], job.spec['id_paths'])
        fields = build_batch(feed.adapter_spec, sample())['items'][0]['fields']
        self.assertEqual(fields['双倍温度'], 50)
        self.assertNotIn('温度', fields)
        self.assertRegex(fields['采集日期'], r'^\d{4}-\d{2}-\d{2}$')

    def test_empty_selection_keeps_date_and_invalid_selection_rolls_back(self):
        for selection in ('温度', ['不存在'], [None]):
            job = self.ready_job()
            with self.assertRaises(ValueError):
                collector_api.approve(user=self.admin, job=job, selected_fields=selection, run_now=False)
            job.refresh_from_db()
            self.assertEqual(job.status, 'ready')
        self.assertEqual(KnowledgeFeed.objects.count(), 0)
        feed = collector_api.approve(user=self.admin, job=self.ready_job(), selected_fields=[], run_now=False)
        self.assertEqual(feed.adapter_spec['body_fields'], [{'label':'采集日期','source':'today'}])

    def test_no_selection_preserves_all_fields_plus_collection_date(self):
        feed = collector_api.approve(user=self.admin, job=self.ready_job(), run_now=False)
        self.assertEqual([f['label'] for f in feed.adapter_spec['body_fields']], ['温度','报警','采集日期'])

    def test_document_mode_preserves_raw_fields_and_opens_document_page(self):
        from django.urls import reverse

        job = self.ready_job()
        feed = collector_api.approve(user=self.admin, job=job, ingest_mode='documents',
                                     selected_fields=['温度'], run_now=False)
        self.assertEqual(feed.adapter_spec['ingest_mode'], 'documents')
        self.assertEqual([field['label'] for field in feed.adapter_spec['body_fields']],
                         ['温度', '报警', '采集日期'])
        self.client.force_login(self.admin)
        response = self.client.get(reverse('kb:manage_detail', args=[feed.kb.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'kb/manage_detail.html')
        self.assertContains(response, '配置 AI 提炼字段')

    def test_existing_feed_cannot_change_ingest_mode_in_place(self):
        original = self.ready_job()
        feed = collector_api.approve(user=self.admin, job=original, run_now=False)
        revised = self.ready_job(parent=original, kb=feed.kb, job_spec=spec(ingest_mode='documents'))
        with self.assertRaisesMessage(ValueError, '不能直接切换入库方式'):
            collector_api.approve(user=self.admin, job=revised, run_now=False)
        revised.refresh_from_db()
        self.assertEqual(revised.status, 'ready')
        feed.refresh_from_db()
        self.assertEqual(feed.adapter_spec['ingest_mode'], 'tracker')


class CollectorRefineTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("refine-admin", is_staff=True)
        self.start = patch("kb.collector_api.start_async").start(); self.addCleanup(patch.stopall)

    def test_refine_inherits_credential_intranet_flag_and_entry(self):
        parent = ConnectorDraft.objects.create(user=self.admin, prompt="API https://api.example.test/reports 采集日报",
            api_url="https://api.example.test/reports", status="needs_info", allow_private=True,
            header_env={"Authorization": "FOS_FEED_C_REF"}, created_via="cli")
        child = collector_api.refine_draft(user=self.admin, job=parent, requirement="已授权，请重新读取", allow_private=None)
        self.assertEqual(child.parent_id, parent.pk)
        self.assertTrue(child.allow_private)
        self.assertEqual(child.header_env, parent.header_env)
        self.assertEqual(child.api_url, parent.api_url)
        self.assertEqual(child.created_via, "cli")
        self.assertEqual(ConnectorDraft.objects.get(pk=parent.pk).status, "revised")

    def test_refine_can_open_intranet_for_non_admin(self):
        from accounts.models import UserProfile
        dept = get_user_model().objects.create_user("refine-dept")
        UserProfile.objects.update_or_create(user=dept, defaults={"department": "机械", "role": UserProfile.Role.DEPT_ADMIN})
        parent = ConnectorDraft.objects.create(user=dept, prompt="API https://api.example.test/reports", status="needs_info")
        child = collector_api.refine_draft(user=dept, job=parent, requirement="开启内网", allow_private=True)
        self.assertTrue(child.allow_private)


class BuildFeedCommandTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("cli-admin", password=None)
        patch("kb.collector_api.start_async").start(); self.publish = patch("kb.collector_api.start_feed").start()
        self.addCleanup(patch.stopall)

    def run_command(self, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        code = call_command("build_feed", *args, stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue(), code

    def test_cli_creates_job_with_token_from_environment_and_marks_entry(self):
        with patch.dict("os.environ", {"FOS_TEST_TOKEN": SECRET}):
            out, _, _ = self.run_command("每天13点拉 https://api.example.test/reports 采集每车里程",
                                         "--url", "https://api.example.test/reports", "--token-env", "FOS_TEST_TOKEN",
                                         "--wait", "0", "--json")
        state = json.loads(out)
        job = ConnectorDraft.objects.get(pk=state["id"])
        self.assertEqual(job.created_via, "cli")
        self.assertTrue(job.allow_private)
        self.assertTrue(state["has_credential"])
        self.assertNotIn(SECRET, out)
        self.assertNotIn(SECRET, job.prompt)

    def test_cli_requires_token_environment_variable_to_exist(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(CommandError):
                self.run_command("每天拉 https://api.example.test/reports 采集里程数据", "--token-env", "MISSING_TOKEN")

    def test_cli_approves_existing_job(self):
        job = ConnectorDraft.objects.create(user=self.admin, prompt="API https://api.example.test/reports",
            api_url="https://api.example.test/reports", status="ready", spec=spec(), preview={"valid": True, "items": 2},
            created_via="cli")
        with patch('kb.feed_http.read_url', return_value=(200, {'content-type':'application/json'}, json.dumps(sample()).encode())):
            out, _, _ = self.run_command("--job", str(job.pk), "--approve")
        self.assertEqual(KnowledgeFeed.objects.count(), 1)
        self.assertIn("已启用定时数据源", out)
        self.assertEqual(KnowledgeFeed.objects.get().cron, "0 8 * * *")
        self.publish.assert_not_called()
        self.assertEqual(KnowledgeFeed.objects.get().runs.get().status, 'completed')
        self.assertEqual(TrackerRow.objects.count(), 1)
        self.assertIn('采集日期', TrackerRow.objects.get().values)

    def test_cli_first_import_failure_exits_unsuccessfully_and_releases_lease(self):
        job = ConnectorDraft.objects.create(user=self.admin, prompt='API https://api.example.test/reports',
            api_url=spec()['url'], status='ready', spec=spec(), preview={'valid':True})
        with patch('kb.feed_http.read_url', side_effect=ValueError('合成接口不可用')):
            with self.assertRaisesMessage(CommandError, '首次入库未完成'):
                self.run_command('--job', str(job.pk), '--approve')
        feed = KnowledgeFeed.objects.get()
        self.assertEqual(feed.runs.get().status, 'failed')
        self.assertEqual(feed.lease_token, '')
        self.assertTrue(feed.enabled)
        self.publish.assert_not_called()

    def test_cli_can_schedule_without_first_import(self):
        job = ConnectorDraft.objects.create(user=self.admin, prompt='API https://api.example.test/reports',
            api_url=spec()['url'], status='ready', spec=spec(), preview={'valid':True})
        with patch('kb.collector_api.execute_feed') as execute:
            self.run_command('--job', str(job.pk), '--approve', '--no-first-run')
        execute.assert_not_called()
        self.publish.assert_not_called()
        self.assertEqual(KnowledgeFeed.objects.get().runs.count(), 0)

    def test_cli_reports_needs_info_and_exits_with_code(self):
        ConnectorDraft.objects.create(user=self.admin, prompt="API https://api.example.test/reports", status="ready",
            spec=spec(), questions=["请补充实际 API 地址"], created_via="cli")
        out, _, _ = self.run_command("每天拉 https://api.example.test/reports 采集里程数据", "--wait", "0", "--json")
        created = json.loads(out)["id"]
        ConnectorDraft.objects.filter(pk=created).update(status="needs_info")
        with self.assertRaises(SystemExit) as caught:
            self.run_command("--job", created, "--json")
        self.assertEqual(caught.exception.code, 2)
