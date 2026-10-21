"""kb_image_search 工具（_image_search_core / _softmax_probs / _parse_img_verdict）的单元测试。"""
from unittest import mock

from django.test import TestCase

from kb.agent import (_IMG_REVIEW_PROB, _image_search_core,
                      _judge_candidate, _parse_img_verdict, _softmax_probs)

# 距离按升序（= 相似度降序）：0.45/0.9/1.2 → 概率高度集中在第 1 张
CANDS = [
    {"text": "图 3 超声检测探头布置示意图", "source": "GB-UT.pdf", "section": "第二章",
     "type": "image", "image": "img-a.png", "doc_id": "d1", "distance": 0.45},
    {"text": "表 1 验收标准", "source": "GB-UT.pdf", "section": "第三章",
     "type": "image", "image": "img-b.png", "doc_id": "d1", "distance": 0.90},
    {"text": "现场照片", "source": "ASTM.pdf", "section": "",
     "type": "image", "image": "img-c.png", "doc_id": "d2", "distance": 1.20},
]

MATCH_JSON = ('```json\n{"match": true, "confidence": 88, '
              '"caption": "超声检测探头布置示意", "reason": "与需求一致"}\n```')
NOMATCH_JSON = '{"match": false, "confidence": 75, "caption": "一张数据表格", "reason": "不是示意图"}'


class _FakeEmb:
    def embed_query(self, q):
        return [0.0, 1.0]


def _patches(lane_cands, judge_text=None, img_blk=None):
    """core 依赖的四件套：嵌入、图片召回道、图片块、复核 LLM。"""
    get_llm = mock.Mock(
        return_value=mock.Mock(invoke=mock.Mock(return_value=mock.Mock(text=judge_text))))
    return [
        mock.patch("kb.pipeline._embeddings", return_value=_FakeEmb()),
        mock.patch("kb.retriever._image_lane", return_value=list(lane_cands)),
        mock.patch("kb.agent._image_content_block",
                   return_value=img_blk or {"type": "image", "base64": "eHg=",
                                            "mime_type": "image/png"}),
        mock.patch("kb.agent._get_llm", get_llm),
    ], get_llm


class SoftmaxTests(TestCase):
    def test_dominant_candidate_concentrates(self):
        p = _softmax_probs([0.45, 0.90, 1.20])
        self.assertGreater(p[0], 0.9)
        self.assertLess(p[1], 0.3)

    def test_flat_distribution_spreads_evenly(self):
        p = _softmax_probs([0.6] * 8)
        self.assertAlmostEqual(sum(p), 1.0, places=6)
        for x in p:
            self.assertLess(x, _IMG_REVIEW_PROB)  # 全部低于 30% 门槛

    def test_none_distance_treated_as_worst(self):
        p = _softmax_probs([0.5, None])
        self.assertGreater(p[0], 0.9)


class ParseVerdictTests(TestCase):
    def test_fenced_json(self):
        v = _parse_img_verdict(MATCH_JSON)
        self.assertIs(v["match"], True)
        self.assertEqual(v["confidence"], 88)
        self.assertIn("示意", v["caption"])

    def test_plain_json_false(self):
        v = _parse_img_verdict(NOMATCH_JSON)
        self.assertIs(v["match"], False)

    def test_garbage_returns_unparsed(self):
        v = _parse_img_verdict("我觉得挺匹配的")
        self.assertIsNone(v["match"])
        self.assertIn("无法解析", v["note"])

    def test_missing_match_field(self):
        v = _parse_img_verdict('{"confidence": 50}')
        self.assertIsNone(v["match"])
        self.assertIn("match", v["note"])


class JudgeCandidateTests(TestCase):
    """审图调用链：system 约束 → 散文重试 → 关键词兜底（qwen 无视格式的实测场景）。"""

    def _judge(self, text):
        j = mock.Mock()
        j.invoke.return_value = mock.Mock(text=text)
        return j

    def test_system_constraint_sent(self):
        j = self._judge(MATCH_JSON)
        v = _judge_candidate(j, "示意图", "data:image/png;base64,eHg=")
        self.assertIs(v["match"], True)
        first = j.invoke.call_args_list[0].args[0]
        self.assertEqual(first[0][0], "system")   # 首条必须是 system 格式约束
        self.assertIn("JSON", first[0][1])
        self.assertEqual(j.invoke.call_count, 1)  # JSON 合规不重试

    def test_prose_triggers_retry_then_keyword_match(self):
        j = self._judge("匹配。理由：这是一张标准的探头布置示意图，图中展示了 A 到 G 多个探头位置。")
        v = _judge_candidate(j, "示意图", "data:image/png;base64,eHg=")
        self.assertIs(v["match"], True)
        self.assertEqual(v["reason"], "按关键词判定")
        self.assertEqual(j.invoke.call_count, 2)  # 散文 → 带提醒重试一次

    def test_keyword_nomatch_checked_first(self):
        j = self._judge("判断结果：不匹配。理由：类型不符，候选是一张实物照片而非示意图。")
        v = _judge_candidate(j, "示意图", "data:image/png;base64,eHg=")
        self.assertIs(v["match"], False)
        self.assertEqual(v["reason"], "按关键词判定")

    def test_unrelated_answer_stays_unparsed(self):
        v = _judge_candidate(self._judge("抱歉，我无法处理这个请求。"),
                             "示意图", "data:image/png;base64,eHg=")
        self.assertIsNone(v["match"])


class ImageSearchCoreTests(TestCase):
    def _run(self, cands, judge_text, llm_cfg, cite_sink=None, max_out=3):
        cite_sink = cite_sink if cite_sink is not None else []
        patches, get_llm = _patches(cands, judge_text)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _image_search_core("超声检测示意图", "ndt-ut", ["ndt-ut"],
                                     llm_cfg, cite_sink, max_out)
        return out, cite_sink, get_llm

    def test_confirmed_match_returns_multimodal_blocks(self):
        out, cite, _ = self._run(CANDS, MATCH_JSON, {"vision": True})
        # 视觉模式 + 有确认匹配 → list[dict]：文本块 + 图片本体块
        self.assertIsInstance(out, list)
        self.assertEqual(out[0]["type"], "text")
        self.assertIn("确认匹配 1 张", out[0]["text"])
        self.assertIn("✓", out[0]["text"])
        self.assertEqual(out[1]["type"], "image")
        # 确认图片挂到引用区（前端自动渲染）
        self.assertEqual(cite[0]["images"], ["/kb/doc/d1/img/img-a.png"])

    def test_no_match_reports_honestly_without_citations(self):
        out, cite, _ = self._run(CANDS, NOMATCH_JSON, {"vision": True})
        self.assertIsInstance(out, str)
        self.assertIn("均未确认匹配", out)
        self.assertIn("✗", out)
        self.assertEqual(cite, [])

    def test_vision_off_degrades_to_recall_only(self):
        out, cite, get_llm = self._run(CANDS, None, {"vision": False})
        self.assertIsInstance(out, str)
        self.assertIn("仅向量召回", out)
        self.assertIn("未开启", out)
        self.assertIn("○", out)
        get_llm.assert_not_called()  # 不开视觉绝不调复核模型
        # 降级模式仍把概率前列的图附到引用区供用户自查
        urls = [u for e in cite for u in e.get("images", [])]
        self.assertEqual(urls, ["/kb/doc/d1/img/img-a.png",
                                "/kb/doc/d1/img/img-b.png",
                                "/kb/doc/d2/img/img-c.png"])

    def test_empty_recall(self):
        out, cite, _ = self._run([], None, {"vision": True})
        self.assertIn("未召回图片候选", out)
        self.assertEqual(cite, [])

    def test_duplicates_deduped(self):
        dup = [dict(CANDS[0]), dict(CANDS[0]), dict(CANDS[1])]
        out, _, _ = self._run(dup, MATCH_JSON, {"vision": True})
        text = out[0]["text"] if isinstance(out, list) else out
        self.assertIn("[1]", text)
        self.assertIn("[2]", text)
        self.assertNotIn("[3]", text)

    def test_flat_distribution_reviews_only_top1(self):
        flat = [dict(CANDS[0], distance=0.6, image=f"i{n}.png")
                for n in range(8)]
        out, cite, get_llm = self._run(flat, MATCH_JSON, {"vision": True})
        # 各候选概率均 <15% → 保底只复核头名（不空转也不滥烧复核）
        self.assertEqual(get_llm.return_value.invoke.call_count, 1)
        self.assertIn("复核 1 张", out[0]["text"])

    def test_review_gate_top3_cap(self):
        # 5 张候选概率全部 ≥15%（≈24/22/20/18/16%）→ 仍只复核概率前 3 名
        cands = [dict(CANDS[0], distance=0.40 + 0.005 * n, image=f"t{n}.png")
                 for n in range(5)]
        out, cite, get_llm = self._run(cands, MATCH_JSON, {"vision": True})
        self.assertIn("复核 3 张", out[0]["text"])
        self.assertEqual(get_llm.return_value.invoke.call_count, 3)

    def test_review_gate_below_15_skipped(self):
        # 头名概率 ~100%、第 2/3 名远低于 15% → 只有头名送审
        out, cite, get_llm = self._run(CANDS, MATCH_JSON, {"vision": True})
        self.assertIn("复核 1 张", out[0]["text"])
        self.assertIn("未进概率前 3，或概率低于 15%", out[0]["text"])

    def test_page_block_uses_evidence_url(self):
        cands = [dict(CANDS[0], image="page3", chunk_id="img-d1-page3")]
        out, cite, _ = self._run(cands, MATCH_JSON, {"vision": True})
        self.assertEqual(cite[0]["images"], ["/kb/evidence/img-d1-page3/page.png"])

    def test_max_out_trims_confirmed(self):
        # 前两名概率 ≈55%/45%（均过 30% 门槛、均复核匹配），max_out=1 只输出最优一张
        cands = [dict(CANDS[0], distance=0.40, image="g0.png"),
                 dict(CANDS[0], distance=0.41, image="g1.png"),
                 dict(CANDS[1], distance=0.90)]
        out, cite, _ = self._run(cands, MATCH_JSON, {"vision": True}, max_out=1)
        self.assertIn("复核 2 张", out[0]["text"])
        self.assertIn("确认匹配 1 张", out[0]["text"])
        urls = [u for e in cite for u in e.get("images", [])]
        self.assertEqual(urls, ["/kb/doc/d1/img/g0.png"])
