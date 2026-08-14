from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ad_rules  # noqa: E402
import build_review_html  # noqa: E402
import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import scan_ads  # noqa: E402
import scan_identity  # noqa: E402


class AdRulesF5Tests(unittest.TestCase):
    def test_source_marker_grammar_has_one_executable_owner(self) -> None:
        self.assertIs(scan_ads.SOURCE_MARKER_RE, ad_rules.SOURCE_MARKER_RE)
        self.assertIs(scan_ads.BARE_COPY_MARKER_RE, ad_rules.BARE_COPY_MARKER_RE)

    def test_shared_specs_drive_scanner_decision_features_and_visible_labels(self) -> None:
        site_keys = [spec.key for spec in ad_rules.SITE_SPECS]
        signal_keys = [spec.key for spec in ad_rules.SIGNAL_SPECS]
        intent_keys = [spec.key for spec in ad_rules.INTENT_SPECS]
        self.assertEqual(len(site_keys), len(set(site_keys)))
        self.assertEqual(len(signal_keys), len(set(signal_keys)))
        self.assertEqual(len(intent_keys), len(set(intent_keys)))
        self.assertTrue(all(spec.label for spec in ad_rules.SITE_SPECS))
        self.assertTrue(all(spec.label for spec in ad_rules.SIGNAL_SPECS))
        self.assertTrue(all(spec.label for spec in ad_rules.INTENT_SPECS))

        strong_pattern_keys = {name for name, _ in scan_ads.STRONG_PATTERNS}
        weak_pattern_keys = {name for name, _ in scan_ads.WEAK_PATTERNS}
        self.assertEqual(strong_pattern_keys, ad_rules.signal_keys("strong"))
        self.assertEqual(weak_pattern_keys, ad_rules.signal_keys("weak"))

        spec = next(item for item in ad_rules.SITE_SPECS if item.short_external)
        phrase = f"请访问{spec.aliases[0]}获取后续内容"
        self.assertIn("reader_site", scan_ads.signal_names(phrase))
        self.assertEqual(ad_rules.site_entities(phrase), {spec.key})
        self.assertIn("visit", ad_rules.promotion_intents(phrase))
        family = build_review_html.family_metadata(
            {"family_signature": {"site_entities": [spec.key], "intents": ["visit"]}},
            None,
            None,
        )
        self.assertEqual(family["family_label"], f"{spec.label} · 访问引导")

    def test_reserved_example_domain_is_a_locator_not_a_known_reader_site(self) -> None:
        text = "reader.example.com"
        entities = ad_rules.site_entities(text)
        self.assertEqual(entities, {"domain:example.com"})
        self.assertIn("url", scan_ads.signal_names(text))
        self.assertNotIn("reader_site", scan_ads.signal_names(text))

    def test_gold_catalog_has_full_ad_recall_and_zero_narrative_auto_delete(self) -> None:
        catalog = json.loads(
            (ROOT / "tests/fixtures/gold_manifest.json").read_text(encoding="utf-8")
        )["candidate_catalog"]
        recalled = 0
        narrative_delete_ids: list[str] = []
        for case in catalog:
            candidates, _ = scan_ads.scan_candidates(case["text"], max_candidates=20)
            scan_identity.attach_candidate_fingerprints(candidates)
            scan_identity.attach_anchor_ids(candidates)
            scan_ads.bind_edit_plans(candidates)
            drafts, _ = make_ad_decisions.build_draft_decisions(candidates, [])
            if case["expected_action"] == "delete":
                recalled += int(bool(candidates))
                self.assertTrue(
                    any(draft["verdict"] == "delete" for draft in drafts),
                    case["case_id"],
                )
            elif any(draft["verdict"] == "delete" for draft in drafts):
                narrative_delete_ids.append(case["case_id"])

        self.assertEqual(recalled, 48)
        self.assertEqual(narrative_delete_ids, [])

    def test_narrative_reference_guard_blocks_quoted_external_evidence(self) -> None:
        text = "告示写着“请访问 https://reader.example.com/evidence”，人物随即将它封存为场景证物。"
        candidates, _ = scan_ads.scan_candidates(text, max_candidates=20)
        drafts, report = make_ad_decisions.build_draft_decisions(candidates, [])

        self.assertTrue(candidates)
        self.assertTrue(all(draft["verdict"] != "delete" for draft in drafts))
        self.assertGreater(report["narrative_external_guard_count"], 0)

    def test_ambiguous_site_words_and_negated_references_never_auto_delete(self) -> None:
        spec = next(item for item in ad_rules.SITE_SPECS if item.short_external)
        label = spec.label
        alias = spec.aliases[0]
        cases = (
            f"他决定前往{label}查找失传多年的古籍。",
            f"她建议读完这封信，再前往{alias}与同伴会合。",
            f"他在{label}看书，天亮后前往学院。",
            f"院长请访问学者在{alias}看书，免得打扰白天上课的学生。",
            f"她建议访问城南，随后去{label}看书，清晨再出发。",
            f"掌门建议访问{alias}，与阁主商议结盟之事。",
            f"他建议访问{label}，查阅祖师留下的卷宗。",
            f"导游建议访问{alias}中写过的那座古寺。",
            "他没有请任何人访问 https://reader.example.com 在线阅读全文。",
        )
        for text in cases:
            with self.subTest(text=text):
                candidates, _ = scan_ads.scan_candidates(text, max_candidates=20)
                drafts, report = make_ad_decisions.build_draft_decisions(candidates, [])

                self.assertTrue(candidates)
                self.assertTrue(all(item["verdict"] != "delete" for item in drafts))
                if "没有" in text:
                    self.assertGreater(report["narrative_external_guard_count"], 0)

    def test_quoted_notice_with_character_actions_can_be_kept_without_plot_basis(self) -> None:
        text = "告示写着“请访问 https://reader.example.com/evidence”，她读完后把纸折好收进抽屉。"
        candidates, _ = scan_ads.scan_candidates(text, max_candidates=20)
        scan_identity.attach_candidate_fingerprints(candidates)
        scan_identity.attach_anchor_ids(candidates)
        drafts, report = make_ad_decisions.build_draft_decisions(candidates, [])
        scan_id = "a" * 64
        make_ad_decisions.bind_draft_identity(candidates, drafts, scan_id)
        reviews = [
            {
                "scan_id": scan_id,
                "candidate_id": candidate["candidate_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "verdict": "keep",
                "confidence": 0.99,
                "reason": "网址位于人物阅读并收起的告示叙事中",
            }
            for candidate in candidates
        ]

        decisions = finalize_ad_decisions.compile_formal_decisions(
            candidates,
            reviews,
            drafts,
            scan_id=scan_id,
        )

        self.assertTrue(candidates)
        self.assertTrue(all(draft["verdict"] != "delete" for draft in drafts))
        self.assertGreater(report["narrative_external_guard_count"], 0)
        self.assertTrue(all(decision["verdict"] == "keep" for decision in decisions))
        self.assertTrue(all("keep_basis" not in decision for decision in decisions))

    def test_chinese_site_visit_with_explicit_external_payload_can_auto_delete(self) -> None:
        spec = next(item for item in ad_rules.SITE_SPECS if item.short_external)
        text = f"请访问{spec.aliases[0]}获取后续内容。"
        candidates, _ = scan_ads.scan_candidates(text, max_candidates=20)
        drafts, _ = make_ad_decisions.build_draft_decisions(candidates, [])

        self.assertTrue(candidates)
        self.assertTrue(any(item["verdict"] == "delete" for item in drafts))

    def test_complete_source_watermark_is_a_whole_ad_not_a_mixed_suffix(self) -> None:
        text = "来源水印：本书由匿名整理组校对，仅供学习交流；请访问 reader.example.com/source。"
        candidates, _ = scan_ads.scan_candidates(text, max_candidates=20)
        scan_identity.attach_candidate_fingerprints(candidates)
        scan_identity.attach_anchor_ids(candidates)
        scan_ads.bind_edit_plans(candidates)
        drafts, _ = make_ad_decisions.build_draft_decisions(candidates, [])

        self.assertTrue(candidates)
        self.assertTrue(all(candidate.get("edit_plan") is None for candidate in candidates))
        self.assertTrue(any(item["verdict"] == "delete" for item in drafts))

    def test_long_line_signal_is_uncertain_and_cannot_delete_the_whole_line(self) -> None:
        text = "人物继续核对现场记录。" * 50 + "请访问 https://reader.example.com/update 获取更新。"
        candidates, _ = scan_ads.scan_candidates(text, max_candidates=20)
        scan_identity.attach_candidate_fingerprints(candidates)
        scan_identity.attach_anchor_ids(candidates)
        scan_ads.bind_edit_plans(candidates)
        drafts, _ = make_ad_decisions.build_draft_decisions(candidates, [])
        scan_id = "a" * 64
        make_ad_decisions.bind_draft_identity(candidates, drafts, scan_id)
        reviews = [
            {
                "scan_id": scan_id,
                "candidate_id": candidate["candidate_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "verdict": "delete",
                "confidence": 0.99,
                "reason": "尝试删除整行",
                "action": "delete",
                "splice_strategy": "remove_paragraph",
            }
            for candidate in candidates
        ]

        self.assertTrue(candidates)
        self.assertTrue(
            all(candidate.get("mutation_guard") == "long_line_mixed_content" for candidate in candidates)
        )
        self.assertTrue(all(draft["verdict"] == "uncertain" for draft in drafts))
        with self.assertRaisesRegex(ValueError, "long-line mixed-content"):
            finalize_ad_decisions.compile_formal_decisions(
                candidates,
                reviews,
                drafts,
                scan_id=scan_id,
            )


if __name__ == "__main__":
    unittest.main()
