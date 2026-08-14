from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import finalize_ad_decisions as finalize  # noqa: E402
import apply_decisions  # noqa: E402
import common  # noqa: E402
import make_ad_decisions  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_ads  # noqa: E402
import scan_identity  # noqa: E402
from tests.support_formal_ads import formalize_ads  # noqa: E402


SCAN_ID = "a" * 64
KEEP_BASIS_SCHEMA = "cml.keep-basis.v1"


def reviewed_occurrences(item: dict) -> list[dict[str, str]]:
    return [
        {
            "anchor_id": anchor["anchor_id"],
            "text_sha256": hashlib.sha256(
                anchor["original"].encode("utf-8")
            ).hexdigest(),
        }
        for anchor in item["anchors"]
    ]


def occurrence_coverage_sha256(occurrences: list[dict[str, str]]) -> str:
    payload = json.dumps(
        occurrences,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def keep_basis(
    item: dict,
    basis_type: str = "narrative_context",
    note: str = "已逐条核对，全部为剧情正文。",
) -> dict:
    occurrences = reviewed_occurrences(item)
    return {
        "schema": KEEP_BASIS_SCHEMA,
        "type": basis_type,
        "reviewed_occurrences": occurrences,
        "occurrence_coverage_sha256": occurrence_coverage_sha256(occurrences),
        "note": note,
    }


def candidate(candidate_id: str, original: str, offset: int = 0) -> dict:
    item = {
        "candidate_id": candidate_id,
        "risk_hint": "low",
        "occurrence_count": 1,
        "anchors_truncated": False,
        "anchors": [
            {
                "offset": offset,
                "end": offset + len(original),
                "line": 1,
                "original": original,
                "prefix": "",
                "suffix": "",
                "chapter": {"index": 1, "title": "第一章"},
            }
        ],
    }
    scan_identity.attach_candidate_fingerprints([item])
    scan_identity.attach_anchor_ids([item])
    return item


def review(item: dict, verdict: str = "keep", **overrides: object) -> dict:
    record = {
        "scan_id": SCAN_ID,
        "candidate_id": item["candidate_id"],
        "candidate_fingerprint": item["candidate_fingerprint"],
        "verdict": verdict,
        "confidence": 0.99,
        "reason": "正常正文" if verdict == "keep" else "独立站外广告",
    }
    if verdict == "uncertain":
        record["blocking_reasons"] = ["上下文不足"]
    record.update(overrides)
    return record


def rule_drafts(
    candidates: list[dict], overrides: list[dict] | None = None
) -> list[dict]:
    override_map = {
        str(item["candidate_id"]): copy.deepcopy(item)
        for item in (overrides or [])
    }
    result: list[dict] = []
    for item in candidates:
        record = {
            "scan_id": SCAN_ID,
            "candidate_id": item["candidate_id"],
            "candidate_fingerprint": item["candidate_fingerprint"],
            "verdict": "uncertain",
        }
        record.update(override_map.get(str(item["candidate_id"]), {}))
        result.append(record)
    return result


def prepared_workspace(
    root: Path,
    *,
    profile: dict | None = None,
) -> tuple[Path, list[dict], dict]:
    source = root / "identity-source.txt"
    source.write_text(
        "第一章 起点\n请访问 https://reader.example.com 获取更新。\n正文继续。\n",
        encoding="utf-8",
    )
    workspace = preprocess.run(source)
    parse_structure.run(workspace)
    profile_path = workspace / "meta/book_profile.json"
    if profile is not None:
        common.write_json(profile_path, profile)
    scan_report = scan_ads.run(
        workspace,
        "versions/v1_preprocessed.txt",
        "candidates/ads.jsonl",
        12,
        10,
        10,
    )
    candidates = scan_identity.load_validated_pages(workspace, scan_report)
    if not candidates:
        raise AssertionError("identity fixture must produce ad candidates")
    common.write_jsonl(
        workspace / "decisions/ads_agent_reviews.jsonl",
        [
            review(
                item,
                "keep",
                scan_id=scan_report["scan_id"],
                keep_basis=keep_basis(item),
            )
            for item in candidates
        ],
    )
    make_ad_decisions.run(
        workspace,
        "candidates/ads_pages",
        "decisions/ads_decisions.draft.jsonl",
        "meta/book_profile.json",
        True,
    )
    return workspace, candidates, scan_report


class FinalizeAdDecisionsTests(unittest.TestCase):
    def test_finalize_recomputes_draft_pack_and_profile_before_any_write(self) -> None:
        cases = ("pack", "profile_create", "profile_change", "profile_delete")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                initial_profile = {"terms": ["主角"]} if case in {
                    "profile_change",
                    "profile_delete",
                } else None
                workspace, _, _ = prepared_workspace(root, profile=initial_profile)
                profile_path = workspace / "meta/book_profile.json"
                if case == "profile_create":
                    common.write_json(profile_path, {"terms": ["新建"]})
                elif case == "profile_change":
                    common.write_json(profile_path, {"terms": ["已改变"]})
                elif case == "profile_delete":
                    profile_path.unlink()

                manifest_before = (workspace / "manifest.json").read_bytes()
                formal = workspace / "decisions/ads_decisions.jsonl"
                formal_report = workspace / "report/ad_decision_formal_report.json"
                self.assertFalse(formal.exists())
                self.assertFalse(formal_report.exists())

                context = (
                    mock.patch.object(
                        scan_identity,
                        "build_draft_rule_pack",
                        return_value={
                            **scan_identity.build_draft_rule_pack(),
                            "schema_version": 999,
                        },
                    )
                    if case == "pack"
                    else mock.patch.object(scan_identity, "build_draft_rule_pack", wraps=scan_identity.build_draft_rule_pack)
                )
                with context, self.assertRaisesRegex(ValueError, "stale|profile|rule pack"):
                    finalize.run(
                        workspace,
                        "candidates/ads_pages",
                        "decisions/ads_agent_reviews.jsonl",
                        "decisions/ads_decisions.draft.jsonl",
                        "decisions/ads_decisions.jsonl",
                    )
                self.assertFalse(formal.exists())
                self.assertFalse(formal_report.exists())
                self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)

    def test_finalize_rejects_draft_row_report_and_stage_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidates, scan_report = prepared_workspace(Path(directory))
            draft_path = workspace / "decisions/ads_decisions.draft.jsonl"
            drafts = common.load_jsonl(draft_path)
            manifest = common.load_manifest(workspace)

            cases = {
                "row": ({**drafts[0], "draft_rule_pack_sha256": "f" * 64}, None, None),
                "report": (None, {"draft_rule_pack_sha256": "f" * 64}, None),
                "stage": (None, None, {"draft_rule_pack_sha256": "f" * 64}),
            }
            for label, (row_override, report_override, stage_override) in cases.items():
                with self.subTest(label=label):
                    changed_drafts = copy.deepcopy(drafts)
                    changed_manifest = copy.deepcopy(manifest)
                    if row_override is not None:
                        changed_drafts[0] = row_override
                    report_path = workspace / "report/ad_decision_draft_report.json"
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    if report_override is not None:
                        report.update(report_override)
                    if stage_override is not None:
                        changed_manifest["stages"]["2_ads"].update(stage_override)
                    loads = finalize.json.loads
                    context = (
                        mock.patch.object(finalize.json, "loads", return_value=report)
                        if report_override is not None
                        else mock.patch.object(finalize.json, "loads", wraps=loads)
                    )
                    with context:
                        with self.assertRaisesRegex(ValueError, "stale|identity"):
                            finalize.validate_current_draft_provenance(
                                workspace,
                                draft_path,
                                changed_drafts,
                                candidates,
                                scan_report,
                                manifest=changed_manifest,
                            )

    def test_compiler_rejects_invalid_contracts_before_forming_decisions(self) -> None:
        item = candidate("AD-contract", "广告")
        malformed_truncation = copy.deepcopy(item)
        malformed_truncation["anchors_truncated"] = "false"
        with mock.patch.object(scan_identity, "validate_anchor_ids"):
            with self.assertRaisesRegex(ValueError, "anchors_truncated"):
                finalize.validate_candidate_contract([malformed_truncation])

        malformed_anchors = copy.deepcopy(item)
        malformed_anchors["anchors"] = "not-a-list"
        with mock.patch.object(scan_identity, "validate_anchor_ids"):
            with self.assertRaisesRegex(ValueError, "anchors must be a list"):
                finalize.validate_candidate_contract([malformed_anchors])

        invalid_plan_id = review(item, "delete", action="delete", edit_plan_id="")
        with self.assertRaisesRegex(ValueError, "edit_plan_id is invalid"):
            finalize.validate_review(invalid_plan_id, item, SCAN_ID)

        with self.assertRaisesRegex(ValueError, "formal provenance is missing"):
            finalize.compile_formal_decisions(
                [item],
                [review(item, "keep")],
                rule_drafts([item]),
                scan_id=SCAN_ID,
                provenance={},
            )

        def reject_delete(extra: dict[str, object], message: str) -> None:
            candidate_item = candidate("AD-delete-" + str(len(extra)), "广告")
            agent_review = review(candidate_item, "delete", action="delete", **extra)
            with self.assertRaisesRegex(ValueError, message):
                finalize.compile_formal_decisions(
                    [candidate_item],
                    [agent_review],
                    rule_drafts([candidate_item]),
                    scan_id=SCAN_ID,
                )

        reject_delete({"edit_plan_id": "EP-without-plan"}, "whole-block delete")
        reject_delete({"splice_strategy": "exact_segment"}, "requires a current scanner edit plan")
        guarded = candidate("AD-guarded", "广告")
        guarded["mutation_guard"] = "segment_review_required"
        scan_identity.attach_candidate_fingerprints([guarded])
        scan_identity.attach_anchor_ids([guarded])
        with self.assertRaisesRegex(ValueError, "mixed-content candidate"):
            finalize.compile_formal_decisions(
                [guarded],
                [review(guarded, "delete", action="delete")],
                rule_drafts([guarded]),
                scan_id=SCAN_ID,
            )

    def test_draft_provenance_rejects_deep_report_and_stage_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidates, scan_report = prepared_workspace(Path(directory))
            draft_path = workspace / "decisions/ads_decisions.draft.jsonl"
            drafts = common.load_jsonl(draft_path)
            manifest = common.load_manifest(workspace)
            report_path = workspace / "report/ad_decision_draft_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            original_read_text = Path.read_text

            def reject(
                changed_report: object,
                message: str,
                *,
                changed_scan_report: dict | None = None,
                changed_manifest: dict | None = None,
            ) -> None:
                def read_text(path: Path, *args, **kwargs) -> str:
                    if path == report_path:
                        return json.dumps(changed_report)
                    return original_read_text(path, *args, **kwargs)

                with mock.patch.object(Path, "read_text", new=read_text):
                    with self.assertRaisesRegex(ValueError, message):
                        finalize.validate_current_draft_provenance(
                            workspace,
                            draft_path,
                            drafts,
                            candidates,
                            changed_scan_report or scan_report,
                            manifest=changed_manifest or manifest,
                        )

            missing_profile = copy.deepcopy(report)
            missing_profile.pop("profile")
            reject(missing_profile, "profile path is missing")

            stale_manifest_hash = {**report, "review_pages_manifest_sha256": "0" * 64}
            reject(stale_manifest_hash, "review manifest hash is stale")

            no_review_manifest = copy.deepcopy(report)
            no_review_manifest.pop("review_pages_manifest")
            reject(no_review_manifest, "artifacts are not stage-owned")

            stale_count = {**report, "candidate_count": len(candidates) + 1}
            reject(stale_count, "candidate_count is stale")

            missing_scan_pack = {**scan_report, "scan_rule_pack_sha256": None}
            reject(report, "scan rule pack identity is missing", changed_scan_report=missing_scan_pack)

            stale_stage_counts = copy.deepcopy(manifest)
            stale_stage_counts["stages"]["2_ads"]["draft_keep_count"] = -1
            reject(report, "stage counts are stale", changed_manifest=stale_stage_counts)

            def unreadable_report(path: Path, *args, **kwargs) -> str:
                if path == report_path:
                    raise OSError("denied")
                return original_read_text(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", new=unreadable_report):
                with self.assertRaisesRegex(ValueError, "draft report cannot be read"):
                    finalize.validate_current_draft_provenance(
                        workspace,
                        draft_path,
                        drafts,
                        candidates,
                        scan_report,
                        manifest=manifest,
                    )

            reject([], "report must be a JSON object")

    def test_preserved_formal_report_rejects_missing_invalid_and_stale_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            output_path = workspace / "decisions/ads_decisions.jsonl"
            report_path = workspace / "report/ad_decision_formal_report.json"
            output_path.parent.mkdir(parents=True)
            report_path.parent.mkdir(parents=True)
            run_id = "a" * 32
            output_sha256 = "b" * 64
            reviews_sha256 = "c" * 64
            draft_sha256 = "d" * 64
            scan_id = "e" * 64
            candidate_set_sha256 = "f" * 64
            provenance = {key: key for key in finalize.FORMAL_IDENTITY_KEYS}

            def arguments(manifest: dict) -> None:
                finalize.load_preserved_formal_report(
                    workspace,
                    manifest,
                    output_path,
                    report_path,
                    output_sha256=output_sha256,
                    reviews_sha256=reviews_sha256,
                    draft_sha256=draft_sha256,
                    scan_id=scan_id,
                    candidate_set_sha256=candidate_set_sha256,
                    provenance=provenance,
                )

            with self.assertRaisesRegex(ValueError, "provenance is missing"):
                arguments({})

            def manifest_for(report_sha256: str) -> dict:
                output_relative = output_path.relative_to(workspace).as_posix()
                report_relative = report_path.relative_to(workspace).as_posix()
                return {
                    "stages": {
                        "2_ads": {
                            "status": "done",
                            "formal_run_id": run_id,
                            "formal_decisions": output_relative,
                            "formal_report": report_relative,
                            "formal_decisions_sha256": output_sha256,
                            "formal_reviews_sha256": reviews_sha256,
                            "formal_draft_sha256": draft_sha256,
                            "formal_report_sha256": report_sha256,
                            "scan_id": scan_id,
                            "candidate_set_sha256": candidate_set_sha256,
                        }
                    },
                    "artifacts": {
                        output_relative: {"run_id": run_id, "sha256": output_sha256},
                        report_relative: {"run_id": run_id, "sha256": report_sha256},
                    },
                }

            missing_sha256 = "0" * 64
            with self.assertRaisesRegex(ValueError, "report provenance is stale"):
                arguments(manifest_for(missing_sha256))

            report_path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "report cannot be read"):
                arguments(manifest_for(common.sha256_file(report_path)))

            report_path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "report must be a JSON object"):
                arguments(manifest_for(common.sha256_file(report_path)))

            report_path.write_text("{}", encoding="utf-8")
            stale_stage = manifest_for(common.sha256_file(report_path))
            stale_stage["stages"]["2_ads"]["status"] = "pending"
            with self.assertRaisesRegex(ValueError, "provenance is stale"):
                arguments(stale_stage)
            with self.assertRaisesRegex(ValueError, "provenance is stale"):
                arguments(manifest_for(common.sha256_file(report_path)))

    def test_finalize_propagates_complete_draft_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidates, scan_report = prepared_workspace(
                Path(directory),
                profile={"terms": ["主角"]},
            )
            report = finalize.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_agent_reviews.jsonl",
                "decisions/ads_decisions.draft.jsonl",
                "decisions/ads_decisions.jsonl",
            )
            rows = common.load_jsonl(workspace / "decisions/ads_decisions.jsonl")
            stage = common.load_manifest(workspace)["stages"]["2_ads"]
            expected = {
                "scan_rule_pack_sha256": scan_report["scan_rule_pack_sha256"],
                "draft_rule_pack_sha256": scan_identity.canonical_json_sha256(
                    scan_identity.build_draft_rule_pack()
                ),
                "profile": "meta/book_profile.json",
                **scan_identity.build_profile_identity(workspace / "meta/book_profile.json"),
            }
            self.assertEqual(len(rows), len(candidates))
            for field, value in expected.items():
                self.assertEqual(report[field], value)
                self.assertEqual(stage[field], value)
                for row in rows:
                    self.assertEqual(row[field], value)

    def test_run_uses_current_complete_scan_and_rejects_structure_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "anonymous.txt"
            source.write_text(
                "第一章 起点\n请访问 https://reader.example.com 获取更新。\n正文继续。\n",
                encoding="utf-8",
            )
            workspace = preprocess.run(source)
            parse_structure.run(workspace)
            scan_report = scan_ads.run(
                workspace,
                "versions/v1_preprocessed.txt",
                "candidates/ads.jsonl",
                12,
                10,
                10,
            )
            candidates = scan_identity.load_validated_pages(workspace, scan_report)
            self.assertTrue(candidates)
            common.write_jsonl(
                workspace / "decisions/ads_agent_reviews.jsonl",
                [
                    review(
                        item,
                        "keep",
                        scan_id=scan_report["scan_id"],
                        keep_basis=keep_basis(item),
                    )
                    for item in candidates
                ],
            )

            manifest_before_missing_draft = (workspace / "manifest.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "draft"):
                finalize.run(
                    workspace,
                    "candidates/ads_pages",
                    "decisions/ads_agent_reviews.jsonl",
                    "decisions/ads_decisions.draft.jsonl",
                    "decisions/ads_decisions.jsonl",
                )
            self.assertEqual(
                (workspace / "manifest.json").read_bytes(),
                manifest_before_missing_draft,
            )
            common.write_jsonl(
                workspace / "decisions/ads_decisions.draft.jsonl",
                [
                    {
                        "scan_id": scan_report["scan_id"],
                        "candidate_id": item["candidate_id"],
                        "candidate_fingerprint": item["candidate_fingerprint"],
                        "verdict": "uncertain",
                    }
                    for item in candidates
                ],
            )

            with self.assertRaisesRegex(ValueError, "draft"):
                finalize.run(
                    workspace,
                    "candidates/ads_pages",
                    "decisions/ads_agent_reviews.jsonl",
                    "decisions/ads_decisions.draft.jsonl",
                    "decisions/ads_decisions.jsonl",
                )

            make_ad_decisions.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_decisions.draft.jsonl",
                "meta/book_profile.json",
                True,
            )

            report = finalize.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_agent_reviews.jsonl",
                "decisions/ads_decisions.draft.jsonl",
                "decisions/ads_decisions.jsonl",
            )
            decisions = common.load_jsonl(workspace / "decisions/ads_decisions.jsonl")
            self.assertEqual(report["scan_id"], scan_report["scan_id"])
            self.assertEqual(len(decisions), len(candidates))
            self.assertEqual(
                [item["candidate_fingerprint"] for item in decisions],
                [item["candidate_fingerprint"] for item in candidates],
            )

            output = workspace / "decisions/ads_decisions.jsonl"
            before_output = output.read_bytes()
            chapters = workspace / "meta/chapters.json"
            chapters.write_text(chapters.read_text(encoding="utf-8") + " ", encoding="utf-8")
            before_manifest = (workspace / "manifest.json").read_bytes()
            with self.assertRaises(
                (scan_identity.ScanIdentityError, common.WorkspaceIdentityError)
            ):
                finalize.run(
                    workspace,
                    "candidates/ads_pages",
                    "decisions/ads_agent_reviews.jsonl",
                    "decisions/ads_decisions.draft.jsonl",
                    "decisions/ads_decisions.jsonl",
                )
            self.assertEqual(output.read_bytes(), before_output)
            self.assertEqual((workspace / "manifest.json").read_bytes(), before_manifest)

    def test_drafts_must_cover_every_candidate_exactly(self) -> None:
        first = candidate("AD-1", "广告")
        second = candidate("AD-2", "正文", 10)
        partial = [
            {
                "scan_id": SCAN_ID,
                "candidate_id": first["candidate_id"],
                "candidate_fingerprint": first["candidate_fingerprint"],
            }
        ]

        for drafts in ([], partial):
            with self.subTest(count=len(drafts)), self.assertRaisesRegex(
                ValueError, "complete candidate set"
            ):
                finalize.compile_formal_decisions(
                    [first, second],
                    [review(first), review(second)],
                    drafts,
                    scan_id=SCAN_ID,
                )

    def test_complete_reviews_preserve_identity_for_every_verdict(self) -> None:
        candidates = [
            candidate("AD-1", "广告", 0),
            candidate("AD-2", "正文", 10),
            candidate("AD-3", "混合", 20),
        ]
        reviews = [
            review(candidates[0], "delete", action="delete", splice_strategy="remove_paragraph"),
            review(candidates[1], "keep"),
            review(candidates[2], "uncertain"),
        ]
        drafts = [
            {
                "scan_id": SCAN_ID,
                "candidate_id": "AD-1",
                "candidate_fingerprint": candidates[0]["candidate_fingerprint"],
                "cluster_id": "ADF-1",
                "evidence": [{"type": "domain"}],
                "protected_terms": ["不应复制"],
            }
        ]

        decisions = finalize.compile_formal_decisions(
            candidates,
            reviews,
            rule_drafts(candidates, drafts),
            scan_id=SCAN_ID,
        )

        self.assertEqual(decisions[0]["anchors"], candidates[0]["anchors"])
        self.assertEqual(decisions[0]["action"], "delete")
        self.assertEqual(decisions[0]["cluster_id"], "ADF-1")
        self.assertNotIn("protected_terms", decisions[0])
        for item, decision in zip(candidates, decisions):
            self.assertEqual(decision["scan_id"], SCAN_ID)
            self.assertEqual(
                decision["candidate_fingerprint"], item["candidate_fingerprint"]
            )
            self.assertEqual(
                decision["anchor_ids"],
                [anchor["anchor_id"] for anchor in item["anchors"]],
            )
            self.assertEqual(
                decision["anchor_text_sha256s"],
                [
                    hashlib.sha256(anchor["original"].encode("utf-8")).hexdigest()
                    for anchor in item["anchors"]
                ],
            )
        self.assertNotIn("anchors", decisions[1])
        self.assertNotIn("action", decisions[1])
        self.assertNotIn("anchors", decisions[2])

    def test_reviews_must_cover_every_candidate_exactly(self) -> None:
        first = candidate("AD-1", "广告")
        second = candidate("AD-2", "正文", 10)
        cases = {
            "missing": [review(first)],
            "extra": [review(first), review(second), {**review(first), "candidate_id": "AD-X"}],
            "duplicate ID": [review(first), review(first), review(second)],
            "duplicate fingerprint": [
                review(first),
                {**review(second), "candidate_fingerprint": first["candidate_fingerprint"]},
            ],
        }
        for label, reviews in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                finalize.compile_formal_decisions(
                    [first, second], reviews, rule_drafts([first, second]), scan_id=SCAN_ID
                )

    def test_stale_scan_and_changed_candidate_fingerprint_are_rejected(self) -> None:
        item = candidate("AD-1", "广告")
        cases = (
            {**review(item), "scan_id": "b" * 64},
            {**review(item), "candidate_fingerprint": "c" * 64},
        )
        for stale in cases:
            with self.subTest(stale=stale), self.assertRaises(ValueError):
                finalize.compile_formal_decisions(
                    [item], [stale], rule_drafts([item]), scan_id=SCAN_ID
                )

        changed = copy.deepcopy(item)
        changed["anchors"][0]["original"] = "正文"
        scan_identity.attach_candidate_fingerprints([changed])
        scan_identity.attach_anchor_ids([changed])
        with self.assertRaises(ValueError):
            finalize.compile_formal_decisions(
                [changed], [review(item)], rule_drafts([changed]), scan_id=SCAN_ID
            )

    def test_review_fields_use_strict_types_and_enums(self) -> None:
        item = candidate("AD-1", "广告")
        invalid_overrides = (
            {"verdict": "DELETE"},
            {"confidence": True},
            {"confidence": "0.9"},
            {"confidence": math.nan},
            {"confidence": 1.01},
            {"reason": 7},
            {"reason": "  "},
            {"action": "replace"},
            {"action": 1},
            {"splice_strategy": "guess"},
            {"splice_strategy": 1},
            {"risk": "critical"},
            {"keep_basis": False},
            {"keep_basis": 1},
            {"keep_basis": "narrative"},
            {"unexpected": "field"},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                finalize.compile_formal_decisions(
                    [item],
                    [{**review(item), **overrides}],
                    rule_drafts([item]),
                    scan_id=SCAN_ID,
                )

        uncertain = review(item, "uncertain")
        for blockers in ("上下文不足", [], [""], [1]):
            with self.subTest(blockers=blockers), self.assertRaises(ValueError):
                finalize.compile_formal_decisions(
                    [item],
                    [{**uncertain, "blocking_reasons": blockers}],
                    rule_drafts([item]),
                    scan_id=SCAN_ID,
                )

    def test_non_mutating_verdict_rejects_action_and_splice_strategy(self) -> None:
        item = candidate("AD-1", "正文")
        for verdict in ("keep", "uncertain"):
            base = review(item, verdict)
            for override in ({"action": "delete"}, {"splice_strategy": "exact"}):
                with self.subTest(verdict=verdict, override=override), self.assertRaises(ValueError):
                    finalize.compile_formal_decisions(
                        [item],
                        [{**base, **override}],
                        rule_drafts([item]),
                        scan_id=SCAN_ID,
                    )

    def test_conflicting_keep_requires_an_explicit_supported_basis(self) -> None:
        item = candidate("AD-1", "故事中的纸条网址")
        draft_base = {
            "scan_id": SCAN_ID,
            "candidate_id": item["candidate_id"],
            "candidate_fingerprint": item["candidate_fingerprint"],
            "verdict": "delete",
        }
        conflicting_drafts = (
            {
                **draft_base,
                "evidence": [{"type": "automatic_delete_gate", "value": {}}],
            },
            {
                **draft_base,
                "evidence": [{"type": "family_similarity", "value": {}}],
            },
            {**draft_base, "promoted_from": ["AD-SEED"]},
        )
        for conflicting_draft in conflicting_drafts:
            with self.subTest(draft=conflicting_draft), self.assertRaisesRegex(
                ValueError, "keep_basis"
            ):
                finalize.compile_formal_decisions(
                    [item],
                    [review(item, "keep")],
                    rule_drafts([item], [conflicting_draft]),
                    scan_id=SCAN_ID,
                )
            for basis_type in (
                "plot_dependency",
                "narrative_context",
                "rule_false_positive",
            ):
                basis = keep_basis(item, basis_type)
                decisions = finalize.compile_formal_decisions(
                    [item],
                    [review(item, "keep", keep_basis=basis)],
                    rule_drafts([item], [conflicting_draft]),
                    scan_id=SCAN_ID,
                )
                self.assertEqual(decisions[0]["keep_basis"], basis)

        non_conflicting_drafts = (
            {**draft_base, "verdict": "keep"},
            {
                **draft_base,
                "verdict": "keep",
                "evidence": [{"type": "automatic_delete_gate", "value": {}}],
            },
            {
                **draft_base,
                "verdict": "uncertain",
                "evidence": [{"type": "family_similarity", "value": {}}],
            },
            {
                **draft_base,
                "evidence": [{"type": "unrelated_signal", "value": {}}],
            },
        )
        for ordinary_draft in non_conflicting_drafts:
            with self.subTest(draft=ordinary_draft):
                decisions = finalize.compile_formal_decisions(
                    [item],
                    [review(item, "keep")],
                    rule_drafts([item], [ordinary_draft]),
                    scan_id=SCAN_ID,
                )
                self.assertNotIn("keep_basis", decisions[0])
                basis = keep_basis(item, "plot_dependency")
                decisions = finalize.compile_formal_decisions(
                    [item],
                    [review(item, "keep", keep_basis=basis)],
                    rule_drafts([item], [ordinary_draft]),
                    scan_id=SCAN_ID,
                )
                self.assertEqual(decisions[0]["keep_basis"], basis)

        for verdict in ("delete", "uncertain"):
            with self.subTest(verdict=verdict), self.assertRaises(ValueError):
                finalize.compile_formal_decisions(
                    [item],
                    [review(item, verdict, keep_basis=keep_basis(item))],
                    rule_drafts([item], [conflicting_drafts[0]]),
                    scan_id=SCAN_ID,
                )

    def test_keep_basis_contract_is_closed_bound_and_normalized(self) -> None:
        item = candidate("AD-1", "第一处正文")
        item["anchors"].append(
            {
                "offset": 100,
                "end": 105,
                "line": 9,
                "original": "第二处正文",
                "prefix": "",
                "suffix": "",
                "chapter": {"index": 2, "title": "第二章"},
            }
        )
        item["occurrence_count"] = 2
        scan_identity.attach_candidate_fingerprints([item])
        scan_identity.attach_anchor_ids([item])

        basis = keep_basis(item, "rule_false_positive")
        reversed_basis = copy.deepcopy(basis)
        reversed_basis["reviewed_occurrences"].reverse()
        decisions = finalize.compile_formal_decisions(
            [item],
            [review(item, "keep", keep_basis=reversed_basis)],
            rule_drafts([item]),
            scan_id=SCAN_ID,
        )
        self.assertEqual(decisions[0]["occurrence_count"], 2)
        self.assertEqual(decisions[0]["keep_basis"], basis)

        def changed(mutator: object) -> object:
            value = copy.deepcopy(basis)
            assert callable(mutator)
            mutator(value)
            return value

        invalid_bases = (
            "narrative_context",
            False,
            [],
            changed(lambda value: value.update(extra=True)),
            changed(lambda value: value.__setitem__(1, True)),
            changed(lambda value: value.update(schema="cml.keep-basis.v0")),
            changed(lambda value: value.update(type="narrative")),
            changed(lambda value: value.update(reviewed_occurrences={})),
            changed(lambda value: value["reviewed_occurrences"][0].update(extra=True)),
            changed(lambda value: value["reviewed_occurrences"][0].__setitem__(1, True)),
            changed(lambda value: value["reviewed_occurrences"][0].update(anchor_id="")),
            changed(lambda value: value["reviewed_occurrences"][0].update(text_sha256="A" * 64)),
            changed(lambda value: value["reviewed_occurrences"].append(copy.deepcopy(value["reviewed_occurrences"][0]))),
            changed(lambda value: value["reviewed_occurrences"].pop()),
            changed(lambda value: value["reviewed_occurrences"].append({"anchor_id": "A-extra", "text_sha256": "b" * 64})),
            changed(lambda value: value.update(occurrence_coverage_sha256="B" * 64)),
            changed(lambda value: value.update(occurrence_coverage_sha256="b" * 64)),
            changed(lambda value: value.update(note=1)),
            changed(lambda value: value.update(note="")),
            changed(lambda value: value.update(note="   ")),
            changed(lambda value: value.update(note="说" * 501)),
        )
        for invalid in invalid_bases:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                finalize.compile_formal_decisions(
                    [item],
                    [review(item, "keep", keep_basis=invalid)],
                    rule_drafts([item]),
                    scan_id=SCAN_ID,
                )

        with self.assertRaisesRegex(ValueError, "re-review"):
            finalize.compile_formal_decisions(
                [item],
                [review(item, "keep", keep_basis="narrative_context")],
                rule_drafts([item]),
                scan_id=SCAN_ID,
            )

    def test_occurrence_contract_fails_closed_on_count_and_truncation_drift(self) -> None:
        base = candidate("AD-1", "正文")
        mutations = (
            lambda item: item.pop("occurrence_count"),
            lambda item: item.update(occurrence_count=True),
            lambda item: item.update(occurrence_count=0),
            lambda item: item.update(occurrence_count=2),
            lambda item: item.update(anchors_truncated=True, occurrence_count=1),
        )
        for mutate in mutations:
            item = copy.deepcopy(base)
            mutate(item)
            with self.subTest(item=item), self.assertRaises(ValueError):
                finalize.compile_formal_decisions(
                    [item],
                    [review(item, "keep")],
                    rule_drafts([item]),
                    scan_id=SCAN_ID,
                )

        truncated = copy.deepcopy(base)
        truncated.update(anchors_truncated=True, occurrence_count=2)
        scan_identity.attach_candidate_fingerprints([truncated])
        scan_identity.attach_anchor_ids([truncated])
        with self.assertRaisesRegex(ValueError, "truncated"):
            finalize.compile_formal_decisions(
                [truncated],
                [review(truncated, "keep", keep_basis=keep_basis(truncated))],
                rule_drafts([truncated]),
                scan_id=SCAN_ID,
            )

    def test_inline_splice_strategies_require_subspan_anchors(self) -> None:
        item = candidate("AD-1", "正文中的站外推广")
        for strategy in ("remove_inline_join", "remove_inline_keep_punct"):
            with self.subTest(strategy=strategy), self.assertRaisesRegex(
                ValueError, "splice_strategy is invalid"
            ):
                finalize.compile_formal_decisions(
                    [item],
                    [
                        review(
                            item,
                            "delete",
                            action="delete",
                            splice_strategy=strategy,
                        )
                    ],
                    rule_drafts([item]),
                    scan_id=SCAN_ID,
                )

    def test_truncated_candidate_cannot_become_executable_delete(self) -> None:
        item = candidate("AD-1", "广告")
        item["anchors_truncated"] = True
        item["occurrence_count"] = 2
        scan_identity.attach_candidate_fingerprints([item])
        scan_identity.attach_anchor_ids([item])

        with self.assertRaisesRegex(ValueError, "truncated"):
            finalize.compile_formal_decisions(
                [item],
                [review(item, "delete")],
                rule_drafts([item]),
                scan_id=SCAN_ID,
            )

        for verdict in ("keep", "uncertain"):
            decisions = finalize.compile_formal_decisions(
                [item],
                [review(item, verdict)],
                rule_drafts([item]),
                scan_id=SCAN_ID,
            )
            self.assertEqual(decisions[0]["candidate_fingerprint"], item["candidate_fingerprint"])
            self.assertTrue(decisions[0]["anchors_truncated"])
            self.assertNotIn("anchors", decisions[0])

    def test_anchor_contract_rejects_duplicates_invalid_ranges_and_context_types(self) -> None:
        base = candidate("AD-1", "广告")
        mutations = (
            lambda item: item["anchors"].append(copy.deepcopy(item["anchors"][0])),
            lambda item: item["anchors"][0].update(offset=-1),
            lambda item: item["anchors"][0].update(offset=True),
            lambda item: item["anchors"][0].update(end=99),
            lambda item: item["anchors"][0].update(original=1),
            lambda item: item["anchors"][0].update(prefix=1),
            lambda item: item["anchors"][0].update(suffix=None),
            lambda item: item["anchors"][0].update(chapter={"index": "1", "title": "第一章"}),
            lambda item: item["anchors"][0].update(anchor_id="forged"),
        )
        for mutate in mutations:
            item = copy.deepcopy(base)
            mutate(item)
            with self.subTest(item=item), self.assertRaises(ValueError):
                finalize.compile_formal_decisions(
                    [item],
                    [review(item, "keep")],
                    rule_drafts([item]),
                    scan_id=SCAN_ID,
                )

    def test_non_chapter_locator_kinds_from_structure_are_accepted(self) -> None:
        for kind, index, title in (
            ("body", 1, "正文"),
            ("front_matter", 0, "前置内容"),
            ("fallback_chunk", 1, "Fallback chunk 001"),
        ):
            item = candidate("AD-1", "广告")
            item["anchors"][0]["chapter"] = None
            item["anchors"][0]["locator"] = {
                "kind": kind,
                "index": index,
                "title": title,
            }
            scan_identity.attach_candidate_fingerprints([item])
            scan_identity.attach_anchor_ids([item])

            with self.subTest(kind=kind):
                decisions = finalize.compile_formal_decisions(
                    [item],
                    [review(item, "keep")],
                    rule_drafts([item]),
                    scan_id=SCAN_ID,
                )
                self.assertEqual(decisions[0]["candidate_id"], "AD-1")

    def test_uncertain_requires_machine_readable_blocker(self) -> None:
        item = candidate("AD-1", "混合段落")
        invalid = review(item, "uncertain")
        invalid.pop("blocking_reasons")
        with self.assertRaisesRegex(ValueError, "blocking_reasons"):
            finalize.compile_formal_decisions(
                [item], [invalid], rule_drafts([item]), scan_id=SCAN_ID
            )

    def test_reported_candidate_count_reads_full_scan_total(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "report").mkdir()
            (workspace / "report" / "ads_scan_report.json").write_text(
                json.dumps({"summary": {"total_candidate_count": 111}}),
                encoding="utf-8",
            )

            self.assertEqual(finalize.reported_candidate_count(workspace), 111)

    def test_idempotent_recompile_does_not_downgrade_applied_stage(self) -> None:
        self.assertFalse(finalize.should_mark_formal_ready(False, "done"))
        self.assertTrue(finalize.should_mark_formal_ready(True, "done"))
        self.assertTrue(finalize.should_mark_formal_ready(False, "formal_decisions_ready"))

    def test_unchanged_recompile_after_apply_is_a_true_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "anonymous.txt"
            ad = "站外广告提示"
            source.write_text(f"第一章 起点\n匿名正文。\n{ad}\n", encoding="utf-8")
            workspace = preprocess.run(source)
            input_path = workspace / "versions/v1_preprocessed.txt"
            offset = input_path.read_text(encoding="utf-8").index(ad)
            formalize_ads(
                workspace,
                [{"original": ad, "offset": offset}],
                verdict="delete",
                action="delete",
            )
            decisions_path = workspace / "decisions/ads_decisions.jsonl"
            report_path = workspace / "report/ad_decision_formal_report.json"
            apply_decisions.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )
            before = {
                "manifest": (workspace / "manifest.json").read_bytes(),
                "decisions": decisions_path.read_bytes(),
                "report": report_path.read_bytes(),
            }

            returned = finalize.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_agent_reviews.jsonl",
                "decisions/ads_decisions.draft.jsonl",
                "decisions/ads_decisions.jsonl",
            )

            self.assertEqual((workspace / "manifest.json").read_bytes(), before["manifest"])
            self.assertEqual(decisions_path.read_bytes(), before["decisions"])
            self.assertEqual(report_path.read_bytes(), before["report"])
            self.assertEqual(
                returned,
                json.loads(before["report"].decode("utf-8")),
            )
            apply_decisions.validate_formal_ad_provenance(
                workspace,
                input_path,
                decisions_path,
                common.load_jsonl(decisions_path),
                manifest=common.load_manifest(workspace),
                require_ready=False,
            )


if __name__ == "__main__":
    unittest.main()
