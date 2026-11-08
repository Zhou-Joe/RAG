from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from kb.models import Document, KnowledgeBase, StructuredDataset, StructuredRecord
from kb.structured_data import normalize_key


class ApplicationSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="smoke-admin", password=None, is_staff=True, is_superuser=True,
        )
        cls.folder = KnowledgeBase.objects.create(
            name="测试手册", slug="smoke-manuals", is_folder=True, created_by=cls.user,
        )
        cls.library = KnowledgeBase.objects.create(
            name="制动器手册", slug="brake-manual", parent=cls.folder,
            is_folder=False, created_by=cls.user, doc_count=1, chunk_count=1,
        )
        cls.document = Document.objects.create(
            kb=cls.library,
            original_name="brake.md",
            file="documents/brake.md",
            file_type="md",
            md_content="# 制动器\n图纸号 DWG-700 的每周检查要求：检查磨损。",
            html_content="<h1>制动器</h1><p>图纸号 DWG-700 的每周检查要求：检查磨损。</p>",
            status=Document.Status.COMPLETED,
            chunk_count=1,
        )
        dataset = StructuredDataset.objects.create(
            name="drawing", kind="drawing", source_name="drawing.csv",
            checksum="smoke", row_count=1, imported_by=cls.user,
        )
        StructuredRecord.objects.create(
            dataset=dataset, row_number=2,
            drawing_no="DWG-700", drawing_no_norm=normalize_key("DWG-700"),
            part_no="P-700", part_no_norm=normalize_key("P-700"),
            part_name="制动器", part_name_norm=normalize_key("制动器"),
            g_code="G-700", g_code_norm=normalize_key("G-700"),
            scp_level="SCP-2",
            raw_data={"图纸号": "DWG-700", "部件名称": "制动器"},
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_primary_pages_render(self):
        urls = [
            reverse("kb:manage_list"),
            reverse("kb:search"),
            reverse("kb:inspection"),
            reverse("kb:ask"),
            reverse("kb:settings"),
            reverse("dashboard:index"),
            "/admin/",
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_unified_search_returns_structured_and_manual_results(self):
        response = self.client.get(reverse("kb:search"), {"q": "DWG-700"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "综合关联搜索")
        self.assertContains(response, "G-700")
        self.assertContains(response, "SCP-2")
        self.assertContains(response, "brake.md")
        self.assertContains(response, "手册原文 1")

    def test_document_inspection_and_status_endpoints(self):
        document_page = self.client.get(
            reverse("kb:document_html", kwargs={"doc_id": self.document.id}),
        )
        self.assertEqual(document_page.status_code, 200)
        self.assertContains(document_page, "DWG-700")

        inspection = self.client.get(
            reverse("kb:inspection"), {"doc": str(self.document.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(inspection.status_code, 200)
        self.assertEqual(inspection.json()["doc_name"], "brake.md")

        status = self.client.get(
            reverse("kb:doc_status", kwargs={"slug": self.folder.slug}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(status.status_code, 200)


class KBQuestionRoutingTests(TestCase):
    """按名路由：问题明确提到某库/文档名 → 定位该库；歧义/无命中 → 不干预。"""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        cls.staff = get_user_model().objects.create_user(
            username="router-admin", password=None, is_staff=True,
        )
        from kb.models import Document, KnowledgeBase
        def lib(name, slug, docname):
            kb = KnowledgeBase.objects.create(
                name=name, slug=slug, is_folder=True, created_by=cls.staff)
            child = KnowledgeBase.objects.create(
                name=name, slug=slug + "-lib", is_folder=False,
                parent=kb, chunk_count=10, created_by=cls.staff)
            Document.objects.create(kb=child, original_name=docname,
                                    file_type="pdf", status="completed")
            return child
        cls.p8 = lib("CSEI Design Review", "p8",
                     "P8 CSEI DR report and Certificate.pdf")
        cls.mm = lib("Maintenance Manual", "mm", "SDLSPDRTP0003_EN.pdf")
        cls.ph = lib("药典2025", "ph", "药典.md")

    def _route(self, q):
        from kb.views import _route_kb_by_question
        return _route_kb_by_question(q, self.staff)

    def test_explicit_doc_mention_routes(self):
        self.assertEqual(self._route("P8 报告的附件5写了什么").slug, "p8-lib")
        self.assertEqual(self._route("Dumbo design review 里怎么说").slug, "p8-lib")
        self.assertEqual(self._route("SDLSPDRTP0003 的制动器章节").slug, "mm-lib")
        self.assertEqual(self._route("药典里麝香的用量").slug, "ph-lib")

    def test_no_mention_returns_none(self):
        self.assertIsNone(self._route("制动器怎么保养"))
        self.assertIsNone(self._route(""))

    def test_ambiguous_tie_returns_none(self):
        # P8 和 药典 各命中一个短 token（同分 2）→ 歧义不干预
        self.assertIsNone(self._route("P8 报告和药典有什么区别"))
        # 更长的名称命中记分更高 → 明确定位（非歧义）
        self.assertEqual(self._route("P8 和 SDLSPDRTP0003 有什么区别").slug, "mm-lib")

    def test_partial_code_prefix_and_noise_tokens(self):
        # 长编号前缀提及（sdlspdrtp → sdlspdrtp0003）能定位；
        # dr/en 这类 2 字符纯字母 token 不再造成子串误命中
        self.assertEqual(self._route("SDLSPDRTP 的轨道章节").slug, "mm-lib")
        self.assertIsNone(self._route("轨道的英文缩写是啥"))


class KBDescriptionTests(TestCase):
    """库描述链路：agent 工具输出给 LLM + 管理端可编辑。"""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        cls.staff = get_user_model().objects.create_user(
            username="desc-admin", password="pw", is_staff=True,
        )
        from kb.models import KnowledgeBase
        cls.folder = KnowledgeBase.objects.create(
            name="手册文件夹", slug="desc-folder", is_folder=True,
            description="过山车设备的英文维护手册，含轨道与制动章节",
            created_by=cls.staff,
        )
        cls.lib = KnowledgeBase.objects.create(
            name="维护手册", slug="desc-lib", is_folder=False,
            parent=cls.folder, chunk_count=5,
            description="SDLSPDRTP 过山车维护手册", created_by=cls.staff,
        )

    def test_list_knowledge_bases_shows_description(self):
        from kb.agent import _kb_tree_text
        out = _kb_tree_text({"department__in": ["通用"]})
        self.assertIn("说明: 过山车设备的英文维护手册，含轨道与制动章节", out)
        self.assertIn("说明: SDLSPDRTP 过山车维护手册", out)
        self.assertIn("desc-lib", out)

    def test_rename_updates_description_via_ajax(self):
        self.client.force_login(self.staff)
        r = self.client.post(
            reverse("kb:kb_rename", args=["desc-lib"]),
            {"name": "维护手册2", "description": "新版描述：含年检要求"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["description"], "新版描述：含年检要求")
        kb = KnowledgeBase.objects.get(slug="desc-lib")
        self.assertEqual(kb.description, "新版描述：含年检要求")
        self.assertEqual(kb.name, "维护手册2")

    def test_rename_without_description_field_keeps_it(self):
        """不带 description 字段的旧调用（仅改名）不清空描述。"""
        self.client.force_login(self.staff)
        self.client.post(reverse("kb:kb_rename", args=["desc-lib"]),
                         {"name": "维护手册3"},
                         headers={"x-requested-with": "XMLHttpRequest"})
        kb = KnowledgeBase.objects.get(slug="desc-lib")
        self.assertEqual(kb.description, "SDLSPDRTP 过山车维护手册")

    def test_doc_level_descriptions_in_tree_and_endpoint(self):
        from kb.agent import _kb_tree_text
        from kb.models import Document, KnowledgeBase
        # 文档级描述进树
        Document.objects.create(
            kb=self.lib, original_name="轨道维护.pdf", file_type="pdf",
            status="completed", description="轨道与制动章节的英文维护手册",
        )
        out = _kb_tree_text({"department__in": ["通用"]})
        self.assertIn("· 轨道维护.pdf｜轨道与制动章节的英文维护手册", out)

        # 端点：改文档描述（AJAX）
        doc = Document.objects.get(original_name="轨道维护.pdf")
        self.client.force_login(self.staff)
        r = self.client.post(
            reverse("kb:doc_desc_update", args=["desc-folder", doc.id]),
            {"description": "新版：含月检要求"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        doc.refresh_from_db()
        self.assertEqual(doc.description, "新版：含月检要求")

        # 非管理员不可改
        other = get_user_model().objects.create_user(username="desc-other", password="pw")
        self.client.force_login(other)
        r2 = self.client.post(reverse("kb:doc_desc_update", args=["desc-folder", doc.id]),
                              {"description": "x"})
        self.assertEqual(r2.status_code, 404)


class DocViewModeAndBadgesTests(TestCase):
    """文档列表：视图切换控件 + 文件类型徽章。"""

    def test_manage_detail_has_toggle_and_type_badges(self):
        from kb.models import Document, KnowledgeBase
        user = get_user_model().objects.create_user(
            username="view-admin", password="pw", is_staff=True,
        )
        folder = KnowledgeBase.objects.create(
            name="视图库", slug="view-folder", is_folder=True, created_by=user)
        child = KnowledgeBase.objects.create(
            name="视图子库", slug="view-lib", is_folder=False,
            parent=folder, created_by=user)
        Document.objects.create(
            kb=child, original_name="手册.pdf", file_type="pdf",
            status="completed", chunk_count=3)
        Document.objects.create(
            kb=child, original_name="台账.xlsx", file_type="xlsx",
            status="completed", chunk_count=2)
        self.client.force_login(user)
        r = self.client.get(reverse("kb:manage_detail", args=["view-folder"]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'id="dvtGrid"')
        self.assertContains(r, 'ftype-pdf')
        self.assertContains(r, 'ftype-xls')
        self.assertContains(r, '>PDF</span>')
        self.assertContains(r, '>XLS</span>')

    def test_badge_kind_properties(self):
        from kb.models import Document
        for name, kind, label in [
            ("a.pdf", "pdf", "PDF"), ("b.DOCX", "doc", "DOC"),
            ("c.xlsx", "xls", "XLS"), ("d.md", "txt", "TXT"),
            ("e.xyz", "file", "FILE"),
        ]:
            d = Document(original_name=name, file_type="")
            self.assertEqual(d.badge_kind, kind, name)
            self.assertEqual(d.badge_label, label, name)


class PhotoUploadTests(TestCase):
    """照片直传（JPG/PNG/WebP）：白名单、IMG 徽章、标准化 → 图片块全链。"""

    def test_badge_for_image_files(self):
        for name in ("现场照片.jpg", "IMG_3421.jpeg", "截图.png", "scan.webp"):
            d = Document(original_name=name, file_type="")
            self.assertEqual(d.badge_kind, "img", name)
            self.assertEqual(d.badge_label, "IMG", name)

    def test_upload_whitelist_accepts_jpg_rejects_gif(self):
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (60, 40), (200, 30, 30)).save(buf, format="JPEG")

        user = get_user_model().objects.create_user(
            username="photo-op", password="pw", is_staff=True)
        lib = KnowledgeBase.objects.create(
            name="照片库", slug="photo-lib", is_folder=False, created_by=user)
        self.client.force_login(user)
        url = reverse("kb:manage_detail", args=["photo-lib"])
        with patch("kb.pipeline.process_document_async"):
            r = self.client.post(url, {
                "file": SimpleUploadedFile(
                    "control-cabinet.jpg", buf.getvalue(), content_type="image/jpeg"),
            }, follow=True)
        self.assertContains(r, "已上传")
        doc = Document.objects.get(original_name="control-cabinet.jpg")
        self.assertEqual(doc.file_type, "image")
        self.assertContains(r, "ftype-img")            # 列表行带 IMG 徽章
        self.assertContains(r, ">IMG</span>")
        self.assertContains(r, ".jpg,.jpeg,.png,.webp")  # 文件选择框放开图片
        # 非图片且非文档类型 → 明确拒绝
        with patch("kb.pipeline.process_document_async"):
            r2 = self.client.post(url, {
                "file": SimpleUploadedFile("anim.gif", b"GIF89a", content_type="image/gif"),
            }, follow=True)
        self.assertContains(r2, "仅支持")
        self.assertFalse(
            Document.objects.filter(original_name="anim.gif").exists())

    def test_process_photo_to_image_chunk_pipeline(self):
        """_process_photo 标准化 → 落盘 → _image_chunks 识别为图片块（无外部服务）。"""
        import io as _io
        import os
        import re
        import tempfile
        import uuid
        from django.test import override_settings
        from PIL import Image
        from kb import pipeline

        # 噪点图：JPEG 重编码后必然 > _IMG_MIN_BYTES(3KB)
        buf = _io.BytesIO()
        im = Image.new("RGB", (220, 160))
        im.putdata([(i % 251, (i * 97) % 241, (i * 31) % 233)
                    for i in range(220 * 160)])
        im.save(buf, format="PNG")
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "设备照片.png")
            open(src, "wb").write(buf.getvalue())
            md, images, _cl = pipeline.run_ocr_with_images(
                __import__("pathlib").Path(src), "image")
            self.assertTrue(md.startswith("照片上传：设备照片.png"))
            self.assertEqual(len(images), 1)
            (name, data_url), = images.items()
            self.assertRegex(name, r"^[0-9a-f]{64}\.jpg$")
            self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))

            doc_id = str(uuid.uuid4())
            with override_settings(DATA_DIR=td):
                n = pipeline.save_doc_images(doc_id, images)
                self.assertEqual(n, 1)
                items = pipeline._image_chunks(md, "设备照片.png", doc_id)
            self.assertEqual(len(items), 1)
            chunk, img_path = items[0]
            self.assertEqual(chunk.metadata["type"], "image")
            self.assertEqual(chunk.metadata["image"], name)
            self.assertIn("照片上传", chunk.page_content)
            self.assertEqual(img_path.name, name)


class UnifiedDialogTests(TestCase):
    """全站弹窗统一：原生 confirm/prompt 清零，改走 fos-dialog（添加账号同款）。"""

    def test_base_defines_global_dialog_helpers(self):
        user = get_user_model().objects.create_user(
            username="dlg-admin", password="pw", is_staff=True)
        self.client.force_login(user)
        r = self.client.get(reverse("kb:manage_list"))
        self.assertContains(r, "window.fosConfirm")
        self.assertContains(r, "window.fosPrompt")

    def test_delete_forms_use_declarative_confirm(self):
        from kb.models import Document
        user = get_user_model().objects.create_user(
            username="dlg-admin2", password="pw", is_staff=True)
        folder = KnowledgeBase.objects.create(
            name="弹窗库", slug="dlg-folder", is_folder=True, created_by=user)
        self.client.force_login(user)
        r = self.client.get(reverse("kb:manage_list"))
        # KB 删除表单：声明式 data-confirm，不再有 onsubmit=confirm
        self.assertContains(r, 'data-confirm-title="删除知识库"')
        self.assertNotContains(r, "onsubmit=")
        r2 = self.client.get(reverse("kb:manage_detail", args=["dlg-folder"]))
        self.assertContains(r2, "await fosConfirm")   # 文档删除
        self.assertContains(r2, "await fosPrompt")    # 描述编辑


class KbCreateDialogTests(TestCase):
    """新建手册库：统一弹窗 + AJAX JSON 分支（卡片实时插入由前端完成）。"""

    def test_page_has_dialog_and_ajax_create(self):
        user = get_user_model().objects.create_user(
            username="kbdlg-admin", password="pw", is_staff=True)
        self.client.force_login(user)
        r = self.client.get(reverse("kb:manage_list"))
        self.assertContains(r, 'id="kbCreateDialog"')
        self.assertContains(r, 'id="openKbCreate"')
        self.assertNotContains(r, "createForm")  # 旧内联面板已移除
        # AJAX 创建 → JSON ok + 库落库
        r2 = self.client.post(reverse("kb:manage_list"),
                              {"action": "create_kb", "name": "弹窗建的库",
                               "description": "测试", "department": "通用"},
                              HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        data = r2.json()
        self.assertTrue(data["ok"], data)
        self.assertTrue(KnowledgeBase.objects.filter(name="弹窗建的库").exists())
        # 空名称 → JSON ok=false
        r3 = self.client.post(reverse("kb:manage_list"),
                              {"action": "create_kb", "name": "  "},
                              HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(r3.json()["ok"])


class KbCardActionTests(TestCase):
    """资料上传首页每张卡片只留「知识库主页 + 删除」，其余入口收进知识库主页。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="card-actions", password="pw", is_staff=True)
        self.manual = KnowledgeBase.objects.create(
            name="卡片手册库", slug="card-manual", is_folder=True, created_by=self.user)
        self.business = KnowledgeBase.objects.create(
            name="卡片业务库", slug="card-business", is_folder=True,
            category="business", created_by=self.user)
        self.client.force_login(self.user)
        self.html = self.client.get(reverse("kb:manage_list")).content.decode()

    def test_manual_card_keeps_home_and_delete_only(self):
        self.assertIn('href="' + reverse("kb:manage_detail", args=["card-manual"]) + '">知识库主页</a>', self.html)
        self.assertIn('data-confirm-title="删除知识库"', self.html)
        self.assertNotIn('/kb/ask/?kb=card-manual', self.html)          # 检索入口
        self.assertNotIn('data-rn-toggle', self.html)                    # 改名入口
        self.assertNotIn('id="kbRenameDialog"', self.html)               # 随之失效的弹窗

    def test_business_card_keeps_home_and_delete_only(self):
        self.assertIn('data-business-slug="card-business"', self.html)
        self.assertIn('href="' + reverse("kb:manage_detail", args=["card-business"]) + '">知识库主页</a>', self.html)
        self.assertIn('data-confirm-title="删除采集库"', self.html)
        self.assertNotIn(reverse("kb:tracker", args=["card-business"]), self.html)   # 查看追踪表
        self.assertNotIn("查看追踪表", self.html)
        self.assertNotIn("管理自动更新", self.html)
