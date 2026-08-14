from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ad_decision_policy  # noqa: E402
import verify  # noqa: E402


def anchor(anchor_id: str, text: str, offset: int) -> dict[str, object]:
    return {
        "anchor_id": anchor_id,
        "offset": offset,
        "end": offset + len(text),
        "original": text,
    }


def source_candidate(
    candidate_id: str,
    anchors: list[dict[str, object]],
    *,
    truncated: bool = False,
    occurrence_count: int | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "candidate_fingerprint": hashlib.sha256(candidate_id.encode()).hexdigest(),
        "anchors_truncated": truncated,
        "occurrence_count": len(anchors) if occurrence_count is None else occurrence_count,
        "anchors": anchors,
    }


def keep_decision(candidate: dict[str, object]) -> dict[str, object]:
    occurrences = ad_decision_policy.validate_candidate_occurrences(candidate)
    basis = {
        "schema": ad_decision_policy.KEEP_BASIS_SCHEMA,
        "type": "narrative_context",
        "reviewed_occurrences": occurrences,
        "occurrence_coverage_sha256": (
            ad_decision_policy.occurrence_coverage_sha256(occurrences)
        ),
        "note": "这些网址是人物调查案件时必须保留的剧情证据。",
    }
    return {
        "candidate_id": candidate["candidate_id"],
        "candidate_fingerprint": candidate["candidate_fingerprint"],
        "verdict": "keep",
        "anchors_truncated": candidate["anchors_truncated"],
        "occurrence_count": candidate["occurrence_count"],
        "anchor_ids": [item["anchor_id"] for item in occurrences],
        "anchor_text_sha256s": [item["text_sha256"] for item in occurrences],
        "keep_basis": basis,
    }


def residual_candidate(anchors: list[dict[str, object]]) -> dict[str, object]:
    return {
        "candidate_id": "AD-rescanned",
        "priority": "high",
        "risk_hint": "low",
        "occurrence_count": len(anchors),
        "anchors_truncated": False,
        "anchors": anchors,
    }


class VerifyKeepBasisMappingTests(unittest.TestCase):
    def test_evidence_less_basis_consumes_every_mapped_occurrence_once(self) -> None:
        kept = "https://plot.example/case"
        before = f"广告甲\n{kept}\n广告乙\n{kept}\n"
        first = before.index(kept)
        second = before.index(kept, first + 1)
        source = source_candidate(
            "AD-source",
            [anchor("AN-first", kept, first), anchor("AN-second", kept, second)],
        )
        operations = [
            {
                "anchor_id": "AN-delete-1",
                "action": "delete",
                "start": 0,
                "end": len("广告甲\n"),
                "original": "广告甲\n",
                "replacement": "",
            },
            {
                "anchor_id": "AN-delete-2",
                "action": "delete",
                "start": before.index("广告乙\n"),
                "end": before.index("广告乙\n") + len("广告乙\n"),
                "original": "广告乙\n",
                "replacement": "",
            },
        ]
        after, issues = verify.replay_operations(before, operations)
        self.assertEqual(issues, [])
        assert after is not None
        final_first = after.index(kept)
        final_second = after.index(kept, final_first + 1)
        residual = residual_candidate(
            [
                anchor("rescanned-1", kept, final_first),
                anchor("rescanned-2", kept, final_second),
            ]
        )

        self.assertEqual(
            verify.residual_records(
                [residual],
                [keep_decision(source)],
                source_candidates=[source],
                operations=operations,
            ),
            [],
        )

    def test_invalid_incomplete_or_truncated_basis_never_suppresses(self) -> None:
        kept = "reader.example.com/story"
        anchors = [anchor("AN-1", kept, 0), anchor("AN-2", kept, len(kept) + 1)]
        source = source_candidate("AD-source", anchors)
        residual = residual_candidate(anchors)
        valid = keep_decision(source)

        incomplete = copy.deepcopy(valid)
        incomplete["keep_basis"]["reviewed_occurrences"].pop()
        malformed_hash = copy.deepcopy(valid)
        malformed_hash["keep_basis"]["reviewed_occurrences"][0]["text_sha256"] = "0" * 64
        truncated_source = source_candidate(
            "AD-source",
            anchors,
            truncated=True,
            occurrence_count=3,
        )

        for label, decision, candidates in (
            ("incomplete", incomplete, [source]),
            ("hash", malformed_hash, [source]),
            ("truncated", valid, [truncated_source]),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    len(
                        verify.residual_records(
                            [residual],
                            [decision],
                            source_candidates=candidates,
                            operations=[],
                        )
                    ),
                    1,
                )

    def test_overlapping_delete_or_reused_basis_occurrence_never_suppresses(self) -> None:
        kept = "https://plot.example/evidence"
        source = source_candidate("AD-source", [anchor("AN-1", kept, 2)])
        decision = keep_decision(source)
        residual = residual_candidate([anchor("rescanned", kept, 2)])
        overlapping = [
            {
                "anchor_id": "AN-delete",
                "action": "delete",
                "start": 1,
                "end": 3,
                "original": "xx",
                "replacement": "",
            }
        ]

        self.assertEqual(
            len(
                verify.residual_records(
                    [residual],
                    [decision],
                    source_candidates=[source],
                    operations=overlapping,
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                verify.residual_records(
                    [residual],
                    [decision, copy.deepcopy(decision)],
                    source_candidates=[source],
                    operations=[],
                )
            ),
            1,
        )

    def test_matching_hash_at_wrong_final_offset_is_not_consumed(self) -> None:
        kept = "reader.example.com/plot"
        source = source_candidate("AD-source", [anchor("AN-1", kept, 10)])
        wrong_location = residual_candidate([anchor("rescanned", kept, 0)])

        self.assertEqual(
            len(
                verify.residual_records(
                    [wrong_location],
                    [keep_decision(source)],
                    source_candidates=[source],
                    operations=[],
                )
            ),
            1,
        )

    def test_malformed_ranges_fail_closed_instead_of_crashing(self) -> None:
        kept = "reader.example.com/plot"
        source = source_candidate("AD-source", [anchor("AN-1", kept, 0)])
        decision = keep_decision(source)
        residual = residual_candidate([anchor("rescanned", kept, 0)])

        for label, broken_residual, operations in (
            (
                "residual-original",
                {**residual, "anchors": [{"offset": 0, "end": 1, "original": 3}]},
                [],
            ),
            (
                "operation-start",
                residual,
                [
                    {
                        "action": "delete",
                        "start": "0",
                        "end": 1,
                        "original": "x",
                        "replacement": "",
                    }
                ],
            ),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    len(
                        verify.residual_records(
                            [broken_residual],
                            [decision],
                            source_candidates=[source],
                            operations=operations,
                        )
                    ),
                    1,
                )


if __name__ == "__main__":
    unittest.main()
