from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_decisions  # noqa: E402
import common  # noqa: E402
import export_outputs  # noqa: E402
import preprocess  # noqa: E402
import verify  # noqa: E402
from tests.support_formal_ads import formalize_clean_ads  # noqa: E402


class ExportBatchGateF3Tests(unittest.TestCase):
    def make_verified_workspace(self, root: Path, name: str) -> Path:
        source = root / f"{name}.txt"
        source.write_text("第一章 起点\n这是匿名正文。\n", encoding="utf-8")
        workspace = preprocess.run(source, encoding="utf-8")
        formalize_clean_ads(workspace)
        apply_decisions.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "decisions/ads_decisions.jsonl",
            "versions/v2_ads_removed.txt",
            "2_ads",
        )
        report = verify.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "auto",
            "decisions/ads_decisions.jsonl",
            False,
        )
        self.assertEqual(report["status"], "passed")
        return workspace

    def test_partial_batch_records_each_book_without_exporting_the_failed_book(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            passed = self.make_verified_workspace(root, "passed")
            failed = self.make_verified_workspace(root, "failed")
            common.update_stage(failed, "6_verify", "pending")

            report = export_outputs.run_batch(
                [passed, failed],
                "auto",
                None,
                root / "exports",
                requested_formats=("epub", "txt"),
            )

            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["success_count"], 1)
            self.assertEqual(report["failure_count"], 1)
            self.assertEqual([item["status"] for item in report["items"]], ["passed", "failed"])
            self.assertEqual(report["items"][1]["phase"], "preflight")
            self.assertNotIn("output_dir", report["items"][1])
            self.assertTrue(Path(report["items"][0]["output_dir_abs"]).is_dir())
            self.assertEqual(report["items"][0]["requested_formats"], ["txt", "epub"])
            self.assertEqual(report["items"][0]["produced_formats"], ["txt", "epub"])
            self.assertEqual(report["requested_formats"], ["txt", "epub"])
            self.assertEqual(
                json.loads((failed / "manifest.json").read_text(encoding="utf-8"))["stages"][
                    "7_export"
                ]["status"],
                "pending",
            )

    def test_all_failed_batch_writes_only_a_failed_batch_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspaces = [
                self.make_verified_workspace(root, "failed-a"),
                self.make_verified_workspace(root, "failed-b"),
            ]
            for workspace in workspaces:
                common.update_stage(workspace, "6_verify", "pending")

            report = export_outputs.run_batch(
                workspaces,
                "auto",
                None,
                root / "exports",
            )

            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["success_count"], 0)
            self.assertEqual(report["failure_count"], 2)
            batch_dir = Path(report["output_dir_abs"])
            self.assertEqual(
                [path.name for path in batch_dir.iterdir()],
                ["batch_export_report.json"],
            )


if __name__ == "__main__":
    unittest.main()
