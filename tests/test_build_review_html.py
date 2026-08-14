from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_review_html as review  # noqa: E402


class ReviewPageF5Tests(unittest.TestCase):
    def test_report_only_modules_have_no_decision_or_draft_inputs(self) -> None:
        inputs = set(review.REVIEW_INPUT_PATHS.values())

        for module in ("titles", "blocked"):
            self.assertNotIn(f"decisions/{module}_decisions.jsonl", inputs)
            self.assertNotIn(f"decisions/{module}_decisions.draft.jsonl", inputs)

        self.assertEqual(review.ADS_DECISIONS, "decisions/ads_decisions.jsonl")
        self.assertEqual(
            review.ADS_DRAFT_DECISIONS,
            "decisions/ads_decisions.draft.jsonl",
        )

    def test_current_logs_ignore_report_only_runs_and_non_ad_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "logs").mkdir()
            (workspace / "logs" / "operations.jsonl").write_text(
                "".join(
                    (
                        '{"run_id":"ads-run","module":"ads","candidate_id":"AD-1"}\n',
                        '{"run_id":"ads-run","module":"titles","candidate_id":"TITLE-1"}\n',
                        '{"run_id":"title-run","module":"titles","candidate_id":"TITLE-2"}\n',
                    )
                ),
                encoding="utf-8",
            )
            (workspace / "logs" / "anomalies.jsonl").write_text(
                '{"run_id":"ads-run","message":"current"}\n'
                '{"run_id":"title-run","message":"legacy"}\n',
                encoding="utf-8",
            )
            manifest = {
                "current_head": "versions/v2_ads_removed.txt",
                "artifacts": {},
                "stages": {
                    "2_ads": {"status": "done", "active_run_id": "ads-run"},
                    "3_titles": {"status": "done", "active_run_id": "title-run"},
                    "4_blocked_words": {"status": "done", "active_run_id": "blocked-run"},
                },
            }

            operations, anomalies = review.current_logs(workspace, manifest)

        self.assertEqual([item["candidate_id"] for item in operations], ["AD-1"])
        self.assertEqual([item["message"] for item in anomalies], ["current"])

    def candidate(self) -> dict[str, object]:
        return {
            "candidate_id": "AD-0001",
            "layer": "L3",
            "risk_hint": "low",
            "signals": ["reader_site"],
            "sample": "欢迎访问示例阅读站",
            "anchors": [
                {
                    "offset": 12,
                    "original": "欢迎访问示例阅读站",
                    "chapter": {"index": 1, "title": "第一章"},
                }
            ],
        }

    def empty_modules(self) -> dict[str, dict[str, object]]:
        return {
            module: {"review_items": []}
            for module in review.MODULES
        }

    def test_family_metadata_keeps_a_human_visible_label_and_evidence(self) -> None:
        family = review.family_metadata(
            self.candidate(),
            {
                "cluster_id": "site:domain:example.com|intent:visit",
                "family_signature": {
                    "site_entities": ["domain:example.com"],
                    "intents": ["read", "visit"],
                },
                "evidence": [{"type": "site_entity", "value": "domain:example.com"}],
                "promoted_from": ["AD-0000"],
            },
            None,
        )

        self.assertEqual(family["cluster_id"], "site:domain:example.com|intent:visit")
        self.assertTrue(family["family_label"])
        self.assertEqual(family["promoted_from"], ["AD-0000"])

    def test_only_formal_uncertainty_or_current_evidence_enters_exception_review(self) -> None:
        candidate = self.candidate()
        draft_only = review.review_candidate(
            Path("C:/books/example.cleanwork"),
            "ads",
            candidate,
            {"candidate_id": "AD-0001", "verdict": "uncertain"},
            None,
            None,
        )
        modules = self.empty_modules()
        modules["ads"]["review_items"] = [draft_only]

        draft_summary = review.annotate_review_items(modules, {}, [])

        self.assertEqual(draft_summary["review_candidate_count"], 0)
        self.assertFalse(draft_only["needs_review"])

        formal = review.review_candidate(
            Path("C:/books/example.cleanwork"),
            "ads",
            candidate,
            {"candidate_id": "AD-0001", "verdict": "delete"},
            {"candidate_id": "AD-0001", "verdict": "uncertain"},
            None,
        )
        modules["ads"]["review_items"] = [formal]
        formal_summary = review.annotate_review_items(modules, {}, [])

        self.assertEqual(formal_summary["formal_uncertain"], 1)
        self.assertEqual(formal_summary["review_candidate_count"], 1)
        self.assertTrue(formal["needs_review"])

    def test_residual_issue_count_ignores_scan_volume_metrics(self) -> None:
        residual_report = {
            "residuals": [{"candidate_id": "AD-0001", "message": "still present"}],
            "residual_scan": {
                "block_count": 78_763,
                "candidate_count": 27,
                "strong_candidate_count": 1,
            },
        }
        clean_report = {
            "warnings": [],
            "residuals": [],
            "residual_scan": {
                "block_count": 78_763,
                "candidate_count": 27,
                "strong_candidate_count": 0,
            },
        }

        self.assertGreater(
            review.report_issue_count(residual_report, {"AD-0001"}),
            0,
        )
        self.assertEqual(review.report_issue_count(clean_report, set()), 0)

    def test_zero_anomaly_metric_uses_resolved_state_class(self) -> None:
        page = review.render_metric_cards({"anomalies": 0}, ("anomalies",))

        self.assertIn("metric-anomalies metric-zero", page)


if __name__ == "__main__":
    unittest.main()
