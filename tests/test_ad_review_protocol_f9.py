from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ad_review_protocol as protocol  # noqa: E402
import common  # noqa: E402
import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_ads  # noqa: E402
import scan_identity  # noqa: E402


def identity() -> dict:
    implementation = {"schema_version": 1, "fixture": "review-protocol"}
    return {
        "scan_id": "a" * 64,
        "candidate_set_sha256": "b" * 64,
        "scan_rule_pack_sha256": "c" * 64,
        "draft_rule_pack_sha256": "d" * 64,
        "profile_present": False,
        "book_profile_sha256": "e" * 64,
        "book_profile_file_sha256": None,
        "review_protocol_identity": implementation,
        "review_protocol_identity_sha256": protocol.canonical_sha256(implementation),
    }


def fixture(candidate_count: int, anchor_count: int, original_size: int = 24):
    source_parts: list[str] = []
    candidates: list[dict] = []
    drafts: list[dict] = []
    cursor = 0
    remaining = anchor_count
    for candidate_index in range(candidate_count):
        count = remaining // (candidate_count - candidate_index)
        remaining -= count
        anchors = []
        for anchor_index in range(count):
            prefix = "前" * 360
            original = (
                f"候选{candidate_index:03d}出现{anchor_index:03d}广告"
                + "广" * original_size
            )
            suffix = "后" * 360 + "\n"
            source_parts.extend((prefix, original, suffix))
            start = cursor + len(prefix)
            end = start + len(original)
            anchors.append(
                {
                    "offset": start,
                    "end": end,
                    "line": candidate_index * max(1, count) + anchor_index + 1,
                    "original": original,
                    "prefix": prefix[-10:],
                    "suffix": suffix[:10],
                    "chapter": {
                        "index": candidate_index + 1,
                        "title": f"第{candidate_index + 1}章",
                    },
                }
            )
            cursor += len(prefix) + len(original) + len(suffix)
        candidate = {
            "candidate_id": f"AD-{candidate_index + 1:06d}",
            "layer": "L3",
            "detector": "pattern-hit",
            "reason": f"fixture-signal-{candidate_index}",
            "signals": ["domain"],
            "risk_hint": "medium",
            "occurrence_count": len(anchors),
            "anchors_truncated": False,
            "anchors": anchors,
            "suggested_decision": {"anchors": [dict(item) for item in anchors]},
        }
        scan_identity.attach_candidate_fingerprints([candidate])
        scan_identity.attach_anchor_ids([candidate])
        candidates.append(candidate)
        drafts.append(
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "verdict": "uncertain",
                "risk": "medium",
                "splice_strategy": "remove_paragraph",
                "source_candidate": {
                    "signals": ["domain"],
                    "detector": "pattern-hit",
                },
                "anchors": [dict(item) for item in candidate["anchors"]],
            }
        )
    result_identity = identity()
    result_identity["candidate_set_sha256"] = scan_identity.candidate_set_sha256(candidates)
    return "".join(source_parts), candidates, drafts, result_identity


def write_projection(workspace: Path, projection: dict) -> None:
    for relative, encoded in protocol.projection_artifacts(projection).items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)


class AdReviewProtocolTests(unittest.TestCase):
    def test_candidate_without_anchors_remains_visible_and_bound(self) -> None:
        candidate = {
            "candidate_id": "AD-EMPTY",
            "layer": "L3",
            "detector": "fixture",
            "reason": "missing anchors",
            "signals": ["domain"],
            "risk_hint": "high",
            "occurrence_count": 0,
            "anchors_truncated": False,
            "anchors": [],
        }
        scan_identity.attach_candidate_fingerprints([candidate])
        scan_identity.attach_anchor_ids([candidate])
        draft = {
            "candidate_id": candidate["candidate_id"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "verdict": "uncertain",
            "risk": "high",
            "splice_strategy": "remove_paragraph",
        }
        bound_identity = identity()
        bound_identity["candidate_set_sha256"] = scan_identity.candidate_set_sha256(
            [candidate]
        )
        projection = protocol.build_review_projection(
            [candidate], [draft], "", bound_identity
        )
        record = projection["pages"][0]["records"][0]
        self.assertEqual(record["candidate_id"], "AD-EMPTY")
        self.assertEqual(record["occurrences"], [])
        self.assertEqual(projection["manifest"]["candidate_count"], 1)

    def test_make_and_finalize_use_paged_projection_without_executable_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "novel.txt"
            source_path.write_text(
                "第一章 开端\n请访问 https://reader.example.com 获取最新章节。\n剧情继续。\n",
                encoding="utf-8",
            )
            workspace = preprocess.run(source_path)
            parse_structure.run(workspace)
            scan_report = scan_ads.run(
                workspace,
                "versions/v1_preprocessed.txt",
                "candidates/ads.jsonl",
                12,
                50,
                50,
            )
            candidates = scan_identity.load_validated_pages(workspace, scan_report)
            self.assertTrue(candidates)
            draft_report = make_ad_decisions.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_decisions.draft.jsonl",
                "meta/book_profile.json",
                True,
            )
            review_manifest_path = workspace / draft_report["review_pages_manifest"]
            review_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                draft_report["review_page_count"], len(review_manifest["pages"])
            )
            self.assertNotIn(b'"offset"', b"".join(
                (workspace / entry["file"]).read_bytes()
                for entry in review_manifest["pages"]
            ))
            manifest_sha256 = hashlib.sha256(review_manifest_path.read_bytes()).hexdigest()
            review_dir = workspace / "decisions/ads_agent_reviews/pages"
            review_dir.mkdir(parents=True)
            first_page_by_candidate: dict[str, int] = {}
            for entry in review_manifest["pages"]:
                for candidate_id in entry["candidate_ids"]:
                    first_page_by_candidate.setdefault(candidate_id, entry["page_number"])
            candidate_map = {candidate["candidate_id"]: candidate for candidate in candidates}
            group_by_candidate = {
                candidate_id: group["review_group_id"]
                for group in review_manifest["review_groups"]
                for candidate_id in group["member_candidate_ids"]
            }
            for entry in review_manifest["pages"]:
                records = [
                    {
                        "record_type": "page_attestation",
                        "schema": protocol.REVIEW_ATTESTATION_SCHEMA,
                        "page_number": entry["page_number"],
                        "page_sha256": entry["sha256"],
                        "manifest_sha256": manifest_sha256,
                        "projection_set_sha256": review_manifest["projection_set_sha256"],
                        "occurrence_coverage_sha256": entry["occurrence_coverage_sha256"],
                    }
                ]
                for candidate_id, page_number in first_page_by_candidate.items():
                    if page_number == entry["page_number"]:
                        candidate = candidate_map[candidate_id]
                        records.append(
                            {
                                "record_type": "candidate_verdict",
                                "review_group_id": group_by_candidate[candidate_id],
                                "candidate_id": candidate_id,
                                "candidate_fingerprint": candidate["candidate_fingerprint"],
                                "verdict": "uncertain",
                                "confidence": 0.5,
                                "reason": "测试中保守标记为待复核",
                                "blocking_reasons": ["测试待复核"],
                            }
                        )
                common.write_jsonl(
                    review_dir / f"page_{entry['page_number']:04d}.jsonl", records
                )
            formal_report = finalize_ad_decisions.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_agent_reviews.jsonl",
                "decisions/ads_decisions.draft.jsonl",
                "decisions/ads_decisions.jsonl",
            )
            self.assertTrue(formal_report["paged_reviews_used"])
            self.assertEqual(formal_report["review_count"], len(candidates))
            self.assertTrue((workspace / "decisions/ads_agent_reviews.jsonl").is_file())

    def test_projection_has_no_offsets_or_duplicated_executable_anchors(self) -> None:
        source, candidates, drafts, bound_identity = fixture(1, 5)
        previous = [
            {
                "candidate_id": candidates[0]["candidate_id"],
                "candidate_fingerprint": candidates[0]["candidate_fingerprint"],
                "verdict": "delete",
            }
        ]
        first = protocol.build_review_projection(
            candidates, drafts, source, bound_identity, previous_formal=previous
        )
        second = protocol.build_review_projection(
            candidates, drafts, source, bound_identity, previous_formal=previous
        )
        self.assertEqual(protocol.projection_artifacts(first), protocol.projection_artifacts(second))
        encoded = b"".join(protocol.projection_artifacts(first).values())
        self.assertNotIn(b'"offset"', encoded)
        self.assertNotIn(b'"suggested_decision"', encoded)
        self.assertNotIn(b'"anchors"', encoded)
        records = [record for page in first["pages"] for record in page["records"]]
        occurrences = [
            occurrence
            for record in records
            if record["record_type"] == "candidate_occurrences"
            for occurrence in record["occurrences"]
        ]
        self.assertEqual(len(occurrences), 5)
        self.assertTrue(all("context_sha256" in item for item in occurrences))
        fifth = occurrences[4]
        self.assertGreaterEqual(len(fifth["context_before"]), protocol.EXPANDED_CONTEXT_CHARS)
        self.assertGreaterEqual(len(fifth["context_after"]), protocol.EXPANDED_CONTEXT_CHARS)
        for entry in first["manifest"]["pages"]:
            self.assertLessEqual(entry["bytes"], protocol.HARD_PAGE_BYTES)

    def test_delete_group_requires_independent_exact_gates_and_shape(self) -> None:
        source, candidates, drafts, _ = fixture(3, 3)
        for draft in drafts:
            draft["verdict"] = "delete"
            draft["evidence"] = [
                {
                    "type": "automatic_delete_gate",
                    "value": {"locator": True, "promotion_intent": True},
                }
            ]
        drafts[2]["protected_terms"] = ["主角"]
        groups = protocol.build_review_groups(candidates, drafts)
        exact = [group for group in groups if group["group_kind"] == "delete_exact"]
        uncertain = [group for group in groups if group["group_kind"] == "uncertain_review"]
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0]["member_candidate_ids"], [
            candidates[0]["candidate_id"], candidates[1]["candidate_id"]
        ])
        self.assertTrue(exact[0]["delete_group_allowed"])
        self.assertEqual(uncertain[0]["member_candidate_ids"], [candidates[2]["candidate_id"]])
        self.assertFalse(uncertain[0]["delete_group_allowed"])

    def test_merge_is_resumable_deterministic_and_rejects_stale_or_missing_pages(self) -> None:
        source, candidates, drafts, bound_identity = fixture(2, 4)
        projection = protocol.build_review_projection(
            candidates, drafts, source, bound_identity, target_bytes=4096, hard_limit_bytes=8192
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_projection(workspace, projection)
            manifest_path = workspace / "candidates/ads_review_pages/manifest.json"
            manifest = projection["manifest"]
            manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            reviews_dir = workspace / "decisions/ads_agent_reviews/pages"
            reviews_dir.mkdir(parents=True)
            verdict_page: dict[str, int] = {}
            for entry in manifest["pages"]:
                for candidate_id in entry["candidate_ids"]:
                    verdict_page.setdefault(candidate_id, entry["page_number"])
            for entry in manifest["pages"]:
                records = [
                    {
                        "record_type": "page_attestation",
                        "schema": protocol.REVIEW_ATTESTATION_SCHEMA,
                        "page_number": entry["page_number"],
                        "page_sha256": entry["sha256"],
                        "manifest_sha256": manifest_sha256,
                        "projection_set_sha256": manifest["projection_set_sha256"],
                        "occurrence_coverage_sha256": entry["occurrence_coverage_sha256"],
                    }
                ]
                for candidate in candidates:
                    if verdict_page[candidate["candidate_id"]] == entry["page_number"]:
                        records.append(
                            {
                                "record_type": "candidate_verdict",
                                "review_group_id": next(
                                    group["review_group_id"]
                                    for group in manifest["review_groups"]
                                    if candidate["candidate_id"] in group["member_candidate_ids"]
                                ),
                                "candidate_id": candidate["candidate_id"],
                                "candidate_fingerprint": candidate["candidate_fingerprint"],
                                "verdict": "uncertain",
                                "confidence": 0.5,
                                "reason": "需要人工复核",
                                "blocking_reasons": ["上下文不足"],
                            }
                        )
                (reviews_dir / f"page_{entry['page_number']:04d}.jsonl").write_bytes(
                    protocol.jsonl_bytes(records)
                )
            merged = protocol.merge_review_pages(workspace, candidates)
            self.assertEqual(merged, protocol.merge_review_pages(workspace, candidates))
            self.assertEqual([item["candidate_id"] for item in merged], [item["candidate_id"] for item in candidates])
            missing = reviews_dir / f"page_{len(manifest['pages']):04d}.jsonl"
            saved = missing.read_bytes()
            missing.unlink()
            with self.assertRaisesRegex(protocol.ReviewProtocolError, "incomplete"):
                protocol.merge_review_pages(workspace, candidates)
            missing.write_bytes(saved)
            page_path = workspace / manifest["pages"][0]["file"]
            page_path.write_bytes(page_path.read_bytes() + b" ")
            with self.assertRaisesRegex(protocol.ReviewProtocolError, "hash drifted"):
                protocol.merge_review_pages(workspace, candidates)

    def test_delete_exact_group_verdict_expands_to_candidate_reviews(self) -> None:
        source, candidates, drafts, bound_identity = fixture(2, 2)
        for draft in drafts:
            draft["verdict"] = "delete"
            draft["evidence"] = [
                {
                    "type": "automatic_delete_gate",
                    "value": {"locator": True, "promotion_intent": True},
                }
            ]
        projection = protocol.build_review_projection(candidates, drafts, source, bound_identity)
        group = projection["manifest"]["review_groups"][0]
        self.assertEqual(group["group_kind"], "delete_exact")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_projection(workspace, projection)
            manifest_path = workspace / "candidates/ads_review_pages/manifest.json"
            manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            reviews_dir = workspace / "decisions/ads_agent_reviews/pages"
            reviews_dir.mkdir(parents=True)
            for entry in projection["manifest"]["pages"]:
                records = [
                    {
                        "record_type": "page_attestation",
                        "schema": protocol.REVIEW_ATTESTATION_SCHEMA,
                        "page_number": entry["page_number"],
                        "page_sha256": entry["sha256"],
                        "manifest_sha256": manifest_sha256,
                        "projection_set_sha256": projection["manifest"]["projection_set_sha256"],
                        "occurrence_coverage_sha256": entry["occurrence_coverage_sha256"],
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
                            "verdict": "delete",
                            "confidence": 0.99,
                            "reason": "每个成员均通过精确删除门禁",
                            "risk": "low",
                            "action": "delete",
                            "splice_strategy": "remove_paragraph",
                        }
                    )
                (reviews_dir / f"page_{entry['page_number']:04d}.jsonl").write_bytes(
                    protocol.jsonl_bytes(records)
                )
            merged = protocol.merge_review_pages(workspace, candidates)
            self.assertEqual(len(merged), 2)
            self.assertTrue(all(item["verdict"] == "delete" for item in merged))
            self.assertEqual(
                [item["candidate_id"] for item in merged],
                [item["candidate_id"] for item in candidates],
            )
            self.assertTrue(all("review_group_id" not in item for item in merged))

    def test_150_candidates_7000_occurrences_fit_budget_and_reduce_repeated_payload(self) -> None:
        benchmark = json.loads(
            (ROOT / "tests/performance/review_projection_baseline.json").read_text(
                encoding="utf-8"
            )
        )
        source, candidates, drafts, bound_identity = fixture(
            benchmark["candidate_count"],
            benchmark["occurrence_count"],
            original_size=benchmark["synthetic_original_chars"],
        )
        projection = protocol.build_review_projection(candidates, drafts, source, bound_identity)
        artifacts = protocol.projection_artifacts(projection)
        projection_bytes = sum(
            len(encoded) for relative, encoded in artifacts.items() if relative.endswith(".json")
        )
        repeated_baseline = len(
            json.dumps(
                {"candidates": candidates, "drafts": drafts},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self.assertEqual(projection["manifest"]["candidate_count"], benchmark["candidate_count"])
        self.assertEqual(projection["manifest"]["occurrence_count"], benchmark["occurrence_count"])
        self.assertEqual(projection["manifest"]["review_record_oversize_count"], 0)
        self.assertLessEqual(
            projection_bytes,
            repeated_baseline
            * benchmark["maximum_projection_to_repeated_payload_ratio"],
        )
        self.assertTrue(
            all(entry["bytes"] <= protocol.HARD_PAGE_BYTES for entry in projection["manifest"]["pages"])
        )


if __name__ == "__main__":
    unittest.main()
