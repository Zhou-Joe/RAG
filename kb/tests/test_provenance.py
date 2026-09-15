"""chunk 溯源 + 问答增强两步（规划/核实）+ 证据接口。"""
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from kb.models import ChunkProvenance, Document, KnowledgeBase
from kb.pipeline import _md_for_embedding
from kb.provenance import (
    _locate_blocks, _sig, annotate_results, build_provenance_rows, parse_content_list,
    persist_content_list, load_content_list,
)


def _fake_msg(content):
    class M:
        pass
    m = M()
    m.content = content
    m.usage_metadata = {"input_tokens": 3, "output_tokens": 2}
    return m


# 模拟 MinerU 输出：md（含标题/段落/HTML 表格/图片）+ 对应 content_list
_MD = """## 检验报告

游乐设施为创极速光轮，工作单号 7336768。

<table><tr><td rowspan=1 colspan=2>收件数量</td><td rowspan=1 colspan=1>19</td></tr><tr><td rowspan=1 colspan=2>检验数量</td><td rowspan=1 colspan=1>19</td></tr></table>

图纸为 WMKTRONMV0010。

![示意图](images/abc123.jpg)
"""

_CONTENT_LIST = [
    {"type": "text", "text": "检验报告", "text_level": 2, "page_idx": 0,
     "bbox": [100, 50, 300, 90]},
    {"type": "text", "text": "游乐设施为创极速光轮，工作单号 7336768。", "page_idx": 0,
     "bbox": [50, 120, 700, 160]},
    {"type": "table", "table_body": _MD.split("<table>")[1].split("</table>")[0].join(["<table>", "</table>"]),
     "table_caption": [], "page_idx": 1, "bbox": [40, 200, 900, 600]},
    {"type": "text", "text": "图纸为 WMKTRONMV0010。", "page_idx": 2,
     "bbox": [60, 100, 500, 140]},
    {"type": "image", "img_path": "images/abc123.jpg", "page_idx": 3,
     "bbox": [200, 300, 800, 700]},
    # 页眉/页脚不参与定位
    {"type": "header", "text": "SHDR", "page_idx": 0, "bbox": [0, 0, 50, 20]},
]


class SigAndLocateTests(TestCase):
    def test_sig_strips_punctuation_and_case(self):
        self.assertEqual(_sig("Hello, World! 你好。"), "helloworld你好")

    def test_locate_blocks_pages_and_image_map(self):
        clean = _md_for_embedding(_MD)
        spans, img_map = _locate_blocks(_CONTENT_LIST, clean)
        # 4 个可定位块（标题/两段/表格），页眉排除，图片单独走 map
        self.assertEqual(len(spans), 4)
        pages = [sp["page"] for sp in spans]
        self.assertEqual(pages, [0, 0, 1, 2])
        self.assertEqual(img_map["abc123.jpg"]["page"], 3)
        # 每个块的区间都在 clean_md 内且单调
        prev_end = -1
        for sp in spans:
            self.assertGreaterEqual(sp["start"], prev_end)
            self.assertLess(sp["start"], sp["end"])
            self.assertLessEqual(sp["end"], len(clean))
            prev_end = sp["end"]

    def test_parse_content_list_str_and_invalid(self):
        import json
        self.assertEqual(parse_content_list(json.dumps([1, 2])), [1, 2])
        self.assertEqual(parse_content_list([{"a": 1}]), [{"a": 1}])
        self.assertIsNone(parse_content_list("not json"))
        self.assertIsNone(parse_content_list(None))
        self.assertIsNone(parse_content_list('{"obj": 1}'))


class BuildProvenanceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="prov-admin", password="pw", is_staff=True)
        self.kb = KnowledgeBase.objects.create(
            name="溯源库", slug="prov-lib", created_by=self.user)
        self.doc = Document.objects.create(
            kb=self.kb, original_name="report.pdf", file_type="pdf")

    def _chunks(self):
        """按切块器的真实形态构造 chunk（带【章节】前缀）。"""
        from langchain_core.documents import Document as LCD
        clean = _md_for_embedding(_MD)
        texts = [
            "【检验报告】\n## 检验报告\n\n游乐设施为创极速光轮，工作单号 7336768。",
            "【检验报告】\n收件数量 | 收件数量 | 19\n检验数量 | 检验数量 | 19",
            "【检验报告】\n图纸为 WMKTRONMV0010。",
        ]
        return clean, [LCD(page_content=t, metadata={"source": "report.pdf"})
                       for t in texts]

    def test_build_rows_pages_and_table_rows(self):
        clean, chunks = self._chunks()
        ids = ["c1", "c2", "c3"]
        rows = build_provenance_rows(self.doc, "prov-lib", ids, chunks, clean,
                                     _CONTENT_LIST, image_names=["abc123.jpg"])
        self.assertEqual(len(rows), 4)  # 3 文本 + 1 图片
        by_id = {r.chunk_id: r for r in rows}
        self.assertEqual(by_id["c1"].page_start, 0)  # 标题+首段 → 第1页
        self.assertEqual(by_id["c2"].page_start, 1)  # 表格 → 第2页
        tbl = [b for b in by_id["c2"].blocks if b["kind"] == "table"]
        self.assertEqual(tbl[0]["rows"], [1, 2])     # 表格两行都覆盖
        self.assertEqual(by_id["c3"].page_start, 2)  # 尾段 → 第3页
        img_row = by_id["img-" + str(self.doc.id) + "-abc123.jpg"]
        self.assertEqual(img_row.page_start, 3)
        self.assertEqual(img_row.blocks[0]["kind"], "image")

    def test_persist_and_load_content_list(self):
        with tempfile.TemporaryDirectory() as td:
            with override_settings(DATA_DIR=Path(td)):
                persist_content_list(self.doc.id, _CONTENT_LIST)
                self.assertEqual(load_content_list(self.doc.id), _CONTENT_LIST)
                self.assertIsNone(load_content_list("00000000-0000-0000-0000-000000000000"))


class AnnotateAndEvidenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="ev-user", password="pw")
        self.other = get_user_model().objects.create_user(
            username="ev-other", password="pw")
        self.kb = KnowledgeBase.objects.create(name="证据库", slug="ev-lib",
                                               department="机械")
        self.doc = Document.objects.create(
            kb=self.kb, original_name="ev.pdf", file_type="pdf")
        ChunkProvenance.objects.create(
            chunk_id="ev-chunk-1", document=self.doc, kb_slug="ev-lib",
            page_start=4, page_end=4,
            blocks=[{"page": 4, "bbox": [10, 20, 30, 40], "kind": "text"}])

    def test_annotate_results_adds_page(self):
        results = [{"chunk_id": "ev-chunk-1", "text": "x"}, {"chunk_id": "nope", "text": "y"}]
        annotate_results(results)
        self.assertEqual(results[0]["page_label"], "第 5 页")
        self.assertNotIn("page_label", results[1])

    def test_preview_requires_login_and_dept(self):
        from django.test import Client
        c = Client()
        # 未登录 → 跳登录
        r = c.get("/kb/evidence/ev-chunk-1/preview/")
        self.assertEqual(r.status_code, 302)
        # 其它部门用户 → 404（不泄露存在性）
        c.force_login(self.other)
        self.assertEqual(c.get("/kb/evidence/ev-chunk-1/preview/").status_code, 404)
        # 通用部门普通用户 → 同样不可见（库是「机械」部门）
        c.force_login(self.user)
        r = c.get("/kb/evidence/ev-chunk-1/preview/")
        self.assertEqual(r.status_code, 404)  # 用户不在机械部门 → 也不可见
        # 管理员可见
        admin = get_user_model().objects.create_user(
            username="ev-admin", password="pw", is_staff=True, is_superuser=True)
        c.force_login(admin)
        r = c.get("/kb/evidence/ev-chunk-1/preview/")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["pages"], [4, 4])
        self.assertEqual(data["page_label"], "第 5 页")
        self.assertEqual(data["blocks"][0]["bbox"], [10, 20, 30, 40])
        self.assertFalse(data["has_pdf"])

    def test_preview_no_provenance_returns_ok_false(self):
        """无溯源行（照片/老入库）→ 200 + ok:false，前端降级提示而非 404。"""
        from django.test import Client
        admin = get_user_model().objects.create_user(
            username="ev-admin4", password="pw", is_staff=True, is_superuser=True)
        c = Client()
        c.force_login(admin)
        r = c.get("/kb/evidence/never-seen-chunk/preview/")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no_provenance")

    def test_page_png_rejects_non_pdf(self):
        from django.test import Client
        admin = get_user_model().objects.create_user(
            username="ev-admin2", password="pw", is_staff=True, is_superuser=True)
        c = Client()
        c.force_login(admin)
        self.assertEqual(c.get("/kb/evidence/ev-chunk-1/page.png").status_code, 404)

    def test_page_png_renders_pdf(self):
        import pymupdf
        buf = pymupdf.open()
        for _ in range(6):
            buf.new_page()
        pdf_bytes = buf.tobytes()
        with tempfile.TemporaryDirectory() as td:
            with override_settings(MEDIA_ROOT=Path(td)):
                self.doc.file.save("ev.pdf", ContentFile(pdf_bytes))
                from django.test import Client
                admin = get_user_model().objects.create_user(
                    username="ev-admin3", password="pw", is_staff=True, is_superuser=True)
                c = Client()
                c.force_login(admin)
                r = c.get("/kb/evidence/ev-chunk-1/page.png?page=4")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r["Content-Type"], "image/png")
                self.assertGreater(len(r.content), 1000)
                # 越界或不属于片段的页码必须拒绝，不能伪造定位
                r2 = c.get("/kb/evidence/ev-chunk-1/page.png?page=99")
                self.assertEqual(r2.status_code, 404)
                self.assertEqual(r["Cache-Control"], "private, no-store")
                for page in ("0", "5", "-1", "abc"):
                    response = c.get("/kb/evidence/ev-chunk-1/page.png", {"page": page})
                    self.assertIn(response.status_code, (400, 404))


class PageEmbedTests(TestCase):
    """视觉文档模式：整页块（渲染 + 页文本锚定）。"""

    def test_page_texts_aggregates_and_skips_headers(self):
        from kb.pipeline import page_texts
        cl = [
            {"type": "text", "text": "标题文字", "page_idx": 0},
            {"type": "header", "text": "页眉噪声", "page_idx": 0},
            {"type": "table", "table_body":
             "<table><tr><td rowspan=1 colspan=1>a</td><td rowspan=1 colspan=1>b</td></tr></table>",
             "page_idx": 1},
            {"type": "image", "img_path": "images/x.jpg", "page_idx": 1,
             "image_caption": ["气动控制面板"]},
        ]
        texts = page_texts(cl)
        self.assertEqual(texts[0], "标题文字")
        self.assertIn("a | b", texts[1])
        self.assertIn("气动控制面板", texts[1])
        self.assertNotIn("页眉噪声", texts[0])
        self.assertEqual(page_texts(None), {})

    def test_page_chunks_metadata(self):
        from kb.pipeline import _page_chunks
        items = _page_chunks("r.pdf", [
            {"type": "text", "text": "首页内容", "page_idx": 0},
            {"type": "text", "text": "第二页", "page_idx": 1},
        ])
        self.assertEqual(len(items), 2)
        meta = items[0][0].metadata
        self.assertEqual(meta["type"], "image")
        self.assertEqual(meta["image"], "page0")
        self.assertEqual(meta["page"], 0)
        self.assertIn("【第1页】", items[0][0].page_content)
        self.assertEqual(items[1][0].metadata["page"], 1)

    def test_render_page_jpeg(self):
        import pymupdf
        from kb.pipeline import _render_page_jpeg
        import tempfile
        buf = pymupdf.open()
        buf.new_page()  # 默认 595x842
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(buf.tobytes())
            path = f.name
        raw = _render_page_jpeg(Path(path), 0)
        self.assertTrue(raw and raw[:2] == b"\xff\xd8")  # JPEG magic
        self.assertIsNone(_render_page_jpeg(Path(path), 99))  # 越页 → None


class QaStepsTests(TestCase):
    def test_parse_verdict_three_states_and_legacy(self):
        from kb.qa_steps import _parse_verdict
        self.assertEqual(_parse_verdict('{"verdict": "pass", "issues": []}'),
                         {"verdict": "pass", "issues": []})
        self.assertEqual(_parse_verdict('前言 {"verdict": "fail", "issues": ["扭矩无证据"]} 后语'),
                         {"verdict": "fail", "issues": ["扭矩无证据"]})
        self.assertEqual(_parse_verdict('{"verified": true}'),
                         {"verdict": "pass", "issues": []})
        self.assertIsNone(_parse_verdict("完全不是 JSON"))
        self.assertIsNone(_parse_verdict(""))

    def _fake_llm(self, content):
        class L:
            async def ainvoke(self, msgs, **kw):
                return _fake_msg(content)
        return L()

    def test_verify_answer_fail_and_pass(self):
        import asyncio
        from kb import qa_steps
        ev = [{"source": "a.pdf", "page": "第 1 页", "text": "收件数量 19"}]
        with patch.object(qa_steps, "build_llm", return_value=self._fake_llm(
                '{"verdict": "fail", "issues": ["收件数量25无证据"]}')):
            v, u = asyncio.run(qa_steps.verify_answer(
                {}, "收件数量?", "收件数量 25", ev))
            self.assertEqual(v["verdict"], "fail")
            self.assertEqual(u, {"input_tokens": 3, "output_tokens": 2})
        with patch.object(qa_steps, "build_llm", return_value=self._fake_llm(
                '{"verdict": "pass", "issues": []}')):
            v, _ = asyncio.run(qa_steps.verify_answer(
                {}, "收件数量?", "收件数量 19", ev))
            self.assertEqual(v["verdict"], "pass")

    def test_verify_skips_without_evidence(self):
        import asyncio
        from kb import qa_steps
        v, u = asyncio.run(qa_steps.verify_answer({}, "q", "a", []))
        self.assertIsNone(v)

    def test_plan_query_skip_on_greeting(self):
        import asyncio
        from kb import qa_steps
        with patch.object(qa_steps, "build_llm", return_value=self._fake_llm("SKIP")):
            plan, _ = asyncio.run(qa_steps.plan_query({}, "你好"))
            self.assertIsNone(plan)
        with patch.object(qa_steps, "build_llm", return_value=self._fake_llm(
                '{"standalone":"扭矩是多少","action":"new","output":"text","subquestions":[]}')):
            plan, _ = asyncio.run(qa_steps.plan_query({}, "扭矩是多少"))
            self.assertIn("standalone", plan)
        # LLM 异常 → None（不拖累主流程）
        class Boom:
            async def ainvoke(self, *a, **k):
                raise RuntimeError("down")
        with patch.object(qa_steps, "build_llm", return_value=Boom()):
            plan, _ = asyncio.run(qa_steps.plan_query({}, "扭矩是多少"))
            self.assertIsNone(plan)


class LocationReliabilityTests(TestCase):
    def test_signature_cursor_uses_original_character_offsets(self):
        from kb.provenance import _SigIndex, _chunk_span
        prefix = "prefix" + " . " * 200
        text = prefix + "Motor AB-123 interval daily"
        span, cursor = _chunk_span(_SigIndex(text), text, "Motor AB123 interval daily", len(prefix))
        self.assertEqual(span, (len(prefix), len(text)))
        self.assertEqual(cursor, len(text))

    def test_unmatched_chunk_never_inherits_page(self):
        from langchain_core.documents import Document as LCD
        rows = build_provenance_rows(None, "synthetic", ["missing"],
            [LCD(page_content="Unrelated pump inspection")], "Known valve interval",
            [{"type": "text", "text": "Known valve interval", "page_idx": 0}])
        self.assertEqual(rows, [])

    def test_unicode_expansion_preserves_original_offsets(self):
        from kb.provenance import _SigIndex
        index = _SigIndex("İ..motor")
        self.assertEqual(index.find("motor", 0)[0], (3, 8))

    def test_invalid_boxes_cannot_be_rendered_as_precise_locations(self):
        from kb.provenance import valid_bbox
        for box in ([0, 0, float("nan"), 20], [-1, 0, 20, 20],
                    [10, 10, 5, 20], [0, 0, 1001, 20], [True, 0, 20, 20]):
            self.assertIsNone(valid_bbox(box))
        self.assertEqual(valid_bbox([10, 20, 30, 40]), [10, 20, 30, 40])
