"""write_analysis → check_analysis 沙箱自修复回路的测试。"""
import threading

from django.test import TestCase

from kb import analysis_runs


class AnalysisRunsRegistryTests(TestCase):
    def test_register_report_wait(self):
        analysis_runs.register("andeadbeef")
        # 后台线程延迟回报，主线程等待
        t = threading.Timer(0.05, analysis_runs.report,
                            args=("andeadbeef", False, "KeyError: 'x'", "partial out"))
        t.start()
        res = analysis_runs.wait("andeadbeef", 2)
        t.join()
        self.assertIsNotNone(res)
        self.assertFalse(res["ok"])
        self.assertIn("KeyError", res["error"])
        self.assertEqual(res["stdout"], "partial out")

    def test_wait_timeout_returns_none(self):
        analysis_runs.register("antimeout01")
        self.assertIsNone(analysis_runs.wait("antimeout01", 0.05))

    def test_unknown_aid(self):
        self.assertIsNone(analysis_runs.wait("andoesnotexist", 0.01))

    def test_double_report_rejected(self):
        analysis_runs.register("andouble001")
        self.assertTrue(analysis_runs.report("andouble001", True, "", "ok"))
        self.assertFalse(analysis_runs.report("andouble001", True, "", "again"))

    def test_long_payload_truncated(self):
        analysis_runs.register("anlong00001")
        analysis_runs.report("anlong00001", False, "x" * 20000, "")
        res = analysis_runs.wait("anlong00001", 0.01)
        self.assertEqual(len(res["error"]), 8000)


class AnalysisReportViewTests(TestCase):
    def test_report_roundtrip(self):
        analysis_runs.register("anview00001")
        r = self.client.post("/kb/analysis/report/",
                             data='{"aid": "anview00001", "ok": true, "stdout": "done"}',
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        res = analysis_runs.wait("anview00001", 0.01)
        self.assertTrue(res["ok"])
        self.assertEqual(res["stdout"], "done")

    def test_late_or_unknown_aid_is_idempotent_false(self):
        r = self.client.post("/kb/analysis/report/",
                             data='{"aid": "anghost000", "ok": true}',
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])

    def test_missing_aid_400(self):
        r = self.client.post("/kb/analysis/report/",
                             data='{"ok": true}', content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_invalid_json_400(self):
        r = self.client.post("/kb/analysis/report/",
                             data='not-json', content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_get_not_allowed(self):
        r = self.client.get("/kb/analysis/report/")
        self.assertEqual(r.status_code, 405)
