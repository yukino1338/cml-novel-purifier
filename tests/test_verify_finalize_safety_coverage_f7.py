from __future__ import annotations

import copy
import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_decisions  # noqa: E402
import common  # noqa: E402
import finalize_ad_decisions as finalize  # noqa: E402
import normalize_layout  # noqa: E402
import preprocess  # noqa: E402
import scan_identity  # noqa: E402
import verify  # noqa: E402
from tests.support_formal_ads import formalize_ads  # noqa: E402


SCAN_ID = "a" * 64


def candidate(candidate_id: str = "AD-0001", original: str = "广告") -> dict:
    record = {
        "candidate_id": candidate_id,
        "risk_hint": "low",
        "occurrence_count": 1,
        "anchors_truncated": False,
        "anchors": [
            {
                "offset": 0,
                "end": len(original),
                "line": 1,
                "original": original,
                "prefix": "",
                "suffix": "",
                "chapter": {"index": 1, "title": "第一章"},
            }
        ],
    }
    scan_identity.attach_candidate_fingerprints([record])
    scan_identity.attach_anchor_ids([record])
    return record


def review(item: dict, verdict: str = "keep", **extra: object) -> dict:
    record = {
        "scan_id": SCAN_ID,
        "candidate_id": item["candidate_id"],
        "candidate_fingerprint": item["candidate_fingerprint"],
        "verdict": verdict,
        "confidence": 0.9,
        "reason": "可审计的测试判断",
    }
    if verdict == "uncertain":
        record["blocking_reasons"] = ["上下文不足"]
    record.update(extra)
    return record


def rule_drafts(items: list[dict]) -> list[dict]:
    return [
        {
            "scan_id": SCAN_ID,
            "candidate_id": item["candidate_id"],
            "candidate_fingerprint": item["candidate_fingerprint"],
            "verdict": "uncertain",
        }
        for item in items
    ]


def make_applied_workspace(root: Path, *, text: str | None = None, target: str = "广告甲") -> Path:
    source = root / "coverage-sample.txt"
    source.write_text(
        text or ("第一章 起点\n广告甲\n" + "正文甲。" * 20 + "\n"),
        encoding="utf-8",
    )
    workspace = preprocess.run(source)
    input_path = workspace / "versions/v1_preprocessed.txt"
    input_text = input_path.read_text(encoding="utf-8")
    start = input_text.index(target)
    formalize_ads(
        workspace,
        [{"candidate_id": "AD-0001", "offset": start, "original": target}],
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


class FinalizeSafetyCoverageF7Tests(unittest.TestCase):
    def test_only_implemented_splice_strategies_are_accepted(self) -> None:
        item = candidate()
        self.assertEqual(
            finalize.VALID_SPLICE_STRATEGIES,
            {"exact", "exact_segment", "fallback_newline", "remove_paragraph"},
        )
        for strategy in sorted(finalize.VALID_SPLICE_STRATEGIES - {"exact_segment"}):
            with self.subTest(strategy=strategy):
                decisions = finalize.compile_formal_decisions(
                    [item],
                    [review(item, "delete", action="delete", splice_strategy=strategy)],
                    rule_drafts([item]),
                    scan_id=SCAN_ID,
                )
                self.assertEqual(decisions[0]["splice_strategy"], strategy)

        for removed in ("remove_inline_join", "remove_inline_keep_punct"):
            with self.subTest(removed=removed), self.assertRaisesRegex(
                ValueError, "splice_strategy is invalid"
            ):
                finalize.compile_formal_decisions(
                    [item],
                    [review(item, "delete", action="delete", splice_strategy=removed)],
                    rule_drafts([item]),
                    scan_id=SCAN_ID,
                )

    def test_splice_precedence_and_default_are_deterministic(self) -> None:
        item = candidate()
        suggested = {**item, "suggested_decision": {"splice_strategy": "exact"}}
        self.assertEqual(finalize.splice_strategy(suggested, {}, {}), "exact")
        self.assertEqual(
            finalize.splice_strategy(item, {"splice_strategy": "fallback_newline"}, {}),
            "fallback_newline",
        )
        self.assertEqual(finalize.splice_strategy(item, {}, {}), "remove_paragraph")
        with self.assertRaisesRegex(ValueError, "splice_strategy is invalid"):
            finalize.splice_strategy(item, {}, {"splice_strategy": 1})

    def test_candidate_contract_rejects_each_malformed_shape(self) -> None:
        missing_id = candidate()
        missing_id.pop("candidate_id")
        with self.assertRaisesRegex(ValueError, "without candidate_id"):
            finalize.index_by_candidate_id([missing_id], "candidates")

        duplicate = candidate("AD-0002")
        first = candidate("AD-0001")
        duplicate["candidate_fingerprint"] = first["candidate_fingerprint"]
        contract_mutations = [
            lambda item: item.update(candidate_fingerprint="not-a-sha"),
            lambda item: item.update(anchors_truncated=None),
            lambda item: item.update(anchors={}),
            lambda item: item["anchors"][0].update(end=True),
            lambda item: item["anchors"][0].update(line=0),
            lambda item: item["anchors"][0].update(prefix=1),
            lambda item: item["anchors"][0].update(chapter={"index": 1, "title": "第一章"}, locator={"index": 1, "title": "第一章"}),
            lambda item: item["anchors"][0].update(chapter=[]),
            lambda item: item["anchors"][0].update(chapter={"index": True, "title": "第一章"}),
            lambda item: item["anchors"][0].update(chapter=None, locator={"index": 1, "title": "第一章", "kind": "chapter"}),
        ]
        with mock.patch.object(finalize.scan_identity, "validate_anchor_ids"):
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                finalize.validate_candidate_contract([first, duplicate])
            for mutate in contract_mutations:
                malformed = copy.deepcopy(first)
                mutate(malformed)
                with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                    finalize.validate_candidate_contract([malformed])

    def test_review_and_draft_contracts_reject_ambiguous_mutation(self) -> None:
        item = candidate()
        for bad_review in (
            review(item, "delete", action="keep"),
            review(item, "delete", blocking_reasons=["不应存在"]),
        ):
            with self.subTest(bad_review=bad_review), self.assertRaises(ValueError):
                finalize.compile_formal_decisions(
                    [item], [bad_review], rule_drafts([item]), scan_id=SCAN_ID
                )

        with self.assertRaisesRegex(ValueError, "duplicates"):
            finalize.normalized_blockers({"blocking_reasons": ["重复", " 重复 "]})
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            finalize.compile_formal_decisions(
                [item], [review(item)], rule_drafts([item]), scan_id="invalid"
            )

        empty = candidate()
        empty["anchors"] = []
        empty["occurrence_count"] = 0
        scan_identity.attach_candidate_fingerprints([empty])
        scan_identity.attach_anchor_ids([empty])
        with self.assertRaisesRegex(ValueError, "no executable anchors"):
            finalize.compile_formal_decisions(
                [empty],
                [review(empty, "delete")],
                rule_drafts([empty]),
                scan_id=SCAN_ID,
            )

        base_draft = {
            "scan_id": SCAN_ID,
            "candidate_id": item["candidate_id"],
            "candidate_fingerprint": item["candidate_fingerprint"],
        }
        bad_drafts = (
            {**base_draft, "candidate_id": "AD-unknown"},
            {**base_draft, "scan_id": "b" * 64},
            {**base_draft, "candidate_fingerprint": "c" * 64},
        )
        for draft in bad_drafts:
            with self.subTest(draft=draft), self.assertRaises(ValueError):
                finalize.compile_formal_decisions(
                    [item], [review(item)], [draft], scan_id=SCAN_ID
                )

    def test_candidate_count_reader_fails_closed_on_non_integer_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            self.assertIsNone(finalize.reported_candidate_count(workspace))
            report_dir = workspace / "report"
            report_dir.mkdir()
            report_path = report_dir / "ads_scan_report.json"
            for payload in ([], {"summary": []}, {"summary": {"total_candidate_count": True}}):
                with self.subTest(payload=payload):
                    report_path.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertIsNone(finalize.reported_candidate_count(workspace))

    def test_rule_draft_provenance_requires_manifest_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "anonymous.txt"
            source.write_text("第一章 起点\n正文。\n", encoding="utf-8")
            workspace = preprocess.run(source)
            draft_path = workspace / "decisions/ads_decisions.draft.jsonl"

            with self.assertRaisesRegex(ValueError, "provenance is missing"):
                finalize.validate_current_draft_provenance(
                    workspace,
                    draft_path,
                    [],
                    [],
                    {},
                    manifest={"stages": {}, "artifacts": {}},
                )

            with self.assertRaisesRegex(ValueError, "artifacts are missing"):
                finalize.validate_current_draft_provenance(
                    workspace,
                    draft_path,
                    [],
                    [],
                    {},
                    manifest={
                        "stages": {
                            "2_ads": {
                                "draft_report": "report/ad_decision_draft_report.json"
                            }
                        },
                        "artifacts": {},
                    },
                )

    def test_command_entrypoint_is_wired_to_help(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["finalize_ad_decisions.py", "--help"]),
            redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(str(ROOT / "scripts/finalize_ad_decisions.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--reviews", output.getvalue())


class VerifySafetyCoverageF7Tests(unittest.TestCase):
    def test_non_ads_module_has_no_unverified_execution_path(self) -> None:
        for module in ("titles", "blocked"):
            with self.subTest(module=module), self.assertRaisesRegex(
                ValueError,
                "formally compiled ads module",
            ):
                verify.run(Path("unused"), module, "before", "after", "decisions", False)

    def test_helper_contracts_cover_malformed_and_warning_paths(self) -> None:
        self.assertTrue(verify.is_mutating({"action": "delete"}))
        self.assertFalse(verify.is_mutating({"action": "restore"}))
        self.assertFalse(verify.is_mutating({"verdict": "keep"}))
        self.assertEqual(verify.count_chars("甲乙"), 2)
        self.assertEqual(verify.load_json_if_exists(ROOT / "does-not-exist.json"), {})

        before = [{"index": 1, "title": "第一章", "word_count": 1000}]
        self.assertEqual(verify.compare_chapters(before, []), [])
        self.assertEqual(
            verify.compare_chapters([{"word_count": 0}], [{"word_count": 0}]), []
        )
        warnings = verify.compare_chapters(before, [{"word_count": 400}])
        self.assertEqual(warnings[0]["before"], 1000)
        self.assertEqual(warnings[0]["after"], 400)

        invalid_range = [{"index": 1, "title": "第一章", "start_offset": 2, "end_offset": 1}]
        self.assertFalse(verify.compare_chapter_identity(invalid_range, invalid_range)["passed"])
        bool_range = [{"index": 1, "title": "第一章", "start_offset": True, "end_offset": 2}]
        self.assertFalse(verify.compare_layout_chapters(bool_range, bool_range)["passed"])

        accounting = verify.verify_decision_accounting(
            [
                {
                    "candidate_id": "AD-delete",
                    "verdict": "delete",
                    "anchors": [{"anchor_id": "A-expected"}, None],
                },
                {"candidate_id": "AD-keep", "verdict": "keep"},
            ],
            [
                {"candidate_id": "AD-keep", "anchor_id": "A-extra"},
                {"candidate_id": "AD-keep", "anchor_id": "A-extra"},
            ],
        )
        self.assertEqual(accounting["missing_operation_anchor_ids"], ["A-expected"])
        self.assertEqual(accounting["unexpected_operation_anchor_ids"], ["A-extra"])
        self.assertEqual(accounting["duplicate_operation_anchor_ids"], ["A-extra"])
        self.assertEqual(accounting["missing_operation_candidate_ids"], ["AD-delete"])
        self.assertEqual(accounting["unexpected_operation_candidate_ids"], ["AD-keep"])

    def test_operation_replay_rejects_bad_fields_mismatch_and_overlap(self) -> None:
        replayed, issues = verify.replay_operations(
            "abcdef",
            [
                {
                    "anchor_id": "bad",
                    "action": "delete",
                    "start": True,
                    "end": 1,
                    "original": "a",
                    "replacement": "",
                }
            ],
        )
        self.assertIsNone(replayed)
        self.assertIn("invalid replay fields", issues[0])

        replayed, issues = verify.replay_operations(
            "abcdef",
            [
                {
                    "anchor_id": "mismatch",
                    "action": "delete",
                    "start": 0,
                    "end": 2,
                    "original": "zz",
                    "replacement": "",
                }
            ],
        )
        self.assertIsNone(replayed)
        self.assertIn("does not match", issues[0])

        replayed, issues = verify.replay_operations(
            "abcdef",
            [
                {
                    "anchor_id": "left",
                    "action": "delete",
                    "start": 0,
                    "end": 3,
                    "original": "abc",
                    "replacement": "",
                },
                {
                    "anchor_id": "right",
                    "action": "delete",
                    "start": 2,
                    "end": 4,
                    "original": "cd",
                    "replacement": "",
                },
            ],
        )
        self.assertIsNone(replayed)
        self.assertEqual(issues, ["operation spans overlap"])

        replayed, issues = verify.replay_operations(
            "abcdef",
            [
                {
                    "anchor_id": "left",
                    "action": "delete",
                    "start": 0,
                    "end": 1,
                    "original": "a",
                    "replacement": "",
                },
                {
                    "anchor_id": "right",
                    "action": "delete",
                    "start": 5,
                    "end": 6,
                    "original": "f",
                    "replacement": "\n",
                },
            ],
        )
        self.assertEqual((replayed, issues), ("bcde\n", []))

        for operation, message in (
            (
                {
                    "anchor_id": "replace",
                    "action": "replace",
                    "start": 0,
                    "end": 1,
                    "original": "a",
                    "replacement": "",
                },
                "unsupported action",
            ),
            (
                {
                    "anchor_id": "replacement",
                    "action": "delete",
                    "start": 0,
                    "end": 1,
                    "original": "a",
                    "replacement": "x",
                },
                "unsupported delete replacement",
            ),
            (
                {
                    "anchor_id": "replacement-shape",
                    "action": "delete",
                    "start": 0,
                    "end": 1,
                    "original": "a",
                    "replacement": [],
                },
                "unsupported delete replacement",
            ),
        ):
            with self.subTest(message=message):
                replayed, issues = verify.replay_operations("abcdef", [operation])
                self.assertIsNone(replayed)
                self.assertIn(message, issues[0])

    def test_residual_mapping_ignores_invalid_shapes_and_requires_unique_match(self) -> None:
        decisions = [
            {"candidate_id": "", "anchors": [{"original": "ignored"}]},
            {"candidate_id": "bad-anchors", "anchors": {}},
            {"candidate_id": "AD-one", "anchors": [None, {"original": 1}, {"original": "shared"}]},
            {"candidate_id": "AD-two", "anchors": [{"original": "shared"}]},
            {"candidate_id": "AD-unique", "anchors": [{"original": "unique"}]},
        ]
        records = verify.residual_records(
            [
                {"candidate_id": "skip", "priority": "medium", "risk_hint": "medium"},
                {"candidate_id": "none", "priority": "high", "anchors": {}},
                {
                    "candidate_id": "multiple",
                    "priority": "high",
                    "anchors": [None, {"original": 1}, {"original": "shared"}],
                },
                {"candidate_id": "unique", "risk_hint": "low", "anchors": [{"original": "unique"}]},
            ],
            decisions,
        )
        self.assertEqual(len(records), 3)
        self.assertNotIn("candidate_id", records[0])
        self.assertEqual(records[1]["matched_formal_candidate_ids"], ["AD-one", "AD-two"])
        self.assertEqual(records[2]["candidate_id"], "AD-unique")

    def test_report_writers_escape_operations_and_render_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diff = root / "diff.html"
            verify.write_diff_html(
                diff,
                "before<&.txt",
                "after>.txt",
                [
                    {
                        "candidate_id": "<script>",
                        "action": "delete",
                        "start": 1,
                        "original": "<b>",
                        "replacement": "&",
                        "reason": "x > y",
                    }
                ],
            )
            rendered = diff.read_text(encoding="utf-8")
            self.assertNotIn("<script>", rendered)
            self.assertIn("&lt;script&gt;", rendered)

            empty_diff = root / "empty.html"
            verify.write_diff_html(empty_diff, "before", "after", [])
            self.assertIn("No operations recorded", empty_diff.read_text(encoding="utf-8"))

            final_report = root / "final.md"
            verify.write_final_report(
                final_report,
                {
                    "module": "ads",
                    "before": "before",
                    "after": "after",
                    "char_counts": {"before": 10, "after": 8, "deletion_ratio": 0.2},
                    "decision_accounting": {
                        "decision_count": 1,
                        "mutating_decision_count": 1,
                        "operation_count": 1,
                    },
                    "warnings": ["deletion ratio is above 8%"],
                    "residual_scan": {"candidate_count": 0, "by_layer": {}},
                },
            )
            report_text = final_report.read_text(encoding="utf-8")
            self.assertIn("- deletion ratio is above 8%", report_text)
            self.assertIn(
                'python scripts/rollback.py "<workspace>" --level all',
                report_text,
            )
            self.assertIn(
                'python scripts/rollback.py "<workspace>" --level module --module ads --overwrite',
                report_text,
            )
            self.assertNotIn("copy `versions/v0_original.txt`", report_text)

    def test_missing_apply_contract_is_reported_without_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_applied_workspace(Path(directory))
            actual = common.load_manifest(workspace)
            forged = copy.deepcopy(actual)
            forged["stages"] = []
            forged["current_head"] = "versions/not-current.txt"
            forged.setdefault("artifacts", {})["versions/not-current.txt"] = "invalid"

            with mock.patch.object(verify, "load_manifest", return_value=forged):
                report = verify.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "auto",
                    "decisions/ads_decisions.jsonl",
                    True,
                )

            apply_check = next(item for item in report["checks"] if item["name"] == "apply_binding")
            self.assertEqual(report["status"], "blocked")
            self.assertIn("apply stage is not done", apply_check["issues"])
            self.assertIn("apply stage has no active_run_id", apply_check["issues"])
            self.assertIn("verified output is not the manifest current_head", apply_check["issues"])
            self.assertNotIn("attestation", report)

    def test_stale_operation_metadata_and_missing_fingerprint_are_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_applied_workspace(Path(directory))
            operations_path = workspace / "logs/operations.jsonl"
            operations = common.load_jsonl(operations_path)
            self.assertEqual(len(operations), 1)
            operations[0]["module"] = "titles"
            operations[0].pop("candidate_fingerprint")

            with mock.patch.object(
                verify, "load_jsonl_for_run", side_effect=[operations, []]
            ):
                report = verify.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "auto",
                    "decisions/ads_decisions.jsonl",
                    True,
                )
            check = next(item for item in report["checks"] if item["name"] == "operation_binding")
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(any("stale module" in issue for issue in check["issues"]))
            self.assertTrue(any("no candidate_fingerprint" in issue for issue in check["issues"]))

    def test_large_real_deletion_emits_both_risk_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = "广告" * 301
            workspace = make_applied_workspace(
                Path(directory),
                text="第一章 起点\n" + target + "正文" * 200 + "\n",
                target=target,
            )
            report = verify.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "auto",
                "decisions/ads_decisions.jsonl",
                True,
            )
            self.assertIn("deletion ratio is above 8%", report["warnings"])
            self.assertTrue(any("chapters shrank" in warning for warning in report["warnings"]))

    def test_layout_contract_reports_missing_invalid_and_unreplayable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = make_applied_workspace(Path(directory))
            normalize_layout.run(
                workspace,
                "versions/v2_ads_removed.txt",
                "versions/v5_layout_final.txt",
                None,
            )
            actual = common.load_manifest(workspace)
            current_head = actual["current_head"]

            missing = copy.deepcopy(actual)
            missing["stages"]["5_layout"] = {}
            missing["artifacts"][current_head] = "invalid"
            with mock.patch.object(verify, "load_manifest", return_value=missing):
                missing_report = verify.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "auto",
                    "decisions/ads_decisions.jsonl",
                    True,
                )
            missing_binding = next(
                item for item in missing_report["checks"] if item["name"] == "layout_binding"
            )
            missing_replay = next(
                item for item in missing_report["checks"] if item["name"] == "layout_replay"
            )
            self.assertIn("layout stage has no committed report", missing_binding["issues"])
            self.assertIn("layout stage has no active_run_id", missing_binding["issues"])
            self.assertIn("layout stage is not done", missing_binding["issues"])
            self.assertIn("layout report has no replayable config", missing_replay["issues"])

            for name, payload, expected in (
                ("broken", "{", "layout report is invalid"),
                ("array", "[]", "layout report is not an object"),
            ):
                bad_path = workspace / "report" / f"layout-{name}.json"
                bad_path.write_text(payload, encoding="utf-8")
                forged = copy.deepcopy(actual)
                forged["stages"]["5_layout"]["report"] = bad_path.relative_to(workspace).as_posix()
                with (
                    self.subTest(name=name),
                    mock.patch.object(verify, "load_manifest", return_value=forged),
                ):
                    report = verify.run(
                        workspace,
                        "ads",
                        "versions/v1_preprocessed.txt",
                        "auto",
                        "decisions/ads_decisions.jsonl",
                        True,
                    )
                    binding = next(
                        item for item in report["checks"] if item["name"] == "layout_binding"
                    )
                    self.assertTrue(any(expected in issue for issue in binding["issues"]))

            valid_layout_report = json.loads(
                (workspace / "report/layout_report.json").read_text(encoding="utf-8")
            )
            wrong_hash = "0" * 64
            replay_path = workspace / "report/layout-unreplayable.json"
            replay_payload = {**valid_layout_report, "config_sha256": wrong_hash}
            replay_path.write_text(json.dumps(replay_payload), encoding="utf-8")
            forged = copy.deepcopy(actual)
            forged_stage = forged["stages"]["5_layout"]
            forged_stage["report"] = replay_path.relative_to(workspace).as_posix()
            forged_stage["config_sha256"] = wrong_hash
            forged_stage["artifacts"] = [current_head, replay_path.relative_to(workspace).as_posix()]
            forged["artifacts"][current_head]["config_sha256"] = wrong_hash
            with (
                mock.patch.object(verify, "load_manifest", return_value=forged),
                mock.patch.object(verify, "normalize_text", side_effect=ValueError("bad config")),
            ):
                replay_report = verify.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "auto",
                    "decisions/ads_decisions.jsonl",
                    True,
                )
            replay_check = next(
                item for item in replay_report["checks"] if item["name"] == "layout_replay"
            )
            self.assertTrue(any("config content" in issue for issue in replay_check["issues"]))
            self.assertTrue(any("layout replay failed" in issue for issue in replay_check["issues"]))

    def test_command_entrypoint_is_wired_to_help(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["verify.py", "--help"]),
            redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(str(ROOT / "scripts/verify.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--skip-residual-scan", output.getvalue())


if __name__ == "__main__":
    unittest.main()
