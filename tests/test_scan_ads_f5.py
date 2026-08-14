from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import scan_ads  # noqa: E402
import parse_structure  # noqa: E402


class ScanAdsF5Tests(unittest.TestCase):
    def test_every_shared_chapter_heading_is_excluded_from_ad_blocks(self) -> None:
        headings = (
            "第六部 风起",
            "第 12 節 夜雨",
            "第兩百話 重逢",
            "卷三 山河",
            "後記",
            "尾聲",
            "終章",
            "Chapter-001 Arrival",
            "Prologue",
            "Epilogue Return",
        )
        for heading in headings:
            with self.subTest(heading=heading):
                self.assertIsNotNone(parse_structure.match_chapter(heading))
                self.assertFalse(scan_ads.is_candidate_block(heading, 1))
                self.assertFalse(scan_ads.is_short_external_block(heading))

    def test_short_external_lines_bypass_normal_minimum_length(self) -> None:
        text = "\n".join(
            ("a@b.test", "c@d.test", "example.org", "example.net", "普通短句")
        )
        metrics: dict[str, int] = {}

        blocks = scan_ads.split_blocks(text, min_chars=12, metrics=metrics)
        by_text = {block["text"]: block for block in blocks}

        self.assertIn("email", by_text["a@b.test"]["signals"])
        self.assertIn("email", by_text["c@d.test"]["signals"])
        self.assertIn("url", by_text["example.org"]["signals"])
        self.assertIn("url", by_text["example.net"]["signals"])
        self.assertNotIn("普通短句", by_text)
        self.assertEqual(metrics["short_external_block_count"], 4)

    def test_short_promotion_cues_remain_visible_to_residual_scans(self) -> None:
        promotion = "星灯阅读站提示：请访问以下地址"
        for phrase in (promotion, "请访问example.org获取更新"):
            with self.subTest(phrase=phrase):
                blocks = scan_ads.split_blocks(phrase, min_chars=12)
                candidates, _ = scan_ads.scan_candidates(phrase, min_chars=12)

                self.assertEqual([block["text"] for block in blocks], [phrase])
                self.assertIn("watermark", blocks[0]["signals"])
                self.assertTrue(candidates)

        candidates, _ = scan_ads.scan_candidates(
            f"{promotion}\nexample.org\n",
            min_chars=12,
        )
        originals = {
            str(anchor["original"])
            for candidate in candidates
            for anchor in candidate["anchors"]
        }
        self.assertIn(promotion, originals)
        self.assertIn("example.org", originals)

    def test_narrative_substrings_are_not_source_markers(self) -> None:
        narrative_phrases = (
            "运转自身",
            "回转自己",
            "圆转自如",
            "扭转自己",
            "精神扫描",
            "整理衣服",
            "抬手打断",
        )

        for phrase in narrative_phrases:
            with self.subTest(phrase=phrase):
                signals = scan_ads.signal_names(phrase)
                self.assertNotIn("watermark", signals)
                self.assertNotIn("copy_marker", signals)

    def test_bare_copy_verbs_near_generic_web_words_are_not_source_markers(self) -> None:
        narrative_phrases = (
            "随手打开网页，登录拓荒网站",
            "两人手打脚踢闯进网站",
            "她整理衣服后打开网站",
            "他校对答案后登录网站",
            "录入成绩后关闭文本",
            "扫描完庭院便返回书库",
            "他制作网站模型供课堂展示",
            "运转自星灯网站记载的阵法",
            "他随手打版画作为礼物",
            "扫描版本仍需核对后才能归档",
        )

        for phrase in narrative_phrases:
            with self.subTest(phrase=phrase):
                signals = scan_ads.signal_names(phrase)
                self.assertNotIn("watermark", signals)
                self.assertNotIn("copy_marker", signals)

    def test_source_grammar_never_bridges_sentence_punctuation(self) -> None:
        narrative_phrases = (
            "他整理衣服。随后打开网站",
            "她校对答案，随后登录网站",
            "他录入成绩；最后关闭文本",
            "设备扫描结束：众人返回书库",
        )

        for phrase in narrative_phrases:
            with self.subTest(phrase=phrase):
                signals = scan_ads.signal_names(phrase)
                self.assertNotIn("watermark", signals)
                self.assertNotIn("copy_marker", signals)

    def test_copy_source_context_still_emits_auditable_signal(self) -> None:
        source_phrases = (
            "本书由OCR扫描组整理",
            "全文手打版，仅供学习交流",
            "电子书由整理组录入",
            "本书由星灯公众号整理制作",
            "本文经青禾工作室校对",
            "转自星灯网站",
            "本文转自星灯网站",
            "内容来源：青禾论坛",
            "扫描版",
        )
        for phrase in source_phrases:
            with self.subTest(phrase=phrase):
                signals = scan_ads.signal_names(phrase)
                self.assertIn("watermark", signals)
                self.assertIn("copy_marker", signals)

    def test_adjacent_promotion_and_short_domain_remain_separate_candidates(self) -> None:
        promotion = "星灯阅读站提示：请访问以下地址"
        domain = "example.org"
        text = f"{promotion}\n{domain}\n这是后续的正常小说正文，不应并入广告锚点。"

        candidates, summary = scan_ads.scan_candidates(text, max_candidates=20)
        promotion_candidate = next(item for item in candidates if item["sample"] == promotion)
        domain_candidate = next(item for item in candidates if item["sample"] == domain)

        self.assertEqual([item["original"] for item in promotion_candidate["anchors"]], [promotion])
        self.assertEqual([item["original"] for item in domain_candidate["anchors"]], [domain])
        self.assertEqual(promotion_candidate["neighbor_spans"][0]["original"], domain)
        self.assertEqual(domain_candidate["neighbor_spans"][0]["original"], promotion)
        self.assertEqual(summary["signal_metrics"]["neighbor_pair_count"], 1)

    def test_one_blank_line_is_allowed_but_intervening_body_is_not(self) -> None:
        promotion = "星灯阅读站提示：请访问以下地址"
        domain = "example.org"

        blank_separated = scan_ads.split_blocks(f"{promotion}\n\n{domain}", min_chars=12)
        self.assertTrue(blank_separated[0].get("_neighbor_spans"))

        body_separated = scan_ads.split_blocks(f"{promotion}\n他笑了。\n{domain}", min_chars=12)
        self.assertFalse(body_separated[0].get("_neighbor_spans"))
        self.assertFalse(body_separated[-1].get("_neighbor_spans"))

    def test_suppressed_copy_marker_count_is_reported(self) -> None:
        metrics: dict[str, int] = {}
        scan_ads.split_blocks(
            "众人在浓郁的光明气息中运转自身魂力，不敢有丝毫分心。",
            min_chars=12,
            metrics=metrics,
        )

        self.assertEqual(metrics["suppressed_copy_marker_block_count"], 1)


if __name__ == "__main__":
    unittest.main()
