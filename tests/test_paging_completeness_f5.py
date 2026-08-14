from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import dry_run  # noqa: E402
import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_ads  # noqa: E402
import scan_blocked  # noqa: E402
import scan_identity  # noqa: E402
import scan_titles  # noqa: E402


def candidate(index: int, *, truncated: bool = False) -> dict[str, object]:
    sample = f"promotion-{index}"
    return {
        "candidate_id": f"AD-{index:04d}",
        "layer": "L3" if index % 2 else "L1",
        "priority": "high" if index % 2 else "medium",
        "risk_hint": "low" if index % 2 else "medium",
        "sample": sample,
        "signals": ["url"] if index % 2 else [],
        "signal_strength": "strong" if index % 2 else "none",
        "occurrence_count": 1,
        "anchors_truncated": truncated,
        "anchors": [
            {
                "offset": index,
                "end": index + len(sample),
                "original": sample,
                "prefix": "",
                "suffix": "",
            }
        ],
        "suggested_decision": {
            "candidate_id": f"AD-{index:04d}",
            "verdict": "uncertain",
        },
    }


class PagingCompletenessF5Tests(unittest.TestCase):
    def test_page_size_changes_only_page_boundaries(self) -> None:
        pool = [candidate(index) for index in range(1, 7)]
        performance = {"timings_seconds": {}, "signal_metrics": {}}
        with mock.patch.object(
            scan_ads,
            "build_candidate_pool",
            side_effect=lambda *_args: (copy.deepcopy(pool), 6, copy.deepcopy(performance)),
        ):
            single, single_summary = scan_ads.scan_candidates("body", max_candidates=1)
            multi, multi_summary = scan_ads.scan_candidates("body", max_candidates=4)

        for candidates in (single, multi):
            scan_identity.attach_candidate_fingerprints(candidates)
            scan_identity.attach_anchor_ids(candidates)
        self.assertEqual(
            [item["candidate_fingerprint"] for item in single],
            [item["candidate_fingerprint"] for item in multi],
        )
        self.assertEqual(
            scan_identity.candidate_set_sha256(single),
            scan_identity.candidate_set_sha256(multi),
        )
        self.assertEqual(single_summary["page_count"], 6)
        self.assertEqual(multi_summary["page_count"], 2)

    def test_duplicate_candidate_ids_and_fingerprints_are_rejected(self) -> None:
        duplicate_fingerprint = [candidate(1), candidate(1)]
        duplicate_fingerprint[1]["candidate_id"] = "AD-9999"
        duplicate_fingerprint[1]["suggested_decision"]["candidate_id"] = "AD-9999"  # type: ignore[index]
        duplicate_fingerprint[0]["anchors"] = []
        duplicate_fingerprint[1]["anchors"] = []
        scan_identity.attach_candidate_fingerprints(duplicate_fingerprint)
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "fingerprint"):
            scan_identity.validate_candidate_set(duplicate_fingerprint)

        duplicate_id = [candidate(1), candidate(2)]
        duplicate_id[1]["candidate_id"] = "AD-0001"
        scan_identity.attach_candidate_fingerprints(duplicate_id)
        scan_identity.attach_anchor_ids(duplicate_id)
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "candidate ID"):
            scan_identity.validate_candidate_set(duplicate_id)

    def make_scanned_workspace(self, root: Path) -> Path:
        source = root / "anonymous.txt"
        source.write_text("第一章 起点\n正文。\n", encoding="utf-8")
        workspace = preprocess.run(source)
        parse_structure.run(workspace)
        candidates = [candidate(1), candidate(2, truncated=True), candidate(3)]
        summary = {
            "candidate_count": 1,
            "first_page_count": 1,
            "total_candidate_count": 3,
            "page_size": 1,
            "page_count": 3,
            "performance": {"timings_seconds": {}},
        }
        with mock.patch.object(scan_ads, "scan_candidates", return_value=(candidates, summary)):
            scan_ads.run(
                workspace,
                "versions/v1_preprocessed.txt",
                "candidates/ads.jsonl",
                12,
                1,
                20,
            )
        scan_titles.run(workspace, "auto", "candidates/titles.jsonl")
        scan_blocked.run(workspace, "auto", "candidates/blocked.jsonl", 20)
        for module in ("ads", "titles", "blocked"):
            common.write_jsonl(workspace / f"decisions/{module}_decisions.jsonl", [])
        return workspace

    def test_dry_run_consumes_complete_manifest_pages_and_counts_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_scanned_workspace(Path(directory))
            report = dry_run.run(workspace)

            ads = report["modules"]["ads"]
            self.assertEqual(ads["candidate_count"], 3)
            self.assertEqual(ads["anchor_count"], 3)
            self.assertEqual(ads["truncated_candidate_count"], 1)
            self.assertEqual(ads["candidate_file"], "candidates/ads_pages")

    def test_dry_run_rejects_a_missing_declared_page_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_scanned_workspace(Path(directory))
            (workspace / "candidates/ads_pages/ads_page_002.jsonl").unlink()

            with self.assertRaises((common.WorkspaceIdentityError, scan_identity.ScanIdentityError)):
                dry_run.run(workspace)
            self.assertFalse((workspace / "report/dry_run_report.json").exists())

    def test_report_only_scan_caps_keep_dry_run_pending_without_text_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "anonymous.txt"
            source.write_text(
                "\n".join(f"正文提到第{index + 1}章内容。" for index in range(60))
                + "\n甲*乙。丙*丁。\n",
                encoding="utf-8",
            )
            workspace = preprocess.run(source, encoding="utf-8")
            parse_structure.run(workspace)
            scan_ads.run(
                workspace,
                "versions/v1_preprocessed.txt",
                "candidates/ads.jsonl",
                12,
                20,
                20,
            )
            common.write_json(workspace / "meta/book_profile.json", {})
            make_ad_decisions.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_decisions.draft.jsonl",
                "meta/book_profile.json",
                True,
            )
            common.write_jsonl(workspace / "decisions/ads_agent_reviews.jsonl", [])
            finalize_ad_decisions.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_agent_reviews.jsonl",
                "decisions/ads_decisions.draft.jsonl",
                "decisions/ads_decisions.jsonl",
            )
            current_head = common.resolve_current_head(workspace)
            protected_before = {
                path: path.read_bytes()
                for path in (
                    source,
                    workspace / "versions/v0_original.txt",
                    current_head,
                )
            }

            titles = scan_titles.run(workspace, "auto", "candidates/titles.jsonl")
            blocked = scan_blocked.run(
                workspace,
                "auto",
                "candidates/blocked.jsonl",
                1,
            )
            report = dry_run.run(workspace)

            self.assertGreater(titles["summary"]["suppressed_report_only_count"], 0)
            self.assertTrue(blocked["summary"]["max_candidates_reached"])
            self.assertEqual(report["modules"]["titles"]["status"], "pending")
            self.assertEqual(report["modules"]["blocked"]["status"], "pending")
            self.assertEqual(report["status"], "pending")
            for path, content in protected_before.items():
                self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
