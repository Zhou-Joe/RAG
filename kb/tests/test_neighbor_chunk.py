"""kb_neighbor_chunk 工具（_chunk_neighbors）的单元测试。"""
from django.test import TestCase

from kb.agent import _chunk_neighbors
from kb.models import Document, KnowledgeBase

MD = """## 第一章 总则

本标准规定了大型游乐设施无损检测的通用要求。

## 第二章 超声检测

超声检测（UT）应按材料类型分级执行。碳钢锻件的质量等级为 I 级时，当量缺陷尺寸不应大于 2mm。

## 第三章 验收

验收不合格的项目应复检两次后方可判定。
"""


class NeighborChunkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.kb = KnowledgeBase.objects.create(name="ndt", slug="ndt-ut")
        cls.doc = Document.objects.create(
            kb=cls.kb, original_name="GB-UT-test.pdf", file_type="pdf",
            md_content=MD, status=Document.Status.COMPLETED,
        )
        # 部门隔离对照组：机密部门的同名文档对其它部门不可见
        cls.kb_secret = KnowledgeBase.objects.create(
            name="secret", slug="secret-kb", department="机密部")
        Document.objects.create(
            kb=cls.kb_secret, original_name="GB-SECRET.pdf", file_type="pdf",
            md_content=MD, status=Document.Status.COMPLETED,
        )

    def test_prev_and_next(self):
        out = _chunk_neighbors("GB-UT-test.pdf", "超声检测（UT）应按材料类型", "both", "")
        self.assertIn("上一片段", out)
        self.assertIn("下一片段", out)
        self.assertIn("通用要求", out)      # 上一块（第一章）
        self.assertIn("验收不合格", out)    # 下一块（第三章）

    def test_direction_prev_only(self):
        out = _chunk_neighbors("GB-UT-test.pdf", "超声检测（UT）应按材料类型", "prev", "")
        self.assertIn("上一片段", out)
        self.assertNotIn("下一片段 ·", out)

    def test_boundary_at_doc_start(self):
        out = _chunk_neighbors("GB-UT-test.pdf", "本标准规定了大型游乐设施", "prev", "")
        self.assertIn("文档边界", out)

    def test_anchor_not_found(self):
        out = _chunk_neighbors("GB-UT-test.pdf", "这段文字在文档里根本不存在", "next", "")
        self.assertIn("未定位到", out)

    def test_anchor_too_short(self):
        out = _chunk_neighbors("GB-UT-test.pdf", "超", "next", "")
        self.assertIn("太短", out)

    def test_dept_isolation(self):
        out = _chunk_neighbors("GB-SECRET.pdf", "超声检测（UT）应按材料类型", "next", "研发部")
        self.assertIn("未找到", out)

    def test_dept_member_can_access_own(self):
        out = _chunk_neighbors("GB-SECRET.pdf", "超声检测（UT）应按材料类型", "next", "机密部")
        self.assertIn("验收不合格", out)

    def test_unknown_doc(self):
        out = _chunk_neighbors("nope.pdf", "随便什么内容", "next", "")
        self.assertIn("未找到", out)
