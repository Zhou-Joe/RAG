from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from kb.models import Document, KnowledgeBase, StructuredDataset
from kb.structured_data import (
    StructuredDataError, import_structured_dataset, lookup_drawing, lookup_related,
)


def csv_upload(name: str, text: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, text.encode("utf-8-sig"), content_type="text/csv")


class StructuredDataTests(TestCase):
    def test_csv_import_and_three_hop_drawing_lookup(self):
        import_structured_dataset(
            csv_upload(
                "drawing.csv",
                "图纸号,部件号,G-code,SCP等级\nDWG-1001,P-77,G-42,SCP-2\n",
            ),
            StructuredDataset.Kind.DRAWING,
        )
        import_structured_dataset(
            csv_upload(
                "parts.csv",
                "Part No,Part Name,Equipment\nP-77,主轴承,ROLLER-01\n",
            ),
            StructuredDataset.Kind.PARTS,
        )
        import_structured_dataset(
            csv_upload(
                "downtime.csv",
                "设备号,停机小时,故障原因\nROLLER-01,3.5,轴承温度高\n",
            ),
            StructuredDataset.Kind.DOWNTIME,
        )
        import_structured_dataset(
            csv_upload(
                "apex.csv",
                "Asset,APEX ID,状态\nROLLER-01,APEX-88,Open\n",
            ),
            StructuredDataset.Kind.APEX,
        )

        result = lookup_drawing("dwg 1001")
        self.assertEqual(len(result["records"]), 4)
        self.assertEqual(
            {group["kind"] for group in result["groups"]},
            {"drawing", "parts", "downtime", "apex"},
        )
        drawing = next(r for r in result["records"] if r.dataset.kind == "drawing")
        self.assertEqual(drawing.g_code, "G-42")
        self.assertEqual(drawing.scp_level, "SCP-2")

        by_part_name = lookup_related("主轴承")
        self.assertEqual(len(by_part_name["records"]), 4)
        self.assertEqual(by_part_name["direct_count"], 1)

    def test_xlsx_import_detects_english_headers(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "BOM"
        sheet.append(["Drawing master data export"])
        sheet.append(["Generated for maintenance lookup"])
        sheet.append(["Drawing Number", "Component No", "Component Name", "SCP Level"])
        sheet.append(["A-200", "C-9", "Brake Assembly", "3"])
        payload = BytesIO()
        workbook.save(payload)

        dataset = import_structured_dataset(
            SimpleUploadedFile(
                "drawing.xlsx", payload.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            StructuredDataset.Kind.DRAWING,
        )
        record = dataset.records.get()
        self.assertEqual(record.drawing_no, "A-200")
        self.assertEqual(record.part_no, "C-9")
        self.assertEqual(record.part_name, "Brake Assembly")
        self.assertEqual(record.scp_level, "3")

    def test_duplicate_file_is_rejected(self):
        content = "图纸号,G-code\nD-1,G-1\n"
        import_structured_dataset(csv_upload("same.csv", content), "drawing")
        with self.assertRaises(StructuredDataError):
            import_structured_dataset(csv_upload("same.csv", content), "drawing")

    def test_staff_can_import_and_render_lookup_page(self):
        user = get_user_model().objects.create_user(
            username="operator", password="test-password", is_staff=True,
        )
        self.client.force_login(user)
        upload_page = self.client.get(reverse("kb:manage_list"))
        self.assertContains(upload_page, "资料上传中心")
        self.assertContains(upload_page, "业务数据")

        # 业务关联导入是显式操作；知识库文件上传保留原始文档。
        response = self.client.post(
            reverse("kb:manage_list"),
            {"action": "upload_structured", "file": csv_upload(
                "drawing-ui.csv",
                "图纸号,部件名称,G-code,SCP等级\nUI-900,制动器,G-9,SCP-1\n",
            )},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        # 消息经 json_script 渲染，连字符会被转义成 \u002D → 断言不含连字符的前缀
        self.assertContains(response, "已导入")

        response = self.client.get(reverse("kb:search"), {"q": "ui 900"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "制动器")
        self.assertContains(response, "G-9")
        self.assertContains(response, "SCP-1")

        legacy = self.client.get(reverse("kb:asset_lookup"), {"q": "ui 900"})
        self.assertRedirects(
            legacy, reverse("kb:search") + "?q=ui+900",
            fetch_redirect_response=False,
        )

    @patch("kb.pipeline.process_document_async")
    def test_staff_can_upload_manual_from_same_upload_center(self, process_async):
        user = get_user_model().objects.create_user(
            username="manual-operator", password="test-password", is_staff=True,
        )
        library = KnowledgeBase.objects.create(
            name="设备手册", slug="manuals", is_folder=True, created_by=user,
        )
        self.client.force_login(user)
        response = self.client.post(reverse("kb:manage_list"), {
            "action": "upload_manual",
            "kb_slug": library.slug,
            "file": SimpleUploadedFile("brake-manual.txt", b"drawing UI-900", content_type="text/plain"),
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "已上传手册")
        document = Document.objects.get(original_name="brake-manual.txt")
        self.assertEqual(document.kb.parent, library)
        process_async.assert_called_once_with(document.id)


class SemanticSearchSectionTests(TestCase):
    """综合搜索的语义命中区：与问答同链路、部门过滤、失败降级。"""

    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="semantic-admin", password=None, is_staff=True,
        )
        cls.gen_kb = KnowledgeBase.objects.create(  # 通用 + 有向量 → 入检索范围
            name="通用手册", slug="sem-gen", is_folder=False, chunk_count=10,
            created_by=cls.staff,
        )
        cls.mech_kb = KnowledgeBase.objects.create(  # 机械部门 → 通用用户不可见
            name="机械手册", slug="sem-mech", is_folder=False, chunk_count=10,
            department="机械", created_by=cls.staff,
        )
        KnowledgeBase.objects.create(  # 无向量 → 跳过
            name="空库", slug="sem-empty", is_folder=False, chunk_count=0,
            created_by=cls.staff,
        )

    def setUp(self):
        self.client.force_login(self.staff)

    def _hits(self):
        return [
            {"doc_id": "d1", "source": "DR报告.pdf", "section": "结构",
             "text": "【结构】主要结构型式 1.立柱", "via": "vec+rr",
             "distance": 0.4, "type": "image", "image": "a" * 64 + ".jpg"},
            {"doc_id": "d2", "source": "手册.pdf", "section": "",
             "text": "轨道布置说明", "via": "vec"},
        ]

    def test_section_renders_image_and_text_hits_with_dept_filter(self):
        with patch("kb.retriever.search_folder", return_value=self._hits()) as sf:
            r = self.client.get(reverse("kb:search"), {"q": "结构图纸"})
        self.assertEqual(r.status_code, 200)
        # 三栏结构：语义命中（文本）与图片命中（多模态召回道）分区展示
        self.assertContains(r, "语义命中")
        self.assertContains(r, "图片命中")
        self.assertContains(r, "/kb/doc/d1/img/" + "a" * 64 + ".jpg")
        # 图片命中以缩略图卡片展示（点击新标签看原图），不是只有占位链接
        self.assertContains(r, '<img src="/kb/doc/d1/img/' + "a" * 64 + '.jpg"')
        self.assertContains(r, 'loading="lazy"')
        self.assertContains(r, "查看切片上下文")
        # 检索范围 = 有向量的可见库（通用用户的 通用 ∪ 本部门 → 只有 sem-gen）
        self.assertEqual(sf.call_args.args[0], ["sem-gen"])

    def test_retrieval_failure_degrades_to_empty_section(self):
        with patch("kb.retriever.search_folder",
                   side_effect=RuntimeError("WeMM 不可达")):
            r = self.client.get(reverse("kb:search"), {"q": "结构图纸"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "语义 0")
        self.assertContains(r, "图片 0")

    def test_no_indexed_kb_skips_retrieval_entirely(self):
        KnowledgeBase.objects.filter(slug__in=["sem-gen", "sem-mech"]).update(chunk_count=0)
        with patch("kb.retriever.search_folder") as sf:
            r = self.client.get(reverse("kb:search"), {"q": "任意"})
        self.assertEqual(r.status_code, 200)
        sf.assert_not_called()

    def test_manual_upload_does_not_trigger_structured_import(self):
        """upload_manual 分支不应顺带跑结构化导入（5270d18 曾丢失 elif 导致
        手册上传后额外弹「仅支持 CSV」矛盾错误）。"""
        from unittest.mock import patch
        user = get_user_model().objects.create_user(
            username="manual-only", password="pw", is_staff=True,
        )
        library = KnowledgeBase.objects.create(
            name="手册库", slug="manual-only-lib", is_folder=True, created_by=user,
        )
        self.client.force_login(user)
        with patch("kb.pipeline.process_document_async"):
            r = self.client.post(reverse("kb:manage_list"), {
                "action": "upload_manual",
                "kb_slug": library.slug,
                "file": SimpleUploadedFile(
                    "guide.txt", b"brake ui-900", content_type="text/plain"),
            }, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "仅支持 CSV")
        self.assertNotContains(r, "已导入")
