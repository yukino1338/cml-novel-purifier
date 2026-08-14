from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_decisions  # noqa: E402
import build_review_html  # noqa: E402
import common  # noqa: E402
import dry_run  # noqa: E402
import export_outputs  # noqa: E402
import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import normalize_layout  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_ads  # noqa: E402
import scan_blocked  # noqa: E402
import scan_titles  # noqa: E402
import verify  # noqa: E402


class CurrentRunReportingF3Tests(unittest.TestCase):
    def complete_clean_workspace(self, root: Path) -> tuple[Path, dict[str, object]]:
        source = root / "clean.txt"
        source.write_text("第一章 起点\n这是干净的匿名正文。\n", encoding="utf-8")
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
        common.write_jsonl(
            workspace / "logs/operations.jsonl",
            [{"run_id": "historical-run", "candidate_id": "old"}],
        )
        common.write_jsonl(
            workspace / "logs/anomalies.jsonl",
            [{"run_id": "historical-run", "candidate_id": "old", "message": "old"}],
        )
        apply_decisions.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "decisions/ads_decisions.jsonl",
            "versions/v2_ads_removed.txt",
            "2_ads",
        )
        scan_titles.run(workspace, "auto", "candidates/titles.jsonl")
        scan_blocked.run(workspace, "auto", "candidates/blocked.jsonl", 20)
        dry_report = dry_run.run(workspace)
        normalize_layout.run(workspace, "auto", "versions/v5_layout_final.txt", None)
        verify.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "auto",
            "decisions/ads_decisions.jsonl",
            False,
        )
        export_outputs.run(workspace, "auto", None, root / "exports")
        return workspace, dry_report

    def test_clean_zero_candidate_completion_ignores_history_and_stale_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, dry_report = self.complete_clean_workspace(Path(directory))

            completed = build_review_html.workspace_review(workspace, 80, 3)

            self.assertEqual(dry_report["status"], "complete")
            self.assertEqual(completed["workflow"]["key"], "completed")
            self.assertEqual(completed["summary"]["ads_candidates"], 0)
            self.assertEqual(completed["summary"]["formal_decisions"], 0)
            self.assertEqual(completed["summary"]["operations"], 0)
            self.assertEqual(completed["summary"]["anomalies"], 0)
            self.assertEqual(set(completed["rollback"]), {"all"})

            common.update_stages(
                workspace,
                {
                    "6_verify": ("pending", {"invalidated_by": "test"}),
                    "7_export": ("pending", {"invalidated_by": "test"}),
                },
            )
            pending = build_review_html.workspace_review(workspace, 80, 3)

            self.assertNotEqual(pending["workflow"]["key"], "completed")
            self.assertEqual(pending["reports"]["verify"], {})
            self.assertEqual(pending["reports"]["export"], {})

    def test_missing_declared_page_is_rejected_instead_of_reported_as_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ads.txt"
            source.write_text(
                "第一章 起点\n访问 https://reader.example.com 获取更新。\n",
                encoding="utf-8",
            )
            workspace = preprocess.run(source, encoding="utf-8")
            parse_structure.run(workspace)
            report = scan_ads.run(
                workspace,
                "versions/v1_preprocessed.txt",
                "candidates/ads.jsonl",
                12,
                1,
                20,
            )
            page = workspace / report["pages"]["manifest"][0]["file"]
            page.unlink()

            with self.assertRaises((common.WorkspaceIdentityError, ValueError)):
                build_review_html.workspace_review(workspace, 80, 3)

    def test_invalid_cleanwork_directory_is_not_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake.cleanwork"
            fake.mkdir()
            (fake / "versions").mkdir()

            self.assertFalse(build_review_html.is_workspace(fake))
            self.assertEqual(build_review_html.discover_workspaces([root], True), [])


if __name__ == "__main__":
    unittest.main()
