from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_decisions  # noqa: E402
import common  # noqa: E402
import export_outputs  # noqa: E402
import normalize_layout  # noqa: E402
import preprocess  # noqa: E402
import scan_identity  # noqa: E402
import verify  # noqa: E402
from tests.support_formal_ads import formalize_ads  # noqa: E402


class VerifyAttestationF3Tests(unittest.TestCase):
    def make_applied_workspace(self, root: Path) -> Path:
        source = root / "sample-a.txt"
        source.write_text("第一章 起点\n广告甲\n" + "正文甲。" * 20 + "\n", encoding="utf-8")
        workspace = preprocess.run(source)
        input_path = workspace / "versions/v1_preprocessed.txt"
        text = input_path.read_text(encoding="utf-8")
        start = text.index("广告甲")
        formalize_ads(
            workspace,
            [
                {
                    "candidate_id": "AD-0001",
                    "offset": start,
                    "original": "广告甲",
                    "review": {"reason": "测试广告"},
                }
            ],
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
        return workspace

    def run_verify(self, workspace: Path, *, skip: bool = False) -> dict:
        return verify.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "auto",
            "decisions/ads_decisions.jsonl",
            skip,
        )

    def manifest(self, workspace: Path) -> dict:
        return json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))

    def test_passed_attestation_binds_current_apply_run_and_head_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_applied_workspace(Path(directory))

            report = self.run_verify(workspace)
            manifest = self.manifest(workspace)
            apply_stage = manifest["stages"]["2_ads"]
            verify_stage = manifest["stages"]["6_verify"]
            attestation = report["attestation"]

            self.assertEqual(report["status"], "passed")
            self.assertEqual(verify_stage["status"], "passed")
            self.assertEqual(verify_stage["attestation"], attestation)
            self.assertEqual(attestation["apply_run_id"], apply_stage["active_run_id"])
            self.assertEqual(attestation["current_head"], manifest["current_head"])
            self.assertEqual(
                attestation["current_head_sha256"],
                manifest["artifacts"][manifest["current_head"]]["sha256"],
            )
            self.assertEqual(attestation["decision_sha256"], apply_stage["decision_sha256"])
            self.assertEqual(attestation["schema_version"], 3)
            for field in verify.PROVENANCE_IDENTITY_FIELDS:
                self.assertEqual(attestation[field], verify_stage[field])
            self.assertTrue(all(check["passed"] for check in attestation["checks"]))

    def test_skip_residual_scan_is_incomplete_and_never_attested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_applied_workspace(Path(directory))

            report = self.run_verify(workspace, skip=True)
            stage = self.manifest(workspace)["stages"]["6_verify"]

            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(stage["status"], "incomplete")
            self.assertNotIn("attestation", report)
            self.assertNotIn("attestation", stage)

    def test_unresolved_formal_decision_is_rejected_before_apply_or_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-uncertain.txt"
            source.write_text("第一章 起点\n这是普通正文。\n", encoding="utf-8")
            workspace = preprocess.run(source)
            text = (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8")
            original = "这是普通正文。"
            formalize_ads(
                workspace,
                [
                    {
                        "candidate_id": "AD-0001",
                        "offset": text.index(original),
                        "original": original,
                        "review": {
                            "reason": "上下文不足",
                            "blocking_reasons": ["需要复核"],
                        },
                    }
                ],
                verdict="uncertain",
            )
            source_before = source.read_bytes()
            v1_before = (workspace / "versions/v1_preprocessed.txt").read_bytes()
            manifest_before = (workspace / "manifest.json").read_bytes()

            with self.assertRaisesRegex(ValueError, "formal decisions contain uncertain"):
                apply_decisions.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "decisions/ads_decisions.jsonl",
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                )

            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual((workspace / "versions/v1_preprocessed.txt").read_bytes(), v1_before)
            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            self.assertFalse((workspace / "versions/v2_ads_removed.txt").exists())
            self.assertFalse((workspace / "report/apply_report.json").exists())
            self.assertFalse((workspace / "report/verify_report.json").exists())

    def test_historical_operations_cannot_replace_the_active_run_anchor_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_applied_workspace(Path(directory))
            manifest = self.manifest(workspace)
            apply_stage = manifest["stages"]["2_ads"]
            operations_path = workspace / "logs/operations.jsonl"
            historical = common.load_jsonl(operations_path)
            for item in historical:
                item["run_id"] = "historical-run"

            with common.WorkspaceTransaction(workspace) as transaction:
                common.write_jsonl(transaction.stage_path(operations_path), historical)
                transaction.commit(
                    {
                        "2_ads": (
                            "done",
                            {
                                **{
                                    key: apply_stage[key]
                                    for key in (
                                        "input",
                                        "decisions",
                                        "output",
                                        "input_sha256",
                                        "decision_sha256",
                                        "output_sha256",
                                        "expected_anchor_ids",
                                    )
                                },
                                "active_run_id": transaction.run_id,
                            },
                        )
                    }
                )

            report = self.run_verify(workspace)
            stage = self.manifest(workspace)["stages"]["6_verify"]

            self.assertEqual(report["status"], "blocked")
            self.assertEqual(stage["status"], "blocked")
            self.assertEqual(
                report["decision_accounting"]["missing_operation_anchor_ids"],
                apply_stage["expected_anchor_ids"],
            )
            self.assertNotIn("attestation", report)

    def test_chapter_identity_compares_titles_and_order_not_only_count(self) -> None:
        before = [
            {"index": 1, "title": "第一章 起点", "start_offset": 0, "end_offset": 20},
            {"index": 2, "title": "第二章 继续", "start_offset": 20, "end_offset": 40},
        ]
        after = [
            {"index": 1, "title": "第二章 继续", "start_offset": 0, "end_offset": 20},
            {"index": 2, "title": "第一章 起点", "start_offset": 20, "end_offset": 40},
        ]

        comparison = verify.compare_chapter_identity(before, after)

        self.assertFalse(comparison["passed"])
        self.assertEqual(comparison["before_titles"], ["第一章 起点", "第二章 继续"])
        self.assertEqual(comparison["after_titles"], ["第二章 继续", "第一章 起点"])

    def test_export_accepts_only_the_exact_passed_current_head_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_applied_workspace(root)
            verify_report = self.run_verify(workspace)

            report = export_outputs.run(workspace, "auto", None, root / "exports")

            self.assertEqual(
                report["verification"]["attestation_sha256"],
                export_outputs.require_export_attestation(
                    workspace,
                    workspace / "versions/v2_ads_removed.txt",
                )["attestation_sha256"],
            )
            self.assertEqual(
                report["verification"]["input_sha256"],
                verify_report["attestation"]["current_head_sha256"],
            )
            self.assertTrue(all(Path(path).is_file() for path in report["outputs"].values()))

    def test_head_change_after_verification_invalidates_export_without_creating_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_applied_workspace(root)
            self.run_verify(workspace)
            v2 = workspace / "versions/v2_ads_removed.txt"
            v5 = workspace / "versions/v5_layout_final.txt"
            with common.WorkspaceTransaction(workspace) as transaction:
                common.write_utf8(
                    transaction.stage_path(v5),
                    v2.read_text(encoding="utf-8"),
                )
                common.write_json(
                    transaction.stage_path(workspace / "report/layout_report.json"),
                    {"input": "versions/v2_ads_removed.txt", "output": "versions/v5_layout_final.txt"},
                )
                transaction.commit(
                    {
                        "5_layout": (
                            "done",
                            {
                                "input": "versions/v2_ads_removed.txt",
                                "output": "versions/v5_layout_final.txt",
                                "report": "report/layout_report.json",
                            },
                        )
                    }
                )
            output_root = root / "stale-exports"

            with self.assertRaisesRegex(ValueError, "verification|attestation|verify"):
                export_outputs.run(workspace, "auto", None, output_root)

            self.assertFalse(output_root.exists())

    def test_runtime_or_profile_drift_after_verification_blocks_export_before_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_applied_workspace(root)
            self.run_verify(workspace)

            scan_pack = scan_identity.build_scan_rule_pack("ads")
            draft_pack = scan_identity.build_draft_rule_pack()
            cases = (
                (
                    "scan-pack",
                    mock.patch.object(
                        scan_identity,
                        "build_scan_rule_pack",
                        return_value={**scan_pack, "schema_version": 999},
                    ),
                ),
                (
                    "draft-pack",
                    mock.patch.object(
                        scan_identity,
                        "build_draft_rule_pack",
                        return_value={**draft_pack, "schema_version": 999},
                    ),
                ),
            )
            for label, drift in cases:
                output_root = root / f"exports-{label}"
                with (
                    self.subTest(label=label),
                    drift,
                    self.assertRaisesRegex(ValueError, "current-runtime"),
                ):
                    export_outputs.run(workspace, "auto", None, output_root)
                self.assertFalse(output_root.exists())

            profile = workspace / "meta/book_profile.json"
            profile.write_text(
                json.dumps({"title": "后来出现的画像"}, ensure_ascii=False),
                encoding="utf-8",
            )
            output_root = root / "exports-profile"
            with self.assertRaisesRegex(ValueError, "current-runtime"):
                export_outputs.run(workspace, "auto", None, output_root)
            self.assertFalse(output_root.exists())

    def test_final_verification_replays_layout_and_attests_the_current_v5(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_applied_workspace(root)
            layout = normalize_layout.run(
                workspace,
                "versions/v2_ads_removed.txt",
                "versions/v5_layout_final.txt",
                None,
            )

            report = self.run_verify(workspace)
            manifest = self.manifest(workspace)
            layout_check = next(
                check for check in report["checks"] if check["name"] == "layout_replay"
            )

            self.assertEqual(report["status"], "passed")
            self.assertTrue(layout_check["passed"])
            self.assertEqual(report["apply_output"], "versions/v2_ads_removed.txt")
            self.assertEqual(report["after"], "versions/v5_layout_final.txt")
            self.assertEqual(
                report["attestation"]["current_head_sha256"],
                manifest["artifacts"]["versions/v5_layout_final.txt"]["sha256"],
            )
            self.assertEqual(layout["input_sha256"], manifest["stages"]["5_layout"]["input_sha256"])
            self.assertEqual(layout["output_sha256"], manifest["stages"]["5_layout"]["output_sha256"])

            exported = export_outputs.run(workspace, "auto", None, root / "exports-v5")
            self.assertEqual(
                exported["verification"]["input_sha256"],
                report["attestation"]["current_head_sha256"],
            )

    def test_forged_layout_output_cannot_pass_deterministic_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_applied_workspace(Path(directory))
            layout = normalize_layout.run(
                workspace,
                "versions/v2_ads_removed.txt",
                "versions/v5_layout_final.txt",
                None,
            )
            v5 = workspace / "versions/v5_layout_final.txt"
            forged = v5.read_text(encoding="utf-8") + "伪造正文\n"
            with common.WorkspaceTransaction(workspace) as transaction:
                common.write_utf8(transaction.stage_path(v5), forged)
                forged_report = {
                    **layout,
                    "output_sha256": common.sha256_file(transaction.stage_path(v5)),
                }
                common.write_json(
                    transaction.stage_path(workspace / "report/layout_report.json"),
                    forged_report,
                )
                transaction.commit(
                    {
                        "5_layout": (
                            "done",
                            {
                                **forged_report,
                                "report": "report/layout_report.json",
                                "active_run_id": transaction.run_id,
                            },
                        )
                    }
                )

            report = self.run_verify(workspace)
            layout_check = next(
                check for check in report["checks"] if check["name"] == "layout_replay"
            )

            self.assertEqual(report["status"], "blocked")
            self.assertFalse(layout_check["passed"])
            self.assertNotIn("attestation", report)

    def test_export_rejects_attestation_with_a_missing_required_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_applied_workspace(Path(directory))
            self.run_verify(workspace)
            manifest = self.manifest(workspace)
            for missing_name in ("layout_replay", "formal_uncertain"):
                forged = copy.deepcopy(manifest)
                attestation = forged["stages"]["6_verify"]["attestation"]
                attestation["checks"] = [
                    check
                    for check in attestation["checks"]
                    if check["name"] != missing_name
                ]

                with (
                    self.subTest(missing_name=missing_name),
                    mock.patch.object(export_outputs, "load_manifest", return_value=forged),
                    self.assertRaisesRegex(ValueError, "incomplete|blocking"),
                ):
                    export_outputs.require_export_attestation(
                        workspace,
                        workspace / manifest["current_head"],
                    )


if __name__ == "__main__":
    unittest.main()
