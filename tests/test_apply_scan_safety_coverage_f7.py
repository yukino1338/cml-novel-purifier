from __future__ import annotations

import contextlib
import copy
import io
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
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_identity  # noqa: E402
from tests.support_formal_ads import formalize_ads, formalize_clean_ads  # noqa: E402


def make_anchor(text: str, original: str, anchor_id: str = "anchor-1") -> dict[str, object]:
    start = text.index(original)
    return {
        "anchor_id": anchor_id,
        "offset": start,
        "end": start + len(original),
        "original": original,
        "prefix": text[max(0, start - 2) : start],
        "suffix": text[start + len(original) : start + len(original) + 2],
        "splice_strategy": "exact",
    }


def make_decision(
    text: str = "前广告后",
    original: str = "广告",
    *,
    candidate_id: str = "AD-1",
    fingerprint: str = "b" * 64,
    scan_id: str = "a" * 64,
    action: str = "delete",
) -> dict[str, object]:
    return {
        "scan_id": scan_id,
        "profile": "meta/book_profile.json",
        "scan_rule_pack_sha256": "1" * 64,
        "draft_rule_pack_sha256": "2" * 64,
        "book_profile_sha256": "3" * 64,
        "book_profile_file_sha256": None,
        "profile_present": False,
        "candidate_id": candidate_id,
        "candidate_fingerprint": fingerprint,
        "action": action,
        "anchors_truncated": False,
        "anchors": [make_anchor(text, original)],
    }


def make_candidate(candidate_id: str = "AD-1", sample: str = "广告甲") -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "sample": sample,
        "anchors": [
            {
                "offset": 0,
                "end": len(sample),
                "original": sample,
                "prefix": "",
                "suffix": "",
            }
        ],
    }


def bind_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    scan_identity.attach_candidate_fingerprints(candidates)
    scan_identity.attach_anchor_ids(candidates)
    return candidates


class ApplySafetyCoverageF7Tests(unittest.TestCase):
    @staticmethod
    def assert_apply_rejected_before_write(test: unittest.TestCase, workspace: Path) -> None:
        protected = (
            workspace / "manifest.json",
            workspace / "versions/v2_ads_removed.txt",
            workspace / "logs/operations.jsonl",
            workspace / "logs/anomalies.jsonl",
            workspace / "report/apply_report.json",
        )
        before = {
            path: path.read_bytes() if path.exists() else None
            for path in protected
        }
        with test.assertRaises((ValueError, scan_identity.ScanIdentityError)):
            apply_decisions.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )
        after = {
            path: path.read_bytes() if path.exists() else None
            for path in protected
        }
        test.assertEqual(after, before)

    def test_action_and_anchor_schema_are_explicit(self) -> None:
        self.assertEqual(apply_decisions.decision_action({"verdict": " delete "}), "delete")
        self.assertEqual(apply_decisions.decision_action({"action": "delete"}), "delete")
        for action in ("replace", "restore"):
            with self.subTest(action=action):
                self.assertIsNone(apply_decisions.decision_action({"action": action}))
        self.assertIsNone(apply_decisions.decision_action({"verdict": "keep", "action": "skip"}))

        valid = [{"anchor_id": "a"}]
        self.assertIs(apply_decisions.decision_anchors({"anchors": valid}), valid)
        for anchors in (None, [], "anchor", ["anchor"]):
            with self.subTest(anchors=anchors):
                with self.assertRaises(ValueError):
                    apply_decisions.decision_anchors({"anchors": anchors})

    def test_formal_decision_set_rejects_ambiguous_identity_and_shape(self) -> None:
        base = make_decision()
        invalid: list[tuple[str, list[dict[str, object]]]] = []
        for field in ("candidate_id", "candidate_fingerprint", "scan_id"):
            record = copy.deepcopy(base)
            record[field] = ""
            invalid.append((f"missing-{field}", [record]))

        duplicate_id = [copy.deepcopy(base), copy.deepcopy(base)]
        duplicate_id[1]["candidate_fingerprint"] = "c" * 64
        duplicate_id[1]["anchors"] = [make_anchor("前广告后", "广告", "anchor-2")]
        invalid.append(("duplicate-candidate", duplicate_id))

        duplicate_fingerprint = copy.deepcopy(duplicate_id)
        duplicate_fingerprint[1]["candidate_id"] = "AD-2"
        duplicate_fingerprint[1]["candidate_fingerprint"] = "b" * 64
        invalid.append(("duplicate-fingerprint", duplicate_fingerprint))

        duplicate_anchor = copy.deepcopy(duplicate_id)
        duplicate_anchor[1]["candidate_id"] = "AD-2"
        duplicate_anchor[1]["anchors"] = [make_anchor("前广告后", "广告", "anchor-1")]
        invalid.append(("duplicate-anchor", duplicate_anchor))

        multiple_scans = copy.deepcopy(duplicate_id)
        multiple_scans[1]["candidate_id"] = "AD-2"
        multiple_scans[1]["scan_id"] = "d" * 64
        invalid.append(("multiple-scans", multiple_scans))

        non_boolean_truncation = copy.deepcopy(base)
        non_boolean_truncation["anchors_truncated"] = 1
        invalid.append(("non-boolean-truncation", [non_boolean_truncation]))
        truncated_mutation = copy.deepcopy(base)
        truncated_mutation["anchors_truncated"] = True
        invalid.append(("truncated-mutation", [truncated_mutation]))

        non_mutating_scalar = copy.deepcopy(base)
        non_mutating_scalar["action"] = "skip"
        non_mutating_scalar["anchors"] = "bad"
        invalid.append(("non-mutating-scalar", [non_mutating_scalar]))
        non_mutating_item = copy.deepcopy(non_mutating_scalar)
        non_mutating_item["anchors"] = ["bad"]
        invalid.append(("non-mutating-item", [non_mutating_item]))

        missing_anchor_id = copy.deepcopy(base)
        del missing_anchor_id["anchors"][0]["anchor_id"]  # type: ignore[index]
        invalid.append(("missing-anchor-id", [missing_anchor_id]))
        non_string_strategy = copy.deepcopy(base)
        non_string_strategy["anchors"][0]["splice_strategy"] = 1  # type: ignore[index]
        invalid.append(("non-string-strategy", [non_string_strategy]))
        unknown_strategy = copy.deepcopy(base)
        unknown_strategy["anchors"][0]["splice_strategy"] = "guess-and-delete"  # type: ignore[index]
        invalid.append(("unknown-strategy", [unknown_strategy]))

        for case, decisions in invalid:
            with self.subTest(case=case), self.assertRaises(ValueError):
                apply_decisions.validate_decision_set(decisions)

        keep_without_anchors = copy.deepcopy(base)
        keep_without_anchors["action"] = "skip"
        del keep_without_anchors["anchors"]
        apply_decisions.validate_decision_set([keep_without_anchors])
        keep_with_anchors = copy.deepcopy(keep_without_anchors)
        keep_with_anchors["anchors"] = []
        apply_decisions.validate_decision_set([keep_with_anchors])
        inherited_strategy = copy.deepcopy(base)
        inherited_strategy["splice_strategy"] = None
        inherited_strategy["anchors"][0].pop("splice_strategy")  # type: ignore[index]
        apply_decisions.validate_decision_set([inherited_strategy])

    def test_formal_decision_set_requires_one_valid_rule_and_profile_identity(self) -> None:
        base = make_decision()
        invalid: list[tuple[str, list[dict[str, object]]]] = []
        for field in (
            "profile",
            "scan_rule_pack_sha256",
            "draft_rule_pack_sha256",
            "book_profile_sha256",
        ):
            missing = copy.deepcopy(base)
            missing.pop(field)
            invalid.append((f"missing-{field}", [missing]))
            malformed = copy.deepcopy(base)
            malformed[field] = "../outside.json" if field == "profile" else "A" * 64
            invalid.append((f"malformed-{field}", [malformed]))

        missing_present = copy.deepcopy(base)
        missing_present.pop("profile_present")
        invalid.append(("missing-profile-present", [missing_present]))
        non_boolean_present = copy.deepcopy(base)
        non_boolean_present["profile_present"] = 0
        invalid.append(("non-boolean-profile-present", [non_boolean_present]))
        absent_with_file = copy.deepcopy(base)
        absent_with_file["book_profile_file_sha256"] = "4" * 64
        invalid.append(("absent-profile-with-file-sha", [absent_with_file]))
        present_without_file = copy.deepcopy(base)
        present_without_file["profile_present"] = True
        invalid.append(("present-profile-without-file-sha", [present_without_file]))
        present_bad_file = copy.deepcopy(base)
        present_bad_file["profile_present"] = True
        present_bad_file["book_profile_file_sha256"] = "bad"
        invalid.append(("present-profile-bad-file-sha", [present_bad_file]))

        for field in (
            "profile",
            "scan_rule_pack_sha256",
            "draft_rule_pack_sha256",
            "book_profile_sha256",
            "book_profile_file_sha256",
            "profile_present",
        ):
            first = copy.deepcopy(base)
            second = copy.deepcopy(base)
            second["candidate_id"] = "AD-2"
            second["candidate_fingerprint"] = "c" * 64
            second["anchors"][0]["anchor_id"] = "anchor-2"  # type: ignore[index]
            if field == "profile_present":
                second[field] = True
                second["book_profile_file_sha256"] = "4" * 64
            elif field == "book_profile_file_sha256":
                first["profile_present"] = True
                first[field] = "4" * 64
                second["profile_present"] = True
                second[field] = "5" * 64
            elif field == "profile":
                second[field] = "meta/other_profile.json"
            else:
                second[field] = "4" * 64
            invalid.append((f"mixed-{field}", [first, second]))

        for case, decisions in invalid:
            with self.subTest(case=case), self.assertRaises(ValueError):
                apply_decisions.validate_decision_set(decisions, require_identity=True)

        present = copy.deepcopy(base)
        present["profile_present"] = True
        present["book_profile_file_sha256"] = "4" * 64
        apply_decisions.validate_decision_set([present], require_identity=True)

    def test_anchor_matching_is_exact_unique_and_context_bound(self) -> None:
        text = "甲pre目标post乙"
        exact = {
            "offset": 4,
            "end": 6,
            "original": "目标",
            "prefix": "pre",
            "suffix": "post",
        }
        self.assertEqual(apply_decisions.find_anchor(text, exact), (4, 6))

        invalid = (
            ({}, "missing"),
            ({"original": ""}, "missing"),
            ({"original": "目标", "prefix": 1}, "strings"),
            ({**exact, "offset": True}, "integer"),
            ({**exact, "offset": -1}, "outside"),
            ({**exact, "offset": len(text)}, "outside"),
            ({**exact, "end": True}, "end"),
            ({**exact, "end": 7}, "end"),
            ({**exact, "original": "别字"}, "original"),
            ({**exact, "prefix": "bad"}, "prefix"),
            ({**exact, "suffix": "bad"}, "suffix"),
        )
        for anchor, message in invalid:
            with self.subTest(message=message):
                self.assertIn(message, apply_decisions.find_anchor(text, anchor))

        none_context = {**exact, "prefix": None, "suffix": None}
        self.assertEqual(apply_decisions.find_anchor(text, none_context), (4, 6))
        combined = {"original": "目标", "prefix": "pre", "suffix": "post"}
        self.assertEqual(apply_decisions.find_anchor(text, combined), (4, 6))
        self.assertIn("not found", apply_decisions.find_anchor(text, {**combined, "prefix": "x"}))
        self.assertIn(
            "not unique",
            apply_decisions.find_anchor("pre目标postpre目标post", combined),
        )
        self.assertEqual(apply_decisions.find_anchor("甲目标乙", {"original": "目标"}), (1, 3))
        self.assertIn("not found", apply_decisions.find_anchor("正文", {"original": "目标"}))
        self.assertIn(
            "not unique",
            apply_decisions.find_anchor("目标与目标", {"original": "目标"}),
        )

    def test_operation_building_covers_each_supported_splice(self) -> None:
        text = "首行\n广告\n尾行"
        decision = make_decision(text, "广告")
        anchor = decision["anchors"][0]  # type: ignore[index]

        self.assertIn(
            "not match",
            apply_decisions.build_operation(text, decision, {**anchor, "offset": 0}, "delete"),
        )
        fallback = apply_decisions.build_operation(
            text,
            decision,
            {**anchor, "splice_strategy": "fallback_newline"},
            "delete",
        )
        self.assertIsInstance(fallback, apply_decisions.Operation)
        self.assertEqual(fallback.replacement, "\n")  # type: ignore[union-attr]

        paragraph = apply_decisions.build_operation(
            text,
            decision,
            {**anchor, "splice_strategy": "remove_paragraph"},
            "delete",
        )
        self.assertIsInstance(paragraph, apply_decisions.Operation)
        self.assertEqual(paragraph.original, "广告\n")  # type: ignore[union-attr]
        self.assertEqual(apply_decisions.apply_operations(text, [paragraph]), "首行\n尾行")

        last_line_text = "首行\n广告"
        last_line_decision = make_decision(last_line_text, "广告")
        last_line_anchor = last_line_decision["anchors"][0]  # type: ignore[index]
        last_line_anchor["splice_strategy"] = "remove_paragraph"  # type: ignore[index]
        last_line = apply_decisions.build_operation(
            last_line_text, last_line_decision, last_line_anchor, "delete"
        )
        self.assertIsInstance(last_line, apply_decisions.Operation)
        self.assertEqual(last_line.original, "广告")  # type: ignore[union-attr]

        partial_text = "首行\n前广告后\n尾行"
        partial_decision = make_decision(partial_text, "广告")
        partial_anchor = partial_decision["anchors"][0]  # type: ignore[index]
        partial_anchor["splice_strategy"] = "remove_paragraph"  # type: ignore[index]
        self.assertIn(
            "full paragraph",
            apply_decisions.build_operation(
                partial_text, partial_decision, partial_anchor, "delete"
            ),
        )

        for action in ("replace", "restore", "merge"):
            with self.subTest(action=action):
                self.assertEqual(
                    apply_decisions.build_operation(text, decision, anchor, action),
                    f"unsupported action: {action}",
                )

    def test_collection_is_atomic_and_preserves_operation_order(self) -> None:
        text = "甲广告甲乙广告乙"
        keep = make_decision(
            text,
            "广告甲",
            candidate_id="AD-0",
            fingerprint="d" * 64,
            action="skip",
        )
        del keep["anchors"]
        first = make_decision(text, "广告甲", candidate_id="AD-1", fingerprint="b" * 64)
        second = make_decision(text, "广告乙", candidate_id="AD-2", fingerprint="c" * 64)
        second["anchors"][0]["anchor_id"] = "anchor-2"  # type: ignore[index]
        operations = apply_decisions.collect_operations(
            text, [keep, second, first], Path("unused"), "ads"
        )
        self.assertEqual([operation.candidate_id for operation in operations], ["AD-1", "AD-2"])
        self.assertEqual(apply_decisions.apply_operations(text, operations), "甲乙")

        stale = copy.deepcopy(first)
        stale["anchors"][0]["original"] = "不存在"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "preflight"):
            apply_decisions.collect_operations(text, [stale], Path("unused"), "ads")

    def test_empty_apply_is_audited_and_cli_uses_the_fixed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "anonymous.txt"
            source.write_text("第一章 起点\n正文\n", encoding="utf-8")
            workspace = preprocess.run(source)
            scan_report, _, _ = formalize_clean_ads(workspace)

            summary = apply_decisions.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )
            self.assertEqual(summary["scan_id"], scan_report["scan_id"])
            self.assertEqual(summary["operation_count"], 0)
            self.assertFalse((workspace / "logs/operations.jsonl").exists())
            self.assertEqual(
                (workspace / "versions/v2_ads_removed.txt").read_bytes(),
                (workspace / "versions/v1_preprocessed.txt").read_bytes(),
            )
            decisions_path = workspace / "decisions/ads_decisions.jsonl"
            decisions = common.load_jsonl(decisions_path)
            forged = common.load_manifest(workspace)
            forged["stages"]["2_ads"]["decisions"] = "decisions/other.jsonl"
            with self.assertRaisesRegex(ValueError, "applied decision path"):
                apply_decisions.validate_formal_ad_provenance(
                    workspace,
                    workspace / "versions/v1_preprocessed.txt",
                    decisions_path,
                    decisions,
                    manifest=forged,
                    require_ready=True,
                )

        for module in ("unknown", "titles", "blocked"):
            with self.subTest(module=module), self.assertRaisesRegex(
                ValueError,
                "unsupported apply module",
            ):
                apply_decisions.run(Path("unused"), module, "a", "b", "c", "stage")
        with self.assertRaisesRegex(ValueError, "apply stage"):
            apply_decisions.run(Path("unused"), "ads", "a", "b", "c", "wrong")

        argv = [
            "apply_decisions.py",
            "--workspace",
            "workspace",
            "--module",
            "ads",
            "--input",
            "input.txt",
            "--decisions",
            "decisions.jsonl",
            "--output",
            "output.txt",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(apply_decisions, "run", return_value={"status": "done"}) as run,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            apply_decisions.main()
        self.assertEqual(run.call_args.kwargs["stage"], "2_ads")
        self.assertIn("done", stdout.getvalue())

    def test_formal_provenance_rejects_each_stage_and_artifact_binding_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "anonymous.txt"
            source.write_text("第一章 起点\n正文。\n", encoding="utf-8")
            workspace = preprocess.run(source)
            formalize_clean_ads(workspace)
            input_path = workspace / "versions/v1_preprocessed.txt"
            decisions_path = workspace / "decisions/ads_decisions.jsonl"
            decisions = common.load_jsonl(decisions_path)
            manifest = common.load_manifest(workspace)

            mutations = {
                "status": lambda stage, _: stage.update(status="candidates_ready"),
                "report-path": lambda stage, _: stage.update(formal_report=""),
                "run-id": lambda stage, _: stage.update(run_id=""),
                "decision-sha": lambda stage, _: stage.update(
                    formal_decisions_sha256="0" * 64
                ),
                "report-sha": lambda stage, _: stage.update(formal_report_sha256=None),
                "draft-run-id": lambda stage, _: stage.update(draft_run_id=""),
                "draft-sha": lambda stage, _: stage.update(
                    draft_decisions_sha256="0" * 64
                ),
                "draft-report-sha": lambda stage, _: stage.update(
                    draft_report_sha256="0" * 64
                ),
                "stage-ownership": lambda stage, _: stage.update(artifacts=[]),
                "artifact-ledger": lambda _, value: value["artifacts"][
                    "decisions/ads_decisions.jsonl"
                ].update(sha256="0" * 64),
                "draft-artifact-ledger": lambda _, value: value["artifacts"][
                    "decisions/ads_decisions.draft.jsonl"
                ].update(run_id="forged"),
            }
            for case, mutate in mutations.items():
                forged = copy.deepcopy(manifest)
                mutate(forged["stages"]["2_ads"], forged)
                with self.subTest(case=case), self.assertRaises(ValueError):
                    apply_decisions.validate_formal_ad_provenance(
                        workspace,
                        input_path,
                        decisions_path,
                        decisions,
                        manifest=forged,
                        require_ready=True,
                    )

            malformed = root / "malformed-report.json"
            malformed.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "valid UTF-8 JSON"):
                apply_decisions._load_json_object(malformed, "formal report")
            malformed.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                apply_decisions._load_json_object(malformed, "formal report")

    def test_apply_recomputes_current_profile_and_draft_pack_before_any_write(self) -> None:
        for case in ("profile-created", "draft-pack-changed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "identity.txt"
                source.write_text("正文\n", encoding="utf-8")
                workspace = preprocess.run(source)
                formalize_clean_ads(workspace)

                if case == "profile-created":
                    common.write_json(
                        workspace / "meta/book_profile.json",
                        {"terms": ["new"]},
                    )
                    self.assert_apply_rejected_before_write(self, workspace)
                else:
                    changed_pack = copy.deepcopy(scan_identity.build_draft_rule_pack())
                    changed_pack["files"].append(
                        {"path": "scripts/forged.py", "sha256": "0" * 64}
                    )
                    with mock.patch.object(
                        scan_identity,
                        "build_draft_rule_pack",
                        return_value=changed_pack,
                    ):
                        self.assert_apply_rejected_before_write(self, workspace)

    def test_consistent_formal_copies_cannot_override_current_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "identity.txt"
            source.write_text("正文\n", encoding="utf-8")
            workspace = preprocess.run(source)
            formalize_clean_ads(workspace)

            report_path = workspace / "report/ad_decision_formal_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            manifest = common.load_manifest(workspace)
            stage = manifest["stages"]["2_ads"]
            forged = "0" * 64
            report["draft_rule_pack_sha256"] = forged
            stage["draft_rule_pack_sha256"] = forged
            common.write_json(report_path, report)
            report_sha256 = common.sha256_file(report_path)
            stage["formal_report_sha256"] = report_sha256
            manifest["artifacts"]["report/ad_decision_formal_report.json"].update(
                sha256=report_sha256,
                size_bytes=report_path.stat().st_size,
            )
            common.write_json(workspace / "manifest.json", manifest)

            self.assert_apply_rejected_before_write(self, workspace)

    def test_forged_formal_row_identity_is_rejected_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "identity.txt"
            source.write_text("正文\n可疑片段\n", encoding="utf-8")
            workspace = preprocess.run(source)
            text = (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8")
            formalize_ads(
                workspace,
                [
                    {
                        "candidate_id": "AD-1",
                        "offset": text.index("可疑片段"),
                        "original": "可疑片段",
                    }
                ],
                verdict="uncertain",
            )

            decisions_path = workspace / "decisions/ads_decisions.jsonl"
            decisions = common.load_jsonl(decisions_path)
            decisions[0]["scan_rule_pack_sha256"] = "0" * 64
            common.write_jsonl(decisions_path, decisions)
            decisions_sha256 = common.sha256_file(decisions_path)

            report_path = workspace / "report/ad_decision_formal_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["formal_decisions_sha256"] = decisions_sha256
            common.write_json(report_path, report)
            report_sha256 = common.sha256_file(report_path)

            manifest = common.load_manifest(workspace)
            stage = manifest["stages"]["2_ads"]
            stage["formal_decisions_sha256"] = decisions_sha256
            stage["formal_report_sha256"] = report_sha256
            manifest["artifacts"]["decisions/ads_decisions.jsonl"].update(
                sha256=decisions_sha256,
                size_bytes=decisions_path.stat().st_size,
            )
            manifest["artifacts"]["report/ad_decision_formal_report.json"].update(
                sha256=report_sha256,
                size_bytes=report_path.stat().st_size,
            )
            common.write_json(workspace / "manifest.json", manifest)

            self.assert_apply_rejected_before_write(self, workspace)


class ScanIdentitySafetyCoverageF7Tests(unittest.TestCase):
    def write_structure(
        self, root: Path, text: str, structure: dict[str, object]
    ) -> tuple[Path, Path]:
        input_path = root / "input.txt"
        structure_path = root / "chapters.json"
        input_path.write_text(text, encoding="utf-8")
        bound = copy.deepcopy(structure)
        bound["input_sha256"] = common.sha256_file(input_path)
        structure_path.write_text(
            json.dumps(bound, ensure_ascii=False),
            encoding="utf-8",
        )
        return input_path, structure_path

    def body_structure(self, text: str) -> dict[str, object]:
        slices = [
            {
                "kind": "body",
                "start_offset": 0,
                "heading_end_offset": 0,
                "end_offset": len(text),
            }
        ]
        return {
            "schema_version": 2,
            "chapters": [],
            "slices": slices,
            "locators": copy.deepcopy(slices),
            "fallback_chunking": {"enabled": False, "chunk_count": 0},
            "fallback_chunks": [],
        }

    @staticmethod
    def sync_locators(structure: dict[str, object]) -> None:
        structure["locators"] = copy.deepcopy(structure["slices"])

    def make_paged_fixture(
        self, root: Path
    ) -> tuple[Path, dict[str, object], list[dict[str, object]]]:
        source = root / "anonymous.txt"
        source.write_text("第一章 起点\n正文内容。\n", encoding="utf-8")
        workspace = preprocess.run(source)
        parse_structure.run(workspace)
        input_path = workspace / "versions/v1_preprocessed.txt"
        structure_path = workspace / "meta/chapters.json"
        candidates = bind_candidates(
            [make_candidate(f"AD-{index}", f"广告-{index}") for index in range(1, 4)]
        )
        scan_config = {"page_size": 2, "max_anchors": 5}
        identity = scan_identity.build_scan_identity(
            "ads", input_path, structure_path, scan_config, candidates
        )

        pages_dir = workspace / "candidates/ads_pages"
        pages_dir.mkdir(parents=True)
        manifest_entries: list[dict[str, object]] = []
        chunks = (candidates[:2], candidates[2:])
        for page_number, records in enumerate(chunks, start=1):
            page_path = pages_dir / f"ads_page_{page_number:03d}.jsonl"
            common.write_jsonl(page_path, records)
            manifest_entries.append(
                {
                    "file": page_path.relative_to(workspace).as_posix(),
                    "page_number": page_number,
                    "record_count": len(records),
                    "sha256": common.sha256_file(page_path),
                }
            )
        first_page = workspace / "candidates/ads.jsonl"
        common.write_jsonl(first_page, candidates[:2])
        report: dict[str, object] = {
            **identity,
            "input": input_path.relative_to(workspace).as_posix(),
            "scan_config": scan_config,
            "summary": {
                "page_size": 2,
                "total_candidate_count": 3,
            },
            "pages": {
                "pages_dir": pages_dir.relative_to(workspace).as_posix(),
                "first_page": first_page.relative_to(workspace).as_posix(),
                "page_count": 2,
                "manifest": manifest_entries,
            },
        }
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        manifest["stages"]["2_ads"] = {
            "status": "candidates_ready",
            **identity,
        }
        common.write_json(workspace / "manifest.json", manifest)
        return workspace, report, candidates

    def test_candidate_and_anchor_bindings_reject_tampering_and_duplicates(self) -> None:
        candidate = make_candidate()
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "fingerprint"):
            scan_identity.attach_anchor_ids([candidate])

        scan_identity.attach_candidate_fingerprints([candidate])
        tampered = copy.deepcopy(candidate)
        tampered["sample"] = "篡改"
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "fingerprint"):
            scan_identity.attach_anchor_ids([tampered])
        invalid_anchors = copy.deepcopy(candidate)
        invalid_anchors["anchors"] = "bad"
        invalid_anchors["candidate_fingerprint"] = scan_identity.candidate_fingerprint(
            invalid_anchors
        )
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "anchors"):
            scan_identity.attach_anchor_ids([invalid_anchors])

        first = bind_candidates([make_candidate("AD-1", "相同")])[0]
        second = bind_candidates([make_candidate("AD-2", "相同")])[0]
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "duplicated"):
            scan_identity.attach_anchor_ids([copy.deepcopy(first), copy.deepcopy(second)])
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "duplicated"):
            scan_identity.validate_anchor_ids([first, second])

        invalid_fingerprint = copy.deepcopy(first)
        invalid_fingerprint["candidate_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "fingerprint"):
            scan_identity.validate_anchor_ids([invalid_fingerprint])
        invalid_anchor_shape = copy.deepcopy(first)
        invalid_anchor_shape["anchors"] = ["bad"]
        invalid_anchor_shape["candidate_fingerprint"] = scan_identity.candidate_fingerprint(
            invalid_anchor_shape
        )
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "anchors"):
            scan_identity.validate_anchor_ids([invalid_anchor_shape])
        invalid_anchor_id = copy.deepcopy(first)
        invalid_anchor_id["anchors"][0]["anchor_id"] = "AN-forged"  # type: ignore[index]
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "anchor ID"):
            scan_identity.validate_anchor_ids([invalid_anchor_id])

        missing_fingerprint = make_candidate()
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "fingerprint"):
            scan_identity.candidate_set_sha256([missing_fingerprint])
        mismatched_fingerprint = copy.deepcopy(first)
        mismatched_fingerprint["sample"] = "changed"
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "content"):
            scan_identity.candidate_set_sha256([mismatched_fingerprint])
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "duplicated"):
            scan_identity.candidate_set_sha256([first, second])

        for invalid_id in (None, "", 1):
            record = copy.deepcopy(first)
            record["candidate_id"] = invalid_id
            with self.subTest(candidate_id=invalid_id), self.assertRaisesRegex(
                scan_identity.ScanIdentityError, "candidate ID"
            ):
                scan_identity.validate_candidate_set([record])
        duplicate_id = copy.deepcopy(second)
        duplicate_id["candidate_id"] = first["candidate_id"]
        with self.assertRaisesRegex(scan_identity.ScanIdentityError, "candidate ID"):
            scan_identity.validate_candidate_set([first, duplicate_id])

    def test_bound_structure_accepts_each_model_and_rejects_inconsistency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "正文"
            body = self.body_structure(text)
            input_path, structure_path = self.write_structure(root, text, body)
            self.assertEqual(
                scan_identity.load_bound_structure(input_path, structure_path)["slices"],
                body["slices"],
            )

            fallback_slices = [
                {
                    "kind": "fallback_chunk",
                    "start_offset": 0,
                    "heading_end_offset": 0,
                    "end_offset": 1,
                },
                {
                    "kind": "fallback_chunk",
                    "start_offset": 1,
                    "heading_end_offset": 1,
                    "end_offset": 2,
                },
            ]
            fallback = {
                "schema_version": 2,
                "chapters": [],
                "slices": fallback_slices,
                "locators": copy.deepcopy(fallback_slices),
                "fallback_chunking": {"enabled": True, "chunk_count": 2},
                "fallback_chunks": [
                    {key: value for key, value in item.items() if key != "heading_end_offset"}
                    for item in fallback_slices
                ],
            }
            fallback_input, fallback_path = self.write_structure(root, text, fallback)
            self.assertEqual(
                len(scan_identity.load_bound_structure(fallback_input, fallback_path)["slices"]),
                2,
            )

            chapter = {
                "index": 1,
                "title": "第一章",
                "start_offset": 1,
                "heading_end_offset": 3,
                "end_offset": 4,
            }
            chapter_slices = [
                {
                    "kind": "front_matter",
                    "start_offset": 0,
                    "heading_end_offset": 0,
                    "end_offset": 1,
                },
                {"kind": "chapter", **chapter},
            ]
            chapter_model = {
                "schema_version": 2,
                "chapters": [chapter],
                "slices": chapter_slices,
                "locators": copy.deepcopy(chapter_slices),
                "fallback_chunking": {"enabled": False, "chunk_count": 0},
                "fallback_chunks": [],
            }
            chapter_input, chapter_path = self.write_structure(root, "前第一章", chapter_model)
            self.assertEqual(
                scan_identity.load_bound_structure(chapter_input, chapter_path)["chapters"],
                [chapter],
            )

            no_front_chapter = copy.deepcopy(chapter_model)
            no_front_chapter["chapters"][0]["start_offset"] = 0  # type: ignore[index]
            no_front_chapter["chapters"][0]["heading_end_offset"] = 2  # type: ignore[index]
            no_front_chapter["chapters"][0]["end_offset"] = 3  # type: ignore[index]
            no_front_chapter["slices"] = [
                {"kind": "chapter", **no_front_chapter["chapters"][0]}  # type: ignore[index]
            ]
            self.sync_locators(no_front_chapter)
            no_front_input, no_front_path = self.write_structure(
                root, "第一章", no_front_chapter
            )
            scan_identity.load_bound_structure(no_front_input, no_front_path)

        malformed_cases: list[tuple[str, object]] = []
        base = self.body_structure("正文")
        malformed_cases.append(("schema", {**base, "schema_version": 1}))
        malformed_cases.append(("chapters-shape", {**base, "chapters": [1]}))
        malformed_cases.append(("slices-empty", {**base, "slices": [], "locators": []}))
        malformed_cases.append(("slices-shape", {**base, "slices": [1], "locators": [1]}))
        malformed_cases.append(("locators", {**base, "locators": []}))

        invalid_kind = copy.deepcopy(base)
        invalid_kind["slices"][0]["kind"] = "unknown"  # type: ignore[index]
        self.sync_locators(invalid_kind)
        malformed_cases.append(("slice-kind", invalid_kind))
        invalid_offsets = copy.deepcopy(base)
        invalid_offsets["slices"][0]["start_offset"] = True  # type: ignore[index]
        self.sync_locators(invalid_offsets)
        malformed_cases.append(("slice-offsets", invalid_offsets))
        incomplete = copy.deepcopy(base)
        incomplete["slices"][0]["end_offset"] = 1  # type: ignore[index]
        self.sync_locators(incomplete)
        malformed_cases.append(("incomplete-slices", incomplete))
        malformed_cases.append(("fallback-shape", {**base, "fallback_chunking": []}))
        malformed_cases.append(("fallback-chunks-shape", {**base, "fallback_chunks": [1]}))
        malformed_cases.append(
            (
                "fallback-count",
                {**base, "fallback_chunking": {"enabled": False, "chunk_count": 1}},
            )
        )
        undeclared = copy.deepcopy(base)
        undeclared["slices"][0]["kind"] = "fallback_chunk"  # type: ignore[index]
        self.sync_locators(undeclared)
        malformed_cases.append(("undeclared-fallback", undeclared))
        two_bodies = copy.deepcopy(base)
        two_bodies["slices"] = [
            {
                "kind": "body",
                "start_offset": 0,
                "heading_end_offset": 0,
                "end_offset": 1,
            },
            {
                "kind": "body",
                "start_offset": 1,
                "heading_end_offset": 1,
                "end_offset": 2,
            },
        ]
        self.sync_locators(two_bodies)
        malformed_cases.append(("multiple-bodies", two_bodies))

        bad_fallback = copy.deepcopy(fallback)
        bad_fallback["fallback_chunks"][0]["end_offset"] = 2  # type: ignore[index]
        malformed_cases.append(("fallback-mismatch", bad_fallback))
        bad_chapters = copy.deepcopy(chapter_model)
        bad_chapters["chapters"][0]["title"] = "不一致"  # type: ignore[index]
        malformed_cases.append(("chapter-mismatch", bad_chapters))
        bad_front = copy.deepcopy(chapter_model)
        bad_front["slices"][0]["end_offset"] = 0  # type: ignore[index]
        bad_front["slices"][1]["start_offset"] = 0  # type: ignore[index]
        bad_front["chapters"][0]["start_offset"] = 0  # type: ignore[index]
        self.sync_locators(bad_front)
        malformed_cases.append(("front-mismatch", bad_front))

        for case, structure in malformed_cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                input_path, structure_path = self.write_structure(root, "正文", structure)
                with self.assertRaises(scan_identity.ScanIdentityError):
                    scan_identity.load_bound_structure(input_path, structure_path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.txt"
            structure_path = root / "chapters.json"
            input_path.write_text("正文", encoding="utf-8")
            structure_path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(scan_identity.ScanIdentityError, "UTF-8 JSON"):
                scan_identity.load_bound_structure(input_path, structure_path)

            structure_path.write_text(
                json.dumps({**self.body_structure("正文"), "input_sha256": "0" * 64}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(scan_identity.ScanIdentityError, "stale"):
                scan_identity.load_bound_structure(input_path, structure_path)

    def test_scan_report_is_bound_to_committed_workspace_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, report, candidates = self.make_paged_fixture(Path(directory))
            scan_identity.validate_scan_identity(workspace, report, candidates)

            report_cases = (
                ("scanner", None),
                ("scan_id", "bad"),
                ("input", None),
                ("input", "../outside.txt"),
                ("input", "versions/missing.txt"),
                ("scan_config", []),
            )
            for field, value in report_cases:
                forged = copy.deepcopy(report)
                forged[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(
                    scan_identity.ScanIdentityError
                ):
                    scan_identity.validate_scan_identity(workspace, forged, candidates)

            unknown_scanner = copy.deepcopy(report)
            unknown_scanner["scanner"] = "other"
            with self.assertRaisesRegex(
                scan_identity.ScanIdentityError,
                "not active|no declared rule pack",
            ):
                scan_identity.validate_scan_identity(workspace, unknown_scanner, candidates)

            stage_mismatch = copy.deepcopy(report)
            stage_mismatch["scan_id"] = "b" * 64
            with self.assertRaisesRegex(scan_identity.ScanIdentityError, "committed scan"):
                scan_identity.validate_scan_identity(workspace, stage_mismatch, candidates)

            bad_config = copy.deepcopy(report)
            bad_config["scan_config"]["page_size"] = 9  # type: ignore[index]
            with self.assertRaisesRegex(scan_identity.ScanIdentityError, "config hash"):
                scan_identity.validate_scan_identity(workspace, bad_config, candidates)

            alternatives = bind_candidates([make_candidate("AD-X", "另一条")])
            with self.assertRaisesRegex(scan_identity.ScanIdentityError, "candidate set hash"):
                scan_identity.validate_scan_identity(workspace, report, alternatives)

            input_path = workspace / str(report["input"])
            original_input = input_path.read_bytes()
            input_path.write_text("已篡改", encoding="utf-8")
            try:
                with self.assertRaisesRegex(scan_identity.ScanIdentityError, "input hash"):
                    scan_identity.validate_scan_identity(workspace, report, candidates)
            finally:
                input_path.write_bytes(original_input)

            structure_path = workspace / "meta/chapters.json"
            original_structure = structure_path.read_bytes()
            structure_path.write_bytes(original_structure + b" ")
            try:
                with self.assertRaisesRegex(scan_identity.ScanIdentityError, "structure hash"):
                    scan_identity.validate_scan_identity(workspace, report, candidates)
            finally:
                structure_path.write_bytes(original_structure)

            manifest_path = workspace / "manifest.json"
            original_manifest = manifest_path.read_bytes()
            manifest = json.loads(original_manifest.decode("utf-8"))
            manifest["stages"]["2_ads"]["status"] = "pending"
            common.write_json(manifest_path, manifest)
            try:
                with self.assertRaisesRegex(scan_identity.ScanIdentityError, "not active"):
                    scan_identity.validate_scan_identity(workspace, report, candidates)
            finally:
                manifest_path.write_bytes(original_manifest)

            forged_scan = copy.deepcopy(report)
            forged_scan["scan_id"] = "b" * 64
            manifest = json.loads(original_manifest.decode("utf-8"))
            manifest["stages"]["2_ads"]["scan_id"] = "b" * 64
            common.write_json(manifest_path, manifest)
            try:
                with self.assertRaisesRegex(scan_identity.ScanIdentityError, "scan ID"):
                    scan_identity.validate_scan_identity(workspace, forged_scan, candidates)
            finally:
                manifest_path.write_bytes(original_manifest)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.txt"
            structure_path = root / "structure.json"
            input_path.write_text("正文", encoding="utf-8")
            structure_path.write_text("{}", encoding="utf-8")
            candidates = bind_candidates([make_candidate()])
            with self.assertRaisesRegex(scan_identity.ScanIdentityError, "scanner"):
                scan_identity.build_scan_identity(
                    "", input_path, structure_path, {}, candidates
                )

        for value in (None, "f" * 63, "G" * 64, "f" * 64):
            with self.subTest(hash_value=value):
                self.assertEqual(scan_identity._is_sha256(value), value == "f" * 64)

    def test_page_artifacts_fail_closed_for_every_manifest_boundary(self) -> None:
        cases = (
            "pages-shape",
            "summary-shape",
            "directory-fields",
            "manifest-count",
            "directory-outside",
            "directory-missing",
            "entry-shape",
            "entry-fields",
            "page-outside",
            "page-missing",
            "page-hash",
            "page-json",
            "record-shape",
            "record-count-range",
            "non-final-short",
            "page-numbers",
            "duplicate-files",
            "extra-file",
            "total-count",
            "first-page-outside",
            "first-page-json",
            "first-page-mismatch",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                workspace, report, _candidates = self.make_paged_fixture(Path(directory))
                pages = report["pages"]
                summary = report["summary"]
                manifest = pages["manifest"]  # type: ignore[index]
                first_entry = manifest[0]  # type: ignore[index]
                first_page = workspace / first_entry["file"]  # type: ignore[index]

                if case == "pages-shape":
                    report["pages"] = []
                elif case == "summary-shape":
                    report["summary"] = []
                elif case == "directory-fields":
                    pages["page_count"] = True  # type: ignore[index]
                elif case == "manifest-count":
                    pages["manifest"] = manifest[:1]  # type: ignore[index]
                elif case == "directory-outside":
                    pages["pages_dir"] = "../outside"  # type: ignore[index]
                elif case == "directory-missing":
                    pages["pages_dir"] = "candidates/missing"  # type: ignore[index]
                elif case == "entry-shape":
                    manifest[0] = []  # type: ignore[index]
                elif case == "entry-fields":
                    first_entry["sha256"] = "bad"  # type: ignore[index]
                elif case == "page-outside":
                    first_entry["file"] = "candidates/ads.jsonl"  # type: ignore[index]
                elif case == "page-missing":
                    first_page.unlink()
                elif case == "page-hash":
                    first_page.write_text("{}\n", encoding="utf-8")
                elif case == "page-json":
                    first_page.write_text("{broken\n", encoding="utf-8")
                    first_entry["sha256"] = common.sha256_file(first_page)  # type: ignore[index]
                elif case == "record-shape":
                    first_page.write_text("[]\n[]\n", encoding="utf-8")
                    first_entry["sha256"] = common.sha256_file(first_page)  # type: ignore[index]
                elif case == "record-count-range":
                    first_page.write_text("", encoding="utf-8")
                    first_entry["record_count"] = 0  # type: ignore[index]
                    first_entry["sha256"] = common.sha256_file(first_page)  # type: ignore[index]
                elif case == "non-final-short":
                    first_page.write_text(
                        first_page.read_text(encoding="utf-8").splitlines()[0] + "\n",
                        encoding="utf-8",
                    )
                    first_entry["record_count"] = 1  # type: ignore[index]
                    first_entry["sha256"] = common.sha256_file(first_page)  # type: ignore[index]
                elif case == "page-numbers":
                    manifest[1]["page_number"] = 1  # type: ignore[index]
                elif case == "duplicate-files":
                    manifest[1]["file"] = first_entry["file"]  # type: ignore[index]
                    manifest[1]["sha256"] = first_entry["sha256"]  # type: ignore[index]
                    manifest[1]["record_count"] = first_entry["record_count"]  # type: ignore[index]
                elif case == "extra-file":
                    (workspace / "candidates/ads_pages/extra.jsonl").write_text(
                        "{}\n", encoding="utf-8"
                    )
                elif case == "total-count":
                    summary["total_candidate_count"] = 4  # type: ignore[index]
                elif case == "first-page-outside":
                    pages["first_page"] = "../outside.jsonl"  # type: ignore[index]
                elif case == "first-page-json":
                    (workspace / str(pages["first_page"])).write_text(  # type: ignore[index]
                        "{broken\n", encoding="utf-8"
                    )
                elif case == "first-page-mismatch":
                    (workspace / str(pages["first_page"])).write_text(  # type: ignore[index]
                        "{}\n", encoding="utf-8"
                    )

                with self.assertRaises(scan_identity.ScanIdentityError):
                    scan_identity.load_validated_pages(workspace, report)

        with tempfile.TemporaryDirectory() as directory:
            workspace, report, candidates = self.make_paged_fixture(Path(directory))
            self.assertEqual(scan_identity.load_validated_pages(workspace, report), candidates)


if __name__ == "__main__":
    unittest.main()
