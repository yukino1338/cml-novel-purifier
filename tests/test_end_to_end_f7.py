from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))

import apply_decisions  # noqa: E402
import build_review_html  # noqa: E402
import common  # noqa: E402
import export_outputs  # noqa: E402
import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import normalize_layout  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_ads  # noqa: E402
import scan_identity  # noqa: E402
import verify  # noqa: E402


AD_ONE = "站外更新提示：请访问 https://reader.example.com/update 获取后续内容。"
AD_TWO = "下载提示：请访问 https://reader.example.com/file 获取匿名文件。"


def delete_review_shape(candidate: dict[str, Any]) -> dict[str, str]:
    edit_plan = candidate.get("edit_plan")
    if isinstance(edit_plan, dict) and isinstance(edit_plan.get("edit_plan_id"), str):
        return {
            "splice_strategy": "exact_segment",
            "edit_plan_id": edit_plan["edit_plan_id"],
        }
    return {"splice_strategy": "remove_paragraph"}


def anonymous_novel(*, body_lines: int = 60, ads: tuple[str, ...] = (AD_ONE,)) -> str:
    lines = [
        f"人物甲记录匿名场景{index:03d}，并继续观察装置运行。"
        for index in range(1, body_lines + 1)
    ]
    first = lines[: len(lines) // 2]
    second = lines[len(lines) // 2 :]
    for index, ad in enumerate(ads):
        target = first if index % 2 == 0 else second
        target.insert(max(1, len(target) // 2), ad)
    return "\n".join(["第一章 起点", *first, "第二章 继续", *second]) + "\n"


class EndToEndSupport(unittest.TestCase):
    maxDiff = None

    def load_candidates(self, workspace: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
        return scan_identity.load_validated_pages(workspace, report)

    def review_and_apply_ads(
        self,
        workspace: Path,
        *,
        delete_originals: set[str],
        uncertain_originals: set[str] | None = None,
        page_size: int = 1,
        parse_first: bool,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        uncertain_originals = uncertain_originals or set()
        if parse_first:
            parse_structure.run(workspace)
        scan_report = scan_ads.run(
            workspace,
            "versions/v1_preprocessed.txt",
            "candidates/ads.jsonl",
            12,
            page_size,
            120,
        )
        candidates = self.load_candidates(workspace, scan_report)
        common.write_json(workspace / "meta/book_profile.json", {})
        make_ad_decisions.run(
            workspace,
            "candidates/ads_pages",
            "decisions/ads_decisions.draft.jsonl",
            "meta/book_profile.json",
            True,
        )

        reviews: list[dict[str, Any]] = []
        for candidate in candidates:
            originals = {
                str(anchor.get("original") or "")
                for anchor in candidate.get("anchors", [])
                if isinstance(anchor, dict)
            }
            if originals & delete_originals:
                verdict = "delete"
            elif originals & uncertain_originals:
                verdict = "uncertain"
            else:
                verdict = "keep"
            review: dict[str, Any] = {
                "scan_id": scan_report["scan_id"],
                "candidate_id": candidate["candidate_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "verdict": verdict,
                "confidence": 0.99,
                "reason": "fixed anonymous Agent review",
                "risk": "low" if verdict == "delete" else "medium",
            }
            if verdict == "delete":
                review.update(
                    {
                        "action": "delete",
                        **delete_review_shape(candidate),
                    }
                )
            elif verdict == "uncertain":
                review["blocking_reasons"] = ["requires additional context"]
            reviews.append(review)
        common.write_jsonl(workspace / "decisions/ads_agent_reviews.jsonl", reviews)
        finalize_ad_decisions.run(
            workspace,
            "candidates/ads_pages",
            "decisions/ads_agent_reviews.jsonl",
            "decisions/ads_decisions.draft.jsonl",
            "decisions/ads_decisions.jsonl",
        )
        if any(review["verdict"] == "uncertain" for review in reviews):
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
            return scan_report, candidates, None
        apply_report = apply_decisions.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "decisions/ads_decisions.jsonl",
            "versions/v2_ads_removed.txt",
            "2_ads",
        )
        return scan_report, candidates, apply_report

    def prepare_applied_workspace(
        self,
        root: Path,
        name: str,
        text: str,
        *,
        delete_originals: set[str],
        uncertain_originals: set[str] | None = None,
        page_size: int = 1,
    ) -> dict[str, Any]:
        source = root / f"{name}.txt"
        source.write_text(text, encoding="utf-8", newline="")
        source_sha256 = common.sha256_file(source)
        workspace = preprocess.run(source, encoding="utf-8")
        self.assertEqual(common.sha256_file(source), source_sha256)
        self.assertEqual(common.sha256_file(workspace / "versions/v0_original.txt"), source_sha256)

        scan_report, candidates, apply_report = self.review_and_apply_ads(
            workspace,
            delete_originals=delete_originals,
            uncertain_originals=uncertain_originals,
            page_size=page_size,
            parse_first=True,
        )
        self.assertEqual(common.sha256_file(source), source_sha256)
        self.assertEqual(common.sha256_file(workspace / "versions/v0_original.txt"), source_sha256)
        return {
            "source": source,
            "source_sha256": source_sha256,
            "workspace": workspace,
            "scan": scan_report,
            "candidates": candidates,
            "apply": apply_report,
        }

    def finish_workspace(
        self,
        root: Path,
        prepared: dict[str, Any],
        *,
        export: bool,
        review: bool,
    ) -> dict[str, Any]:
        workspace = prepared["workspace"]
        layout_report = normalize_layout.run(
            workspace,
            "auto",
            "versions/v5_layout_final.txt",
            None,
        )
        verify_report = verify.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "auto",
            "decisions/ads_decisions.jsonl",
            False,
        )
        export_report = (
            export_outputs.run(workspace, "auto", None, root / f"{prepared['source'].stem}-exports")
            if export
            else None
        )
        review_report = (
            build_review_html.run([workspace], None, False, 80, 3)
            if review
            else None
        )
        return {
            **prepared,
            "layout": layout_report,
            "verify": verify_report,
            "export": export_report,
            "review": review_report,
        }

    def cli(self, script: str, *args: object) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), *(str(arg) for arg in args)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )

    def assert_cli_success(
        self,
        script: str,
        *args: object,
    ) -> subprocess.CompletedProcess[str]:
        process = self.cli(script, *args)
        self.assertEqual(
            process.returncode,
            0,
            f"{script} failed\nstdout:\n{process.stdout}\nstderr:\n{process.stderr}",
        )
        return process

    def manifest(self, workspace: Path) -> dict[str, Any]:
        return json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))

    def verify_cli(
        self,
        workspace: Path,
        *extra: str,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        process = self.cli("verify.py", workspace, *extra)
        self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
        return process, json.loads(process.stdout)

    def assert_blocked_delivery_and_review(
        self,
        root: Path,
        workspace: Path,
        case: str,
        *,
        expected_workflow: str = "needs-review",
    ) -> dict[str, Any]:
        output_root = root / f"{case}-exports"
        process = self.cli(
            "export_outputs.py",
            workspace,
            "--output-root",
            output_root,
        )
        self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertFalse(output_root.exists())
        review = build_review_html.run([workspace], None, False, 80, 3)
        data = json.loads(Path(review["data"]).read_text(encoding="utf-8"))
        item = data["workspaces"][0]
        self.assertEqual(item["workflow"]["key"], expected_workflow)
        self.assertNotEqual(item["workflow"]["key"], "completed")
        html_text = Path(review["html"]).read_text(encoding="utf-8")
        self.assertIn(item["workflow"]["label"], html_text)
        return item

    def forge_layout(self, workspace: Path, transform: Any) -> None:
        output = workspace / "versions/v5_layout_final.txt"
        report_path = workspace / "report/layout_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        forged_text = transform(output.read_text(encoding="utf-8"))
        with common.WorkspaceTransaction(workspace) as transaction:
            staged_output = transaction.stage_path(output)
            common.write_utf8(staged_output, forged_text)
            forged_report = {
                **report,
                "output_sha256": common.sha256_file(staged_output),
                "active_run_id": transaction.run_id,
            }
            common.write_json(transaction.stage_path(report_path), forged_report)
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

    def stale_active_operation_anchor(self, workspace: Path) -> None:
        manifest = self.manifest(workspace)
        stage = manifest["stages"]["2_ads"]
        active_run = stage["active_run_id"]
        operations_path = workspace / "logs/operations.jsonl"
        operations = common.load_jsonl(operations_path)
        changed = []
        with common.WorkspaceTransaction(workspace) as transaction:
            for index, operation in enumerate(operations):
                item = dict(operation)
                if item.get("run_id") == active_run:
                    item["run_id"] = transaction.run_id
                    item["anchor_id"] = f"stale-{item['anchor_id']}"
                    changed.append(item["anchor_id"])
                operations[index] = item
            self.assertTrue(changed)
            common.write_jsonl(transaction.stage_path(operations_path), operations)
            transaction.commit(
                {
                    "2_ads": (
                        "done",
                        {
                            **{
                                key: stage[key]
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
                            "operation_anchor_ids": changed,
                        },
                    )
                }
            )


class FullPipelineF7Tests(EndToEndSupport):
    def test_real_subprocess_cli_happy_path(self) -> None:
        text = anonymous_novel()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "cli-happy.txt"
            workspace = root / "cli-happy.txt.cleanwork"
            exports = root / "cli-happy-exports"
            source.write_text(text, encoding="utf-8", newline="")
            source_sha256 = common.sha256_file(source)

            self.assert_cli_success(
                "preprocess.py",
                source,
                "--workspace",
                workspace,
                "--encoding",
                "utf-8",
            )
            self.assert_cli_success("parse_structure.py", workspace)
            self.assert_cli_success("scan_ads.py", workspace)
            self.assertEqual(
                self.manifest(workspace)["stages"]["2_ads"]["status"],
                "candidates_ready",
            )

            self.assert_cli_success("make_ad_decisions.py", workspace)
            self.assertEqual(
                self.manifest(workspace)["stages"]["2_ads"]["status"],
                "draft_decisions_ready",
            )

            scan_report = json.loads(
                (workspace / "report/ads_scan_report.json").read_text(encoding="utf-8")
            )
            candidates = self.load_candidates(workspace, scan_report)
            reviews: list[dict[str, Any]] = []
            delete_count = 0
            for candidate in candidates:
                originals = {
                    str(anchor.get("original") or "")
                    for anchor in candidate.get("anchors", [])
                    if isinstance(anchor, dict)
                }
                verdict = "delete" if any(AD_ONE in value for value in originals) else "keep"
                review: dict[str, Any] = {
                    "scan_id": scan_report["scan_id"],
                    "candidate_id": candidate["candidate_id"],
                    "candidate_fingerprint": candidate["candidate_fingerprint"],
                    "verdict": verdict,
                    "confidence": 0.99,
                    "reason": "subprocess CLI happy-path review",
                    "risk": "low" if verdict == "delete" else "medium",
                }
                if verdict == "delete":
                    delete_count += 1
                    review.update(
                        {
                            "action": "delete",
                            **delete_review_shape(candidate),
                        }
                    )
                reviews.append(review)
            self.assertTrue(candidates)
            self.assertGreater(delete_count, 0)
            common.write_jsonl(
                workspace / "decisions/ads_agent_reviews.jsonl",
                reviews,
            )

            self.assert_cli_success("finalize_ad_decisions.py", workspace)
            self.assertEqual(
                self.manifest(workspace)["stages"]["2_ads"]["status"],
                "formal_decisions_ready",
            )
            self.assert_cli_success(
                "apply_decisions.py",
                "--workspace",
                workspace,
                "--module",
                "ads",
                "--input",
                "versions/v1_preprocessed.txt",
                "--decisions",
                "decisions/ads_decisions.jsonl",
                "--output",
                "versions/v2_ads_removed.txt",
                "--stage",
                "2_ads",
            )
            self.assert_cli_success("normalize_layout.py", workspace)
            verify_process = self.assert_cli_success("verify.py", workspace)
            export_process = self.assert_cli_success(
                "export_outputs.py",
                workspace,
                "--output-root",
                exports,
            )
            review_process = self.assert_cli_success("build_review_html.py", workspace)

            verify_summary = json.loads(verify_process.stdout)
            export_report = json.loads(export_process.stdout)
            review_report = json.loads(review_process.stdout)
            manifest = self.manifest(workspace)
            self.assertEqual(verify_summary["status"], "passed")
            self.assertEqual(export_report["status"], "passed")
            self.assertNotIn(
                AD_ONE,
                (workspace / "versions/v2_ads_removed.txt").read_text(encoding="utf-8"),
            )
            self.assertTrue(
                all(Path(path).is_file() for path in export_report["outputs"].values())
            )
            self.assertTrue(Path(review_report["html"]).is_file())
            self.assertEqual(common.sha256_file(source), source_sha256)
            self.assertEqual(
                common.sha256_file(workspace / "versions/v0_original.txt"),
                source_sha256,
            )
            self.assertEqual(manifest["stages"]["0_preprocess"]["status"], "done")
            self.assertEqual(manifest["stages"]["1_parse_structure"]["status"], "done")
            self.assertEqual(manifest["stages"]["2_ads"]["status"], "done")
            self.assertEqual(manifest["stages"]["5_layout"]["status"], "done")
            self.assertEqual(manifest["stages"]["6_verify"]["status"], "passed")
            self.assertEqual(manifest["stages"]["7_export"]["status"], "done")

    def test_long_line_external_signal_is_scanned_and_blocks_unresolved_delivery(self) -> None:
        long_line = (
            "人物继续整理现场记录并核对每一项细节。" * 30
            + "请访问 https://reader.example.com/update 获取更新。"
        )
        text = f"第一章 起点\n{long_line}\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = self.prepare_applied_workspace(
                root,
                "long-line-external-signal",
                text,
                delete_originals=set(),
                uncertain_originals={long_line},
            )

            self.assertTrue(prepared["candidates"])
            self.assertIsNone(prepared["apply"])
            self.assertFalse(
                (prepared["workspace"] / "versions/v2_ads_removed.txt").exists()
            )
            item = self.assert_blocked_delivery_and_review(
                root,
                prepared["workspace"],
                "long-line-external-signal",
            )
            self.assertGreater(item["review_summary"]["formal_uncertain"], 0)

    def test_verify_blocks_when_formal_provenance_is_tampered_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = self.prepare_applied_workspace(
                root,
                "formal-provenance-tamper",
                anonymous_novel(body_lines=24),
                delete_originals={AD_ONE},
            )
            workspace = prepared["workspace"]
            formal_report = workspace / "report/ad_decision_formal_report.json"
            formal_report.write_text(
                formal_report.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )
            normalize_layout.run(
                workspace,
                "auto",
                "versions/v5_layout_final.txt",
                None,
            )

            report = verify.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "auto",
                "decisions/ads_decisions.jsonl",
                False,
            )

            provenance = next(
                check
                for check in report["checks"]
                if check["name"] == "scan_decision_provenance"
            )
            self.assertEqual(report["status"], "blocked")
            self.assertFalse(provenance["passed"])
            self.assertTrue(any("report SHA" in issue for issue in provenance["issues"]))
            self.assertNotIn("attestation", report)

    def test_two_fresh_single_book_runs_are_exact_and_semantically_identical(self) -> None:
        text = anonymous_novel()
        expected_v2 = text.replace(AD_ONE + "\n", "", 1)
        expected_start = text.index(AD_ONE)
        expected_end = expected_start + len(AD_ONE) + 1

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = []
            for name in ("anonymous-a", "anonymous-b"):
                prepared = self.prepare_applied_workspace(
                    root,
                    name,
                    text,
                    delete_originals={AD_ONE},
                )
                result = self.finish_workspace(root, prepared, export=True, review=True)
                results.append(result)

                workspace = result["workspace"]
                self.assertEqual(result["verify"]["status"], "passed")
                self.assertEqual(result["export"]["status"], "passed")
                review_data = json.loads(Path(result["review"]["data"]).read_text(encoding="utf-8"))
                self.assertEqual(review_data["workspaces"][0]["workflow"]["key"], "completed")
                self.assertEqual(
                    (workspace / "versions/v2_ads_removed.txt").read_text(encoding="utf-8"),
                    expected_v2,
                )
                operations = common.load_jsonl(workspace / "logs/operations.jsonl")
                active = [
                    item
                    for item in operations
                    if item.get("run_id") == result["apply"]["active_run_id"]
                ]
                self.assertEqual(len(active), 1)
                self.assertEqual((active[0]["start"], active[0]["end"]), (expected_start, expected_end))
                self.assertEqual(active[0]["original"], AD_ONE + "\n")
                self.assertEqual(
                    Path(result["export"]["outputs"]["txt"]).read_bytes(),
                    (workspace / "versions/v5_layout_final.txt").read_bytes(),
                )
                self.assertEqual(common.sha256_file(result["source"]), result["source_sha256"])
                self.assertEqual(
                    common.sha256_file(workspace / "versions/v0_original.txt"),
                    result["source_sha256"],
                )

            for relative in (
                "versions/v0_original.txt",
                "versions/v1_preprocessed.txt",
                "versions/v2_ads_removed.txt",
                "versions/v5_layout_final.txt",
                "decisions/ads_decisions.jsonl",
            ):
                self.assertEqual(
                    common.sha256_file(results[0]["workspace"] / relative),
                    common.sha256_file(results[1]["workspace"] / relative),
                    relative,
                )

    def test_verify_blocker_matrix_stops_export_and_builds_attention_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            stale = self.prepare_applied_workspace(
                root,
                "stale-anchor",
                anonymous_novel(),
                delete_originals={AD_ONE},
            )
            self.stale_active_operation_anchor(stale["workspace"])
            _, stale_report = self.verify_cli(stale["workspace"])
            self.assertEqual(stale_report["status"], "blocked")
            persisted = json.loads(
                (stale["workspace"] / "report/verify_report.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                persisted["decision_accounting"]["missing_operation_anchor_ids"]
            )
            self.assert_blocked_delivery_and_review(
                root,
                stale["workspace"],
                "stale-anchor",
            )

            chapter = self.prepare_applied_workspace(
                root,
                "chapter-change",
                anonymous_novel(),
                delete_originals={AD_ONE},
            )
            normalize_layout.run(
                chapter["workspace"],
                "auto",
                "versions/v5_layout_final.txt",
                None,
            )
            self.forge_layout(
                chapter["workspace"],
                lambda value: value.replace("第二章 继续", "第二章 改写", 1),
            )
            _, chapter_report = self.verify_cli(chapter["workspace"])
            self.assertEqual(chapter_report["status"], "blocked")
            persisted = json.loads(
                (chapter["workspace"] / "report/verify_report.json").read_text(encoding="utf-8")
            )
            chapter_check = next(
                check
                for check in persisted["checks"]
                if check["name"] == "final_chapter_structure"
            )
            self.assertNotEqual(
                chapter_check["before_titles"],
                chapter_check["after_titles"],
            )
            layout_check = next(
                check
                for check in persisted["checks"]
                if check["name"] == "layout_replay"
            )
            self.assertFalse(layout_check["passed"])
            self.assert_blocked_delivery_and_review(
                root,
                chapter["workspace"],
                "chapter-change",
            )

            with self.assertRaisesRegex(ValueError, "keep_basis"):
                self.prepare_applied_workspace(
                    root,
                    "deep-residual",
                    anonymous_novel(ads=(AD_ONE, AD_TWO)),
                    delete_originals={AD_TWO},
                    page_size=1,
                )
            deep_workspace = common.workspace_for_source(root / "deep-residual.txt")
            self.assert_blocked_delivery_and_review(
                root,
                deep_workspace,
                "deep-residual",
                expected_workflow="awaiting-agent",
            )

            uncertain = self.prepare_applied_workspace(
                root,
                "incomplete-review",
                anonymous_novel(),
                delete_originals=set(),
                uncertain_originals={AD_ONE},
            )
            self.assertIsNone(uncertain["apply"])
            item = self.assert_blocked_delivery_and_review(
                root,
                uncertain["workspace"],
                "incomplete-review",
            )
            self.assertGreater(item["review_summary"]["formal_uncertain"], 0)

            incomplete = self.prepare_applied_workspace(
                root,
                "incomplete-verification",
                anonymous_novel(),
                delete_originals={AD_ONE},
            )
            normalize_layout.run(
                incomplete["workspace"],
                "auto",
                "versions/v5_layout_final.txt",
                None,
            )
            _, incomplete_report = self.verify_cli(
                incomplete["workspace"],
                "--skip-residual-scan",
            )
            self.assertEqual(incomplete_report["status"], "incomplete")
            persisted = json.loads(
                (incomplete["workspace"] / "report/verify_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("attestation", persisted)
            self.assert_blocked_delivery_and_review(
                root,
                incomplete["workspace"],
                "incomplete-verification",
            )

            modified = self.prepare_applied_workspace(
                root,
                "modified-v5",
                anonymous_novel(),
                delete_originals={AD_ONE},
            )
            normalize_layout.run(
                modified["workspace"],
                "auto",
                "versions/v5_layout_final.txt",
                None,
            )
            initial_verify = verify.run(
                modified["workspace"],
                "ads",
                "versions/v1_preprocessed.txt",
                "auto",
                "decisions/ads_decisions.jsonl",
                False,
            )
            self.assertEqual(initial_verify["status"], "passed")
            self.forge_layout(
                modified["workspace"],
                lambda value: value + "被修改的匿名尾行。\n",
            )
            _, modified_report = self.verify_cli(modified["workspace"])
            self.assertEqual(modified_report["status"], "blocked")
            self.assert_blocked_delivery_and_review(
                root,
                modified["workspace"],
                "modified-v5",
            )

    def test_old_verification_is_not_reused_after_a_new_apply_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = self.prepare_applied_workspace(
                root,
                "old-verification",
                anonymous_novel(),
                delete_originals={AD_ONE},
            )
            finished = self.finish_workspace(
                root,
                prepared,
                export=False,
                review=False,
            )
            self.assertEqual(finished["verify"]["status"], "passed")
            before = self.manifest(prepared["workspace"])
            old_apply_run = before["stages"]["2_ads"]["active_run_id"]
            old_verify_run = before["stages"]["6_verify"]["run_id"]

            apply_decisions.run(
                prepared["workspace"],
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )
            manifest = self.manifest(prepared["workspace"])
            self.assertEqual(manifest["stages"]["6_verify"]["status"], "pending")
            self.assertNotEqual(manifest["stages"]["2_ads"]["active_run_id"], old_apply_run)
            self.assertNotEqual(manifest["stages"]["6_verify"].get("run_id"), old_verify_run)
            self.assertNotIn("attestation", manifest["stages"]["6_verify"])
            item = self.assert_blocked_delivery_and_review(
                root,
                prepared["workspace"],
                "old-verification",
                expected_workflow="pending-verify",
            )
            self.assertEqual(item["reports"]["verify"], {})

    def test_over_threshold_book_makes_two_book_export_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            passed = self.prepare_applied_workspace(
                root,
                "batch-passed",
                anonymous_novel(),
                delete_originals={AD_ONE},
            )
            passed = self.finish_workspace(root, passed, export=False, review=False)
            self.assertEqual(passed["verify"]["status"], "passed")

            blocked = self.prepare_applied_workspace(
                root,
                "batch-blocked",
                anonymous_novel(body_lines=20),
                delete_originals={AD_ONE},
            )
            normalize_layout.run(
                blocked["workspace"],
                "auto",
                "versions/v5_layout_final.txt",
                None,
            )
            _, blocked_summary = self.verify_cli(blocked["workspace"])
            self.assertEqual(blocked_summary["status"], "blocked")
            blocked_report = json.loads(
                (blocked["workspace"] / "report/verify_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(blocked_report["status"], "blocked")
            self.assertGreater(blocked_report["char_counts"]["deletion_ratio"], 0.08)
            self.assert_blocked_delivery_and_review(
                root,
                blocked["workspace"],
                "batch-blocked-single",
            )

            output_root = root / "batch-exports"
            process = self.cli(
                "export_outputs.py",
                passed["workspace"],
                blocked["workspace"],
                "--output-root",
                output_root,
            )
            self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
            report = json.loads(process.stdout)
            self.assertEqual(report["status"], "partial")
            self.assertEqual((report["success_count"], report["failure_count"]), (1, 1))
            self.assertEqual([item["status"] for item in report["items"]], ["passed", "failed"])
            self.assertTrue(Path(report["items"][0]["output_dir_abs"]).is_dir())
            self.assertNotIn("output_dir", report["items"][1])

            review_root = root / "batch-review"
            review = build_review_html.run(
                [passed["workspace"], blocked["workspace"]],
                str(review_root),
                False,
                80,
                3,
            )
            review_data = json.loads(Path(review["data"]).read_text(encoding="utf-8"))
            self.assertEqual(
                {book["status"]["key"] for book in review_data["books"]},
                {"completed", "needs-review"},
            )
            self.assertEqual(
                common.sha256_file(blocked["source"]),
                blocked["source_sha256"],
            )
            self.assertEqual(
                common.sha256_file(blocked["workspace"] / "versions/v0_original.txt"),
                blocked["source_sha256"],
            )


class RollbackEndToEndF7Tests(EndToEndSupport):
    def rollback_case(
        self,
        root: Path,
        level: str,
    ) -> None:
        text = (FIXTURES / "texts/rollback.txt").read_text(encoding="utf-8")
        shared = "站外提示：请访问 https://reader.example.com/shared 获取更新。"
        unique = "下载提示：请访问 https://reader.example.com/only-one 获取文件。"
        prepared = self.prepare_applied_workspace(
            root,
            f"rollback-{level}",
            text,
            delete_originals={shared, unique},
            page_size=1,
        )
        workspace = prepared["workspace"]
        clean_sha256 = common.sha256_file(workspace / "versions/v2_ads_removed.txt")
        self.assertEqual(
            (workspace / "versions/v2_ads_removed.txt").read_text(encoding="utf-8"),
            (FIXTURES / "expected/rollback.cleaned.txt").read_text(encoding="utf-8"),
        )
        normalize_layout.run(workspace, "auto", "versions/v5_layout_final.txt", None)
        verify.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "auto",
            "decisions/ads_decisions.jsonl",
            False,
        )
        build_review_html.run([workspace], None, False, 80, 3)

        decisions = common.load_jsonl(workspace / "decisions/ads_decisions.jsonl")
        all_anchor_ids = {
            str(anchor["anchor_id"])
            for decision in decisions
            for anchor in decision.get("anchors", [])
        }
        shared_decision = next(
            decision
            for decision in decisions
            if any(anchor.get("original") == shared for anchor in decision.get("anchors", []))
        )
        if level == "all":
            process = self.assert_cli_success(
                "rollback.py",
                workspace,
                "--level",
                "all",
            )
            expected_name = "rollback.all.txt"
            expected_restored = all_anchor_ids
        elif level == "module":
            process = self.assert_cli_success(
                "rollback.py",
                workspace,
                "--level",
                "module",
                "--module",
                "ads",
                "--overwrite",
            )
            expected_name = "rollback.module-ads.txt"
            expected_restored = all_anchor_ids
        elif level == "chapter":
            process = self.assert_cli_success(
                "rollback.py",
                workspace,
                "--level",
                "chapter",
                "--module",
                "ads",
                "--chapter",
                1,
            )
            expected_name = "rollback.chapter-1.txt"
            expected_restored = {
                str(anchor["anchor_id"])
                for decision in decisions
                for anchor in decision.get("anchors", [])
                if anchor.get("chapter", {}).get("index") == 1
            }
        else:
            process = self.assert_cli_success(
                "rollback.py",
                workspace,
                "--level",
                "point",
                "--module",
                "ads",
                "--candidate-id",
                str(shared_decision["candidate_id"]),
            )
            expected_name = "rollback.point-shared.txt"
            expected_restored = {
                str(anchor["anchor_id"])
                for anchor in shared_decision.get("anchors", [])
            }
        report = json.loads(process.stdout)

        output = workspace / report["output"]
        expected = FIXTURES / "expected" / expected_name
        self.assertEqual(output.read_bytes(), expected.read_bytes())
        self.assertEqual(report["output_sha256"], common.sha256_file(expected))
        manifest = self.manifest(workspace)
        self.assertEqual(manifest["current_head"], report["output"])
        self.assertEqual(
            manifest["artifacts"][report["output"]]["sha256"],
            report["output_sha256"],
        )
        for stage in ("5_layout", "6_verify", "7_export", "review"):
            self.assertEqual(manifest["stages"].get(stage, {}).get("status", "pending"), "pending")
        if level in {"chapter", "point"}:
            self.assertEqual(set(report["restored_anchor_ids"]), expected_restored)
            self.assertEqual(set(report["remaining_anchor_ids"]), all_anchor_ids - expected_restored)

        with self.assertRaisesRegex(ValueError, "verification|attestation|verify"):
            export_outputs.run(workspace, "auto", None, root / f"rollback-{level}-exports")
        manifest_before_old_reuse = (workspace / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "formal decision provenance"):
            apply_decisions.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )
        self.assertEqual(
            (workspace / "manifest.json").read_bytes(),
            manifest_before_old_reuse,
        )
        if level == "all":
            preprocess.run(
                prepared["source"],
                str(workspace),
                encoding="utf-8",
            )
        _, _, fresh_apply = self.review_and_apply_ads(
            workspace,
            delete_originals={shared, unique},
            page_size=1,
            parse_first=level == "all",
        )
        self.assertIsNotNone(fresh_apply)
        self.assertEqual(common.sha256_file(workspace / "versions/v2_ads_removed.txt"), clean_sha256)
        self.assertEqual(common.sha256_file(prepared["source"]), prepared["source_sha256"])
        self.assertEqual(
            common.sha256_file(workspace / "versions/v0_original.txt"),
            prepared["source_sha256"],
        )

    def test_all_module_chapter_and_point_rollbacks_match_gold_and_reapply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for level in ("all", "module", "chapter", "point"):
                with self.subTest(level=level):
                    self.rollback_case(root, level)


if __name__ == "__main__":
    unittest.main()
