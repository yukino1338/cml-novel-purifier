from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_decisions  # noqa: E402
import dry_run  # noqa: E402
import preprocess  # noqa: E402
import scan_blocked  # noqa: E402
import scan_identity  # noqa: E402
import scan_titles  # noqa: E402
from tests.support_formal_ads import formalize_ads  # noqa: E402


class ApplyDecisionsF5Tests(unittest.TestCase):
    def test_report_only_scans_rebind_structure_after_ad_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-post-apply.txt"
            source.write_text(
                "第一章 回家\n正文从这里开始。\n站外广告\n故事继续。\n",
                encoding="utf-8",
            )
            workspace = preprocess.run(source)
            v1_path = workspace / "versions/v1_preprocessed.txt"
            v1_text = v1_path.read_text(encoding="utf-8")
            formalize_ads(
                workspace,
                [
                    {
                        "candidate_id": "AD-1",
                        "offset": v1_text.index("站外广告"),
                        "original": "站外广告",
                    }
                ],
                verdict="delete",
                action="delete",
            )
            v1_sha256 = hashlib.sha256(v1_path.read_bytes()).hexdigest()

            formal_manifest = json.loads(
                (workspace / "manifest.json").read_text(encoding="utf-8")
            )
            expected_identity = {
                key: formal_manifest["stages"]["2_ads"][key]
                for key in apply_decisions.FORMAL_IDENTITY_FIELDS
            }
            summary = apply_decisions.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )

            v2_path = workspace / "versions/v2_ads_removed.txt"
            v2_sha256 = hashlib.sha256(v2_path.read_bytes()).hexdigest()
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            apply_report = json.loads(
                (workspace / "report/apply_report.json").read_text(encoding="utf-8")
            )
            for key, value in expected_identity.items():
                self.assertEqual(summary[key], value)
                self.assertEqual(apply_report[key], value)
                self.assertEqual(manifest["stages"]["2_ads"][key], value)
            self.assertEqual(manifest["current_head"], "versions/v2_ads_removed.txt")
            self.assertNotEqual(v2_sha256, v1_sha256)

            reports = (
                scan_titles.run(workspace, "auto", "candidates/titles.jsonl"),
                scan_blocked.run(
                    workspace,
                    "auto",
                    "candidates/blocked.jsonl",
                    100,
                ),
            )
            for report in reports:
                with self.subTest(scanner=report["scanner"]):
                    self.assertEqual(
                        Path(report["input"]).as_posix(),
                        "versions/v2_ads_removed.txt",
                    )
                    self.assertEqual(report["input_sha256"], v2_sha256)
                    structure_path = workspace / report["structure"]
                    self.assertTrue(structure_path.is_file())
                    structure = json.loads(structure_path.read_text(encoding="utf-8"))
                    self.assertEqual(structure["input_sha256"], v2_sha256)
                    candidates = [
                        json.loads(line)
                        for line in (workspace / report["output"])
                        .read_text(encoding="utf-8")
                        .splitlines()
                        if line
                    ]
                    scan_identity.validate_scan_identity(
                        workspace,
                        report,
                        candidates,
                    )

            self.assertEqual(dry_run.run(workspace)["status"], "complete")

    def test_uncertain_formal_decision_blocks_apply_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-uncertain.txt"
            source.write_text("正文\n疑似广告\n正文\n", encoding="utf-8")
            workspace = preprocess.run(source)
            input_path = workspace / "versions/v1_preprocessed.txt"
            text = input_path.read_text(encoding="utf-8")
            formalize_ads(
                workspace,
                [{"candidate_id": "AD-1", "offset": text.index("疑似广告"), "original": "疑似广告"}],
                verdict="uncertain",
            )
            manifest_before = (workspace / "manifest.json").read_bytes()

            with self.assertRaisesRegex(ValueError, "uncertain"):
                apply_decisions.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "decisions/ads_decisions.jsonl",
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                )

            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            self.assertFalse((workspace / "versions/v2_ads_removed.txt").exists())

    def test_applying_ads_invalidates_downstream_stage_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_text("正文\n广告\n正文\n", encoding="utf-8")
            workspace = preprocess.run(source)
            formalize_ads(
                workspace,
                [{"candidate_id": "AD-1", "offset": 3, "original": "广告"}],
                verdict="delete",
                action="delete",
            )

            apply_decisions.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )

            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stages"]["2_ads"]["status"], "done")
            artifact = manifest["artifacts"]["versions/v2_ads_removed.txt"]
            self.assertEqual(artifact["parent_path"], "versions/v1_preprocessed.txt")
            self.assertEqual(
                artifact["decision_sha256"],
                hashlib.sha256((workspace / "decisions/ads_decisions.jsonl").read_bytes()).hexdigest(),
            )
            self.assertEqual(manifest["current_head"], "versions/v2_ads_removed.txt")
            for stage in ("5_layout", "6_verify", "7_export"):
                self.assertEqual(manifest["stages"][stage]["status"], "pending")
                self.assertEqual(manifest["stages"][stage]["invalidated_by"], "2_ads")


if __name__ == "__main__":
    unittest.main()
