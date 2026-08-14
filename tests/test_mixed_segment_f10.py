from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ad_decision_policy  # noqa: E402
import ad_review_protocol  # noqa: E402
import apply_decisions  # noqa: E402
import build_review_html  # noqa: E402
import common  # noqa: E402
import dry_run  # noqa: E402
import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import rollback  # noqa: E402
import scan_ads  # noqa: E402
import scan_identity  # noqa: E402
import verify  # noqa: E402


MIXED = (
    "“好有灵性的战宠！”  作者有话说：  防止失联,请记住本站备用域名："
    "8  0 8 0 t x t . c  o m"
)
NARRATIVE = "“好有灵性的战宠！”"


class MixedSegmentF10Tests(unittest.TestCase):
    def make_scanned_workspace(self, root: Path, text: str = MIXED + "\n") -> tuple[Path, dict]:
        source = root / "mixed.txt"
        source.write_text(text, encoding="utf-8")
        workspace = preprocess.run(source, encoding="utf-8")
        parse_structure.run(workspace)
        report = scan_ads.run(
            workspace,
            "versions/v1_preprocessed.txt",
            "candidates/ads.jsonl",
            12,
            300,
            120,
        )
        candidates = scan_identity.load_validated_pages(workspace, report)
        mixed = next(item for item in candidates if item.get("edit_plan"))
        return workspace, mixed

    def review_and_draft(self, candidate: dict, scan_id: str) -> tuple[dict, dict]:
        review = {
            "scan_id": scan_id,
            "candidate_id": candidate["candidate_id"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "verdict": "delete",
            "confidence": 0.99,
            "reason": "只删除扫描器锁定的外部推广后缀",
            "splice_strategy": "exact_segment",
            "edit_plan_id": candidate["edit_plan"]["edit_plan_id"],
        }
        draft = {
            "scan_id": scan_id,
            "candidate_id": candidate["candidate_id"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "verdict": "uncertain",
            "risk": "high",
            "splice_strategy": "exact_segment",
            "edit_plan_id": candidate["edit_plan"]["edit_plan_id"],
            "anchors_truncated": False,
        }
        return review, draft

    def formalize_real_segment(
        self,
        workspace: Path,
        candidate: dict,
    ) -> dict:
        report = self.read_json(workspace / "report/ads_scan_report.json")
        make_ad_decisions.run(
            workspace,
            "candidates/ads_pages",
            "decisions/ads_decisions.draft.jsonl",
            "meta/book_profile.json",
            True,
        )
        reviews = []
        candidates = scan_identity.load_validated_pages(workspace, report)
        for item in candidates:
            if item["candidate_id"] == candidate["candidate_id"]:
                review, _draft = self.review_and_draft(item, report["scan_id"])
                reviews.append(review)
            else:
                reviews.append(
                    {
                        "scan_id": report["scan_id"],
                        "candidate_id": item["candidate_id"],
                        "candidate_fingerprint": item["candidate_fingerprint"],
                        "verdict": "keep",
                        "confidence": 0.99,
                        "reason": "test fixture keeps unrelated candidate",
                    }
                )
        common.write_jsonl(
            workspace / "decisions/ads_agent_reviews.jsonl",
            reviews,
        )
        finalize_ad_decisions.run(
            workspace,
            "candidates/ads_pages",
            "decisions/ads_agent_reviews.jsonl",
            "decisions/ads_decisions.draft.jsonl",
            "decisions/ads_decisions.jsonl",
        )
        return next(
            item
            for item in common.load_jsonl(workspace / "decisions/ads_decisions.jsonl")
            if item["candidate_id"] == candidate["candidate_id"]
        )

    @staticmethod
    def read_json(path: Path) -> dict:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise AssertionError("expected JSON object")
        return value

    def test_scanner_builds_a_bound_suffix_plan_and_keeps_narrative_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _workspace, candidate = self.make_scanned_workspace(Path(directory))

        plan = ad_decision_policy.normalize_edit_plan(candidate["edit_plan"], candidate)
        occurrence = plan["occurrence_plans"][0]
        preview = ad_decision_policy.edit_plan_preview(candidate, plan)[0]
        self.assertEqual(candidate["mutation_guard"], "segment_review_required")
        self.assertEqual(occurrence["boundary_kind"], "external_suffix")
        self.assertEqual(preview["keep_text"], NARRATIVE)
        self.assertIn("备用域名", preview["delete_text"])
        self.assertEqual(preview["after_text"], NARRATIVE)

    def test_supported_prefix_and_fullwidth_standalone_clause_shapes_are_exact(self) -> None:
        cases = (
            (
                "请访问https://reader.example.com获取更新。她推开门。",
                "external_prefix",
                "她推开门。",
            ),
            (
                "他停步；请访问https://reader.example.com获取更新；她回头。",
                "standalone_clause",
                "他停步；她回头。",
            ),
        )
        for text, boundary_kind, after in cases:
            with self.subTest(boundary_kind=boundary_kind):
                proposal = ad_decision_policy.propose_edit_segments(text)
                self.assertIsNotNone(proposal)
                self.assertEqual(proposal[0], boundary_kind)
                kept = "".join(
                    text[start:end]
                    for kind, start, end in proposal[1]
                    if kind == "narrative"
                )
                self.assertEqual(kept, after)

    def test_complex_quoted_middle_reference_and_truncated_occurrences_get_no_plan(self) -> None:
        quoted = "他念道“请访问https://reader.example.com获取更新”，然后收起纸条。"
        self.assertIsNone(ad_decision_policy.propose_edit_segments(quoted))
        quoted_continuity = "他说：“防止失联，请记住备用域名foo.example.com”"
        self.assertIsNone(
            ad_decision_policy.propose_edit_segments(quoted_continuity)
        )
        candidate = {
            "candidate_id": "AD-X",
            "candidate_fingerprint": "a" * 64,
            "occurrence_count": 2,
            "anchors_truncated": True,
            "anchors": [
                {
                    "anchor_id": "AN-X",
                    "offset": 0,
                    "end": len(MIXED),
                    "original": MIXED,
                    "prefix": "",
                    "suffix": "",
                }
            ],
        }
        self.assertIsNone(ad_decision_policy.build_edit_plan(candidate))

    def test_full_promotional_line_is_not_misclassified_as_mixed_narrative(self) -> None:
        for value in (
            "站外更新提示：请访问 https://reader.example.com/update 获取后续内容。",
            "下载提示：请访问 https://reader.example.com/file 获取匿名文件。",
        ):
            with self.subTest(value=value):
                self.assertIsNone(ad_decision_policy.propose_edit_segments(value))

        mixed_hint = "她合上书。提示：请访问 https://reader.example.com/update 获取后续内容。"
        proposal = ad_decision_policy.propose_edit_segments(mixed_hint)
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal[0], "external_suffix")
        self.assertEqual(
            "".join(
                mixed_hint[start:end]
                for kind, start, end in proposal[1]
                if kind == "narrative"
            ),
            "她合上书。",
        )

    def test_edit_plan_schema_rejects_parent_hash_range_overlap_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _workspace, candidate = self.make_scanned_workspace(Path(directory))
        cases = []
        bad_parent = copy.deepcopy(candidate["edit_plan"])
        bad_parent["occurrence_plans"][0]["parent"]["text_sha256"] = "0" * 64
        cases.append(bad_parent)
        overlap = copy.deepcopy(candidate["edit_plan"])
        overlap["occurrence_plans"][0]["segments"][1]["relative_start"] -= 1
        cases.append(overlap)
        unknown = copy.deepcopy(candidate["edit_plan"])
        unknown["occurrence_plans"][0]["free_offset"] = 3
        cases.append(unknown)
        overflow = copy.deepcopy(candidate["edit_plan"])
        overflow["occurrence_plans"][0]["segments"][-1]["relative_end"] += 1
        cases.append(overflow)
        context_drift = copy.deepcopy(candidate["edit_plan"])
        context_drift["occurrence_plans"][0]["parent"]["prefix_sha256"] = "0" * 64
        cases.append(context_drift)
        boundary_drift = copy.deepcopy(candidate["edit_plan"])
        boundary_drift["occurrence_plans"][0]["boundary_kind"] = "external_prefix"
        cases.append(boundary_drift)
        joiner_drift = copy.deepcopy(candidate["edit_plan"])
        joiner_drift["occurrence_plans"][0]["joiner"] = " "
        cases.append(joiner_drift)

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ad_decision_policy.normalize_edit_plan(value, candidate)

    def test_edit_plan_id_changes_with_candidate_fingerprint_and_old_review_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _workspace, candidate = self.make_scanned_workspace(Path(directory))
        old_plan = copy.deepcopy(candidate["edit_plan"])
        rebound = copy.deepcopy(candidate)
        rebound["candidate_fingerprint"] = "b" * 64
        rebound["edit_plan"] = copy.deepcopy(old_plan)

        new_plan = ad_decision_policy.bind_edit_plan(rebound)

        self.assertIsNotNone(new_plan)
        self.assertNotEqual(new_plan["edit_plan_id"], old_plan["edit_plan_id"])
        with self.assertRaises(ValueError):
            ad_decision_policy.normalize_edit_plan(old_plan, rebound)

    def test_compiler_accepts_only_edit_plan_id_and_copies_current_ledger_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory))
            report = self.read_json(workspace / "report/ads_scan_report.json")
            scan_id = report["scan_id"]
        review, draft = self.review_and_draft(candidate, scan_id)
        formal = finalize_ad_decisions.compile_formal_decisions(
            [candidate], [review], [draft], scan_id=scan_id
        )[0]

        self.assertEqual(formal["splice_strategy"], "exact_segment")
        self.assertEqual(formal["edit_plan"], candidate["edit_plan"])
        self.assertNotIn("offset", review)

        for mutation in ("wrong-id", "hand-offset"):
            tampered = copy.deepcopy(review)
            if mutation == "wrong-id":
                tampered["edit_plan_id"] = "EP-" + "0" * 64
            else:
                tampered["offset"] = 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                finalize_ad_decisions.compile_formal_decisions(
                    [candidate], [tampered], [draft], scan_id=scan_id
                )

    def test_apply_preflights_the_parent_then_deletes_only_the_bound_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory))
            report = self.read_json(workspace / "report/ads_scan_report.json")
            review, draft = self.review_and_draft(candidate, report["scan_id"])
            formal = finalize_ad_decisions.compile_formal_decisions(
                [candidate], [review], [draft], scan_id=report["scan_id"]
            )
            source = (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8")

        operations = apply_decisions.collect_operations(
            source, formal, Path("unused.jsonl"), "ads"
        )
        cleaned = apply_decisions.apply_operations(source, operations)
        self.assertEqual(cleaned, NARRATIVE + "\n")
        self.assertEqual(operations[0].original, MIXED[len(NARRATIVE) :])
        self.assertEqual(operations[0].edit_plan_id, candidate["edit_plan"]["edit_plan_id"])

        stale = copy.deepcopy(formal)
        stale[0]["edit_plan"]["occurrence_plans"][0]["parent"]["text_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            apply_decisions.collect_operations(source, stale, Path("unused.jsonl"), "ads")

    def test_long_line_keeps_its_guard_but_allows_a_bound_suffix_segment(self) -> None:
        """A long mixed line must not force either whole-line deletion or retention."""
        text = NARRATIVE * 50 + MIXED[len(NARRATIVE) :] + "\n"
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory), text)
            self.assertEqual(candidate.get("mutation_guard"), "long_line_mixed_content")
            self.assertIsNotNone(candidate.get("edit_plan"))
            self.assertEqual(candidate["edit_plan"]["occurrence_plans"][0]["boundary_kind"], "external_suffix")

            decision = self.formalize_real_segment(workspace, candidate)
            source = (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8")

        operations = apply_decisions.collect_operations(source, [decision], Path("unused.jsonl"), "ads")
        cleaned = apply_decisions.apply_operations(source, operations)
        self.assertEqual(decision["splice_strategy"], "exact_segment")
        self.assertEqual(cleaned, NARRATIVE * 50 + "\n")

    def test_overlapping_segment_plans_stop_the_whole_operation_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory))
            report = self.read_json(workspace / "report/ads_scan_report.json")
            review, draft = self.review_and_draft(candidate, report["scan_id"])
            first = finalize_ad_decisions.compile_formal_decisions(
                [candidate], [review], [draft], scan_id=report["scan_id"]
            )[0]
            source = (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8")
        second = copy.deepcopy(first)
        second["candidate_id"] = "AD-OVERLAP"
        second["candidate_fingerprint"] = "c" * 64
        second["anchors"][0]["anchor_id"] = "AN-OVERLAP"
        second["anchor_ids"] = ["AN-OVERLAP"]
        second["edit_plan"]["candidate_id"] = second["candidate_id"]
        second["edit_plan"]["candidate_fingerprint"] = second["candidate_fingerprint"]
        second["edit_plan"]["occurrence_plans"][0]["anchor_id"] = "AN-OVERLAP"
        ad_decision_policy.bind_edit_plan(second)
        second["edit_plan_id"] = second["edit_plan"]["edit_plan_id"]

        with self.assertRaisesRegex(ValueError, "overlapping"):
            apply_decisions.collect_operations(
                source, [first, second], Path("unused.jsonl"), "ads"
            )

    def test_verify_replays_plan_and_detects_any_narrative_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory))
            report = self.read_json(workspace / "report/ads_scan_report.json")
            review, draft = self.review_and_draft(candidate, report["scan_id"])
            formal = finalize_ad_decisions.compile_formal_decisions(
                [candidate], [review], [draft], scan_id=report["scan_id"]
            )
            source = (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8")
        operations = apply_decisions.collect_operations(source, formal, Path("unused"), "ads")
        output = apply_decisions.apply_operations(source, operations)
        logs = [
            {
                "candidate_id": operation.candidate_id,
                "candidate_fingerprint": operation.candidate_fingerprint,
                "scan_id": operation.scan_id,
                "anchor_id": operation.anchor_id,
                "action": operation.action,
                "strategy": operation.strategy,
                "start": operation.start,
                "end": operation.end,
                "original": operation.original,
                "replacement": operation.replacement,
                "edit_plan_id": operation.edit_plan_id,
                "parent_start": operation.parent_start,
                "parent_end": operation.parent_end,
                "expected_after_sha256": operation.expected_after_sha256,
            }
            for operation in operations
        ]

        self.assertEqual(verify.verify_segment_plan_replay(source, output, formal, logs), [])
        self.assertTrue(
            verify.verify_segment_plan_replay(source, "“坏”\n", formal, logs)
        )

    def test_chapter_rollback_filters_plan_occurrences_with_parent_anchors(self) -> None:
        source = "第一章\n" + MIXED + "\n第二章\n" + MIXED + "\n"
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory), source)
            report = self.read_json(workspace / "report/ads_scan_report.json")
            review, draft = self.review_and_draft(candidate, report["scan_id"])
            decision = finalize_ad_decisions.compile_formal_decisions(
                [candidate], [review], [draft], scan_id=report["scan_id"]
            )[0]
        filtered, restored = rollback._filter_chapter([decision], 1)
        self.assertEqual(len(restored), 1)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(filtered[0]["anchors"]), 1)
        self.assertEqual(len(filtered[0]["edit_plan"]["occurrence_plans"]), 1)
        ad_decision_policy.normalize_edit_plan(
            filtered[0]["edit_plan"],
            {
                **candidate,
                "occurrence_count": 1,
                "anchors": filtered[0]["anchors"],
                "anchors_truncated": False,
            },
        )

    def test_dry_run_exposes_keep_delete_and_after_previews(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory))
            report = self.read_json(workspace / "report/ads_scan_report.json")
            review, draft = self.review_and_draft(candidate, report["scan_id"])
            decision = finalize_ad_decisions.compile_formal_decisions(
                [candidate], [review], [draft], scan_id=report["scan_id"]
            )[0]
        summary = dry_run.module_summary(
            [candidate], [decision], "ads", dry_run.MODULES["ads"], True, True
        )
        preview = summary["segment_edit_previews"][0]
        self.assertEqual(preview["keep_text"], NARRATIVE)
        self.assertIn("备用域名", preview["delete_text"])
        self.assertEqual(preview["after_text"], NARRATIVE)

    def test_review_html_uses_shared_python_delete_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory))
        with mock.patch.object(
            ad_decision_policy,
            "delete_eligibility",
            wraps=ad_decision_policy.delete_eligibility,
        ) as shared:
            item = build_review_html.review_candidate(
                workspace, "ads", candidate, None, None, None
            )
        self.assertTrue(shared.called)
        self.assertFalse(item["delete_allowed"])
        self.assertTrue(item["segment_delete_allowed"])
        self.assertEqual(item["keep_preview"], NARRATIVE)

    def test_mixed_review_action_labels_are_explicit(self) -> None:
        script = (ROOT / "assets/review/review.js").read_text(encoding="utf-8")
        self.assertIn("保留整段", script)
        self.assertIn("只删除标出的广告片段", script)
        self.assertIn("暂不判断", script)
        self.assertNotIn("只删标出片段", script)

    def test_agent_projection_has_plan_identity_and_bounded_previews_but_no_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory))
            self.formalize_real_segment(workspace, candidate)
            manifest = self.read_json(
                workspace / "candidates/ads_review_pages/manifest.json"
            )
            page = self.read_json(workspace / manifest["pages"][0]["file"])
        record = next(
            item
            for item in page["records"]
            if item.get("candidate_id") == candidate["candidate_id"]
        )
        self.assertEqual(
            record["segment_identity"]["edit_plan_id"],
            candidate["edit_plan"]["edit_plan_id"],
        )
        occurrence = record["occurrences"][0]
        self.assertEqual(occurrence["after_text_preview"], NARRATIVE)
        serialized = json.dumps(record, ensure_ascii=False)
        for forbidden in ('"offset"', '"relative_start"', '"relative_end"', '"parent"'):
            self.assertNotIn(forbidden, serialized)

    def test_segment_group_verdict_binds_and_expands_each_member_edit_plan_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory))
            report = self.read_json(workspace / "report/ads_scan_report.json")
            _review, draft = self.review_and_draft(candidate, report["scan_id"])
            protocol_identity = {"schema_version": 1, "fixture": "segment-group"}
            identity = {
                "scan_id": report["scan_id"],
                "candidate_set_sha256": scan_identity.candidate_set_sha256([candidate]),
                "scan_rule_pack_sha256": "a" * 64,
                "draft_rule_pack_sha256": "b" * 64,
                "profile_present": False,
                "book_profile_sha256": "c" * 64,
                "book_profile_file_sha256": None,
                "review_protocol_identity": protocol_identity,
                "review_protocol_identity_sha256": ad_review_protocol.canonical_sha256(
                    protocol_identity
                ),
            }
            source = (workspace / "versions/v1_preprocessed.txt").read_text(
                encoding="utf-8"
            )
            projection = ad_review_protocol.build_review_projection(
                [candidate], [draft], source, identity
            )
            for relative, encoded in ad_review_protocol.projection_artifacts(
                projection
            ).items():
                path = workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(encoded)
            manifest_path = workspace / "candidates/ads_review_pages/manifest.json"
            manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            reviews_dir = workspace / "decisions/ads_agent_reviews/pages"
            reviews_dir.mkdir(parents=True, exist_ok=True)
            group = projection["manifest"]["review_groups"][0]
            self.assertEqual(
                group["member_edit_plan_ids"],
                {candidate["candidate_id"]: candidate["edit_plan"]["edit_plan_id"]},
            )
            for entry in projection["manifest"]["pages"]:
                records = [
                    {
                        "record_type": "page_attestation",
                        "schema": ad_review_protocol.REVIEW_ATTESTATION_SCHEMA,
                        "page_number": entry["page_number"],
                        "page_sha256": entry["sha256"],
                        "manifest_sha256": manifest_sha256,
                        "projection_set_sha256": projection["manifest"][
                            "projection_set_sha256"
                        ],
                        "occurrence_coverage_sha256": entry[
                            "occurrence_coverage_sha256"
                        ],
                    }
                ]
                if entry["page_number"] == 1:
                    records.append(
                        {
                            "record_type": "group_verdict",
                            "review_group_id": group["review_group_id"],
                            "member_candidate_ids": group["member_candidate_ids"],
                            "member_fingerprints": group["member_fingerprints"],
                            "member_coverage_sha256": group["member_coverage_sha256"],
                            "member_edit_plan_ids": group["member_edit_plan_ids"],
                            "verdict": "delete",
                            "confidence": 0.99,
                            "reason": "each segment plan was reviewed",
                            "risk": "high",
                            "action": "delete",
                            "splice_strategy": "exact_segment",
                        }
                    )
                (reviews_dir / f"page_{entry['page_number']:04d}.jsonl").write_bytes(
                    ad_review_protocol.jsonl_bytes(records)
                )

            merged = ad_review_protocol.merge_review_pages(workspace, [candidate])

            self.assertEqual(merged[0]["edit_plan_id"], candidate["edit_plan"]["edit_plan_id"])
            self.assertNotIn("member_edit_plan_ids", merged[0])
            page_path = reviews_dir / "page_0001.jsonl"
            tampered_records = common.load_jsonl(page_path)
            tampered_records[1]["member_edit_plan_ids"] = {
                candidate["candidate_id"]: "EP-" + "0" * 64
            }
            page_path.write_bytes(ad_review_protocol.jsonl_bytes(tampered_records))
            with self.assertRaisesRegex(
                ad_review_protocol.ReviewProtocolError,
                "edit plan coverage drifted",
            ):
                ad_review_protocol.merge_review_pages(workspace, [candidate])

            candidate_verdict = {
                "record_type": "candidate_verdict",
                "review_group_id": group["review_group_id"],
                "candidate_id": candidate["candidate_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "verdict": "delete",
                "confidence": 0.99,
                "reason": "reviewed exact segment",
                "risk": "high",
                "action": "delete",
                "splice_strategy": "exact_segment",
                "edit_plan_id": "EP-" + "0" * 64,
            }
            page_path.write_bytes(
                ad_review_protocol.jsonl_bytes(
                    [tampered_records[0], candidate_verdict]
                )
            )
            with self.assertRaisesRegex(
                ad_review_protocol.ReviewProtocolError,
                "edit plan identity is stale",
            ):
                ad_review_protocol.merge_review_pages(workspace, [candidate])

            candidate_verdict["edit_plan_id"] = candidate["edit_plan"]["edit_plan_id"]
            page_path.write_bytes(
                ad_review_protocol.jsonl_bytes(
                    [tampered_records[0], candidate_verdict]
                )
            )
            merged = ad_review_protocol.merge_review_pages(workspace, [candidate])
            self.assertEqual(merged[0]["edit_plan_id"], candidate["edit_plan"]["edit_plan_id"])

    def test_full_provenance_apply_and_point_rollback_round_trip_segment_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory))
            decision = self.formalize_real_segment(workspace, candidate)
            summary = apply_decisions.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )
            self.assertEqual(summary["operation_count"], 1)
            self.assertEqual(
                (workspace / "versions/v2_ads_removed.txt").read_text(encoding="utf-8"),
                NARRATIVE + "\n",
            )
            rollback.rollback_point(workspace, "ads", decision["candidate_id"])
            self.assertEqual(
                (workspace / "versions/v2_ads_removed.txt").read_text(encoding="utf-8"),
                MIXED + "\n",
            )

    def test_parent_drift_fails_before_apply_writes_any_output_or_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, candidate = self.make_scanned_workspace(Path(directory))
            self.formalize_real_segment(workspace, candidate)
            source = workspace / "versions/v1_preprocessed.txt"
            source.write_text("漂移" + source.read_text(encoding="utf-8"), encoding="utf-8")
            output = workspace / "versions/v2_ads_removed.txt"
            operations = workspace / "logs/operations.jsonl"
            output.write_text("旧输出", encoding="utf-8")
            common.write_jsonl(operations, [{"run_id": "old", "anchor_id": "old"}])
            before_output = output.read_bytes()
            before_operations = operations.read_bytes()
            before_manifest = (workspace / "manifest.json").read_bytes()

            with self.assertRaises(ValueError):
                apply_decisions.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "decisions/ads_decisions.jsonl",
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                )

            self.assertEqual(output.read_bytes(), before_output)
            self.assertEqual(operations.read_bytes(), before_operations)
            self.assertEqual((workspace / "manifest.json").read_bytes(), before_manifest)


if __name__ == "__main__":
    unittest.main()
