from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import make_ad_decisions as decisions  # noqa: E402


def candidate(
    candidate_id: str,
    sample: str,
    signals: list[str],
    *,
    anchors: int = 1,
    neighbor_spans: list[dict] | None = None,
) -> dict:
    item = {
        "candidate_id": candidate_id,
        "layer": "L3",
        "detector": "pattern-hit",
        "risk_hint": "low",
        "sample": sample,
        "signals": signals,
        "signal_strength": "strong" if signals else "none",
        "occurrence_count": anchors,
        "anchors": [
            {
                "offset": index * 100,
                "line": index + 1,
                "original": sample,
                "prefix": "正文\n\n",
                "suffix": "\n正文",
            }
            for index in range(anchors)
        ],
        "contexts": [
            {"before": "人物乙正在练习。\n\n", "original": sample, "after": "\n正文继续。"}
        ],
    }
    if neighbor_spans is not None:
        item["neighbor_spans"] = neighbor_spans
    return item


class DraftDecisionTests(unittest.TestCase):
    def test_protected_terms_ignore_surrounding_context(self) -> None:
        item = candidate("AD-1", "欢迎访问星灯阅读站", ["reader_site"])
        self.assertEqual(decisions.protected_hits(item, ["人物乙"]), [])
        self.assertEqual(decisions.draft_for_candidate(item, ["人物乙"])["verdict"], "uncertain")

    def test_protected_terms_still_guard_candidate_owned_text(self) -> None:
        item = candidate("AD-1", "人物甲访问星灯阅读站", ["reader_site"])
        draft = decisions.draft_for_candidate(item, ["人物甲"])
        self.assertEqual(draft["verdict"], "uncertain")
        self.assertEqual(draft["protected_terms"], ["人物甲"])

    def test_batch_uses_two_evidence_types_and_keeps_anchors_isolated(self) -> None:
        seed = candidate(
            "AD-SEED",
            "星灯阅读站：欢迎访问 example.com",
            ["url", "reader_site"],
        )
        obvious = candidate(
            "AD-OBVIOUS",
            "作者荐：喜欢小说的星灯阅读站，欢迎访问 example.com",
            ["reader_site"],
        )
        neighbor = candidate(
            "AD-NEIGHBOR",
            "作者：优秀的在线阅读网站",
            ["watermark"],
            anchors=2,
            neighbor_spans=[
                {
                    "source_offset": offset,
                    "source_line": index + 1,
                    "neighbor_offset": offset + 20,
                    "neighbor_line": index + 2,
                    "direction": "after",
                    "line_distance": 1,
                    "original": "星灯阅读站(example.com)",
                    "signals": ["url", "reader_site"],
                }
                for index, offset in enumerate((0, 100))
            ],
        )
        narrative = candidate(
            "AD-STORY",
            "人物丙运转自身真气，缓缓恢复。",
            ["watermark"],
        )
        similarity_only = candidate(
            "AD-SIMILAR",
            "作者荐：喜欢小说的欢迎访问",
            [],
        )

        drafts, report = decisions.build_draft_decisions(
            [seed, obvious, neighbor, narrative, similarity_only], []
        )
        by_id = {draft["candidate_id"]: draft for draft in drafts}

        self.assertEqual(by_id["AD-SEED"]["verdict"], "delete")
        self.assertEqual(by_id["AD-OBVIOUS"]["verdict"], "delete")
        self.assertEqual(by_id["AD-NEIGHBOR"]["verdict"], "delete")
        self.assertEqual(by_id["AD-STORY"]["verdict"], "keep")
        self.assertEqual(by_id["AD-SIMILAR"]["verdict"], "uncertain")
        self.assertEqual(by_id["AD-NEIGHBOR"]["anchors"], neighbor["anchors"])
        self.assertEqual(len(by_id["AD-NEIGHBOR"]["neighbor_span"]), 2)
        self.assertEqual(by_id["AD-OBVIOUS"]["promoted_from"], [])
        self.assertEqual(report["rule_upgrade_count"], 3)
        self.assertEqual(report["narrative_keep_count"], 1)

    def test_missing_anchors_cannot_be_promoted(self) -> None:
        item = candidate(
            "AD-NO-ANCHOR",
            "星灯阅读站：欢迎访问 example.com",
            ["url", "reader_site"],
            anchors=0,
        )
        drafts, report = decisions.build_draft_decisions([item], [])
        self.assertEqual(drafts[0]["verdict"], "uncertain")
        self.assertTrue(drafts[0]["review_required"])
        self.assertEqual(report["anchor_block_count"], 1)

    def test_truncated_anchors_cannot_be_promoted_to_delete(self) -> None:
        item = candidate(
            "AD-TRUNCATED",
            "星灯阅读站：欢迎访问 example.com",
            ["url", "reader_site"],
            anchors=2,
        )
        item["anchors_truncated"] = True

        drafts, report = decisions.build_draft_decisions([item], [])

        self.assertEqual(drafts[0]["verdict"], "uncertain")
        self.assertEqual(drafts[0]["blocking_reasons"], ["anchors_truncated"])
        self.assertTrue(drafts[0]["review_required"])
        self.assertEqual(report["truncated_anchor_block_count"], 1)

    def test_family_similarity_is_one_hop_from_an_original_seed(self) -> None:
        shared = "作者：优秀的在线阅读网站，免费在线阅读最新章节更新最快，欢迎访问"
        seed = candidate("AD-SEED", shared + "example.com", ["url", "reader_site"])
        member = candidate(
            "AD-MEMBER",
            shared,
            ["watermark"],
            anchors=3,
            neighbor_spans=[
                {
                    "source_offset": 0,
                    "direction": "after",
                    "line_distance": 1,
                    "original": "星灯阅读站 example.com",
                    "signals": ["reader_site"],
                }
            ],
        )

        drafts, report = decisions.build_draft_decisions([seed, member], [])
        by_id = {draft["candidate_id"]: draft for draft in drafts}
        self.assertEqual(by_id["AD-MEMBER"]["verdict"], "delete")
        self.assertEqual(by_id["AD-MEMBER"]["promoted_from"], ["AD-SEED"])
        self.assertEqual(report["original_seed_count"], 1)
        self.assertEqual(report["family_upgrade_count"], 1)
        self.assertEqual(report["comparison_count"], 1)


if __name__ == "__main__":
    unittest.main()
