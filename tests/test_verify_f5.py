from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify  # noqa: E402
import scan_ads  # noqa: E402
import ad_decision_policy  # noqa: E402


class VerifyF5Tests(unittest.TestCase):
    def test_strong_l2_near_repeat_is_a_residual_blocker(self) -> None:
        text = "\n".join(
            (
                "Visit https://reader.example.com/novel alpha chapter update",
                "Visit https://reader.example.com/novel beta chapter update",
                "Visit https://reader.example.com/novel gamma chapter update",
            )
        )
        candidates, _ = scan_ads.scan_candidates(text, near_scan_scope="all")
        residual = next(
            candidate
            for candidate in candidates
            if candidate.get("layer") == "L2"
        )

        self.assertEqual(residual["signal_strength"], "strong")
        self.assertEqual(residual["priority"], "high")
        self.assertEqual(residual["risk_hint"], "low")
        self.assertEqual(len(verify.residual_records([residual], [])), 1)

    def test_formal_keep_suppresses_the_same_exact_residual_text(self) -> None:
        original = "剧情线索中的 https://archive.example.com/case/17"
        decisions = [
            {
                "candidate_id": "AD-0042",
                "verdict": "keep",
                "anchors_truncated": False,
                "anchor_ids": ["AN-1"],
                "anchor_text_sha256s": [
                    hashlib.sha256(original.encode("utf-8")).hexdigest()
                ],
                "evidence": [{"type": "direct_signal", "value": ["url"]}],
            }
        ]
        residual_candidates = [
            {
                "candidate_id": "AD-0001",
                "priority": "high",
                "risk_hint": "low",
                "sample": original,
                "signals": ["domain"],
                "layer": "L3",
                "occurrence_count": 1,
                "anchors_truncated": False,
                "anchors": [{"original": original}],
            }
        ]

        records = verify.residual_records(residual_candidates, decisions)

        self.assertEqual(records, [])

    def test_machine_delete_evidence_prevents_keep_suppression(self) -> None:
        original = "站外更新提示：请访问 https://reader.example.com/update"
        for evidence_type in ("automatic_delete_gate", "family_similarity"):
            with self.subTest(evidence_type=evidence_type):
                records = verify.residual_records(
                    [
                        {
                            "candidate_id": "AD-0001",
                            "priority": "high",
                            "risk_hint": "low",
                            "occurrence_count": 1,
                            "anchors_truncated": False,
                            "anchors": [{"original": original}],
                        }
                    ],
                    [
                        {
                            "candidate_id": "AD-0042",
                            "verdict": "keep",
                            "anchors_truncated": False,
                            "anchor_ids": ["AN-1"],
                            "anchor_text_sha256s": [
                                hashlib.sha256(original.encode("utf-8")).hexdigest()
                            ],
                            "evidence": [{"type": evidence_type, "value": {}}],
                        }
                    ],
                )

                self.assertEqual(len(records), 1)
                self.assertEqual(
                    records[0]["matched_formal_verdicts"], {"AD-0042": "keep"}
                )
                overridden = {
                    "candidate_id": "AD-0042",
                    "verdict": "keep",
                    "anchors_truncated": False,
                    "anchor_ids": ["AN-1"],
                    "anchor_text_sha256s": [
                        hashlib.sha256(original.encode("utf-8")).hexdigest()
                    ],
                    "evidence": [{"type": evidence_type, "value": {}}],
                    "keep_basis": "plot_dependency",
                }
                self.assertEqual(
                    len(
                        verify.residual_records(
                            [
                                {
                                    "candidate_id": "AD-0001",
                                    "priority": "high",
                                    "risk_hint": "low",
                                    "occurrence_count": 1,
                                    "anchors_truncated": False,
                                    "anchors": [{"original": original}],
                                }
                            ],
                            [overridden],
                        )
                    ),
                    1,
                )

                source = {
                    "candidate_id": "AD-0042",
                    "candidate_fingerprint": hashlib.sha256(b"AD-0042").hexdigest(),
                    "anchors_truncated": False,
                    "occurrence_count": 1,
                    "anchors": [
                        {
                            "anchor_id": "AN-1",
                            "offset": 0,
                            "end": len(original),
                            "original": original,
                        }
                    ],
                }
                occurrences = ad_decision_policy.validate_candidate_occurrences(source)
                structured = {
                    **overridden,
                    "candidate_fingerprint": source["candidate_fingerprint"],
                    "occurrence_count": 1,
                    "keep_basis": {
                        "schema": ad_decision_policy.KEEP_BASIS_SCHEMA,
                        "type": "plot_dependency",
                        "reviewed_occurrences": occurrences,
                        "occurrence_coverage_sha256": (
                            ad_decision_policy.occurrence_coverage_sha256(occurrences)
                        ),
                        "note": "网址是案件线索的一部分。",
                    },
                }
                self.assertEqual(
                    verify.residual_records(
                        [
                            {
                                "candidate_id": "AD-0001",
                                "priority": "high",
                                "risk_hint": "low",
                                "occurrence_count": 1,
                                "anchors_truncated": False,
                                "anchors": [
                                    {
                                        "offset": 0,
                                        "end": len(original),
                                        "original": original,
                                    }
                                ],
                            }
                        ],
                        [structured],
                        source_candidates=[source],
                        operations=[],
                    ),
                    [],
                )

    def test_incomplete_or_under_counted_keep_does_not_suppress_residual(self) -> None:
        original = "请访问 reader.example.com"
        residual = {
            "candidate_id": "AD-0001",
            "priority": "high",
            "risk_hint": "low",
            "occurrence_count": 2,
            "anchors_truncated": False,
            "anchors": [{"original": original}, {"original": original}],
        }
        decision = {
            "candidate_id": "AD-0042",
            "verdict": "keep",
            "anchors_truncated": False,
            "anchor_ids": ["AN-1"],
            "anchor_text_sha256s": [
                hashlib.sha256(original.encode("utf-8")).hexdigest()
            ],
        }

        self.assertEqual(len(verify.residual_records([residual], [decision])), 1)

        complete_decision = {
            **decision,
            "anchor_ids": ["AN-1", "AN-2"],
            "anchor_text_sha256s": decision["anchor_text_sha256s"] * 2,
        }
        truncated_residual = {**residual, "anchors_truncated": True}
        self.assertEqual(
            len(verify.residual_records([truncated_residual], [complete_decision])),
            1,
        )

        complete_evidence = {
            **complete_decision,
            "evidence": [{"type": "direct_signal", "value": ["url"]}],
        }
        truncated_decision = {**complete_evidence, "anchors_truncated": True}
        self.assertEqual(
            len(verify.residual_records([residual], [truncated_decision])),
            1,
        )

    def test_keep_without_bound_evidence_does_not_suppress_residual(self) -> None:
        original = "reader.example.com"
        records = verify.residual_records(
            [
                {
                    "candidate_id": "AD-0001",
                    "priority": "high",
                    "risk_hint": "low",
                    "occurrence_count": 1,
                    "anchors_truncated": False,
                    "anchors": [{"original": original}],
                }
            ],
            [
                {
                    "candidate_id": "AD-0042",
                    "verdict": "keep",
                    "anchors_truncated": False,
                    "anchor_ids": ["AN-1"],
                    "anchor_text_sha256s": [
                        hashlib.sha256(original.encode("utf-8")).hexdigest()
                    ],
                }
            ],
        )

        self.assertEqual(len(records), 1)

    def test_residual_delete_is_mapped_back_to_formal_candidate_by_exact_anchor(self) -> None:
        decisions = [
            {
                "candidate_id": "AD-0042",
                "verdict": "delete",
                "anchors": [{"original": "欢迎访问 reader.example.com"}],
            }
        ]
        residual_candidates = [
            {
                "candidate_id": "AD-0001",
                "priority": "high",
                "risk_hint": "low",
                "sample": "欢迎访问 reader.example.com",
                "signals": ["domain"],
                "layer": "L3",
                "occurrence_count": 1,
                "anchors": [{"original": "欢迎访问 reader.example.com"}],
            }
        ]

        records = verify.residual_records(residual_candidates, decisions)

        self.assertEqual(records[0]["candidate_id"], "AD-0042")
        self.assertEqual(records[0]["matched_formal_candidate_ids"], ["AD-0042"])
        self.assertEqual(records[0]["matched_formal_verdicts"], {"AD-0042": "delete"})
        self.assertEqual(records[0]["scan_candidate_id"], "AD-0001")

    def test_partially_matched_keep_candidate_remains_a_blocker(self) -> None:
        reviewed = "剧情中的旧网址"
        records = verify.residual_records(
            [
                {
                    "candidate_id": "AD-0001",
                    "priority": "high",
                    "risk_hint": "low",
                    "occurrence_count": 2,
                    "anchors_truncated": False,
                    "anchors": [
                        {"original": reviewed},
                        {"original": "未审阅的新推广"},
                    ],
                }
            ],
            [
                {
                    "candidate_id": "AD-0042",
                    "verdict": "keep",
                    "anchors_truncated": False,
                    "anchor_ids": ["AN-1"],
                    "anchor_text_sha256s": [
                        hashlib.sha256(reviewed.encode("utf-8")).hexdigest()
                    ],
                }
            ],
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["matched_formal_candidate_ids"], ["AD-0042"])

    def test_medium_non_external_residual_is_not_a_review_blocker(self) -> None:
        records = verify.residual_records(
            [
                {
                    "candidate_id": "AD-0002",
                    "priority": "medium",
                    "risk_hint": "medium",
                    "sample": "正常的重复正文",
                    "anchors": [{"original": "正常的重复正文"}],
                }
            ],
            [],
        )

        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
