from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from common import (
    WorkspaceTransaction,
    load_jsonl,
    load_manifest,
    resolve_in_workspace,
    resolve_workspace_paths,
    sha256_file,
    workspace_transaction_lock,
    write_json,
    write_jsonl,
)
from make_ad_decisions import load_current_ad_candidates
import ad_decision_policy
import ad_review_protocol
import scan_identity


VALID_VERDICTS = {"delete", "keep", "uncertain"}
VALID_RISKS = {"low", "medium", "high"}
VALID_SPLICE_STRATEGIES = {
    "exact",
    "exact_segment",
    "fallback_newline",
    "remove_paragraph",
}
DELETE_EVIDENCE_TYPES = {"automatic_delete_gate", "family_similarity"}
REVIEW_KEYS = {
    "action",
    "blocking_reasons",
    "candidate_fingerprint",
    "candidate_id",
    "confidence",
    "reason",
    "risk",
    "scan_id",
    "splice_strategy",
    "keep_basis",
    "edit_plan_id",
    "verdict",
}
AUDIT_METADATA_KEYS = (
    "cluster_id",
    "family_signature",
    "evidence",
    "mutation_guard",
    "promoted_from",
    "neighbor_span",
)
DRAFT_PROVENANCE_STATUSES = {"draft_decisions_ready", "formal_decisions_ready", "done"}
DRAFT_IDENTITY_KEYS = (
    "scan_rule_pack_sha256",
    "draft_rule_pack_sha256",
    "profile_present",
    "book_profile_sha256",
    "book_profile_file_sha256",
)
FORMAL_IDENTITY_KEYS = ("profile", *DRAFT_IDENTITY_KEYS)


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def index_by_candidate_id(
    records: list[dict[str, Any]], label: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        candidate_id = record.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError(f"{label} contains a record without candidate_id")
        if candidate_id in result:
            raise ValueError(f"{label} contains duplicate candidate_id: {candidate_id}")
        result[candidate_id] = record
    return result


def splice_strategy(candidate: dict[str, Any], draft: dict[str, Any], review: dict[str, Any]) -> str:
    value = review.get("splice_strategy")
    if value is None:
        value = draft.get("splice_strategy")
    suggested = candidate.get("suggested_decision")
    if value is None and isinstance(suggested, dict):
        value = suggested.get("splice_strategy")
    if value is None:
        value = "remove_paragraph"
    if not isinstance(value, str) or value not in VALID_SPLICE_STRATEGIES:
        raise ValueError("splice_strategy is invalid")
    return value


def normalized_blockers(review: dict[str, Any]) -> list[str]:
    values = review.get("blocking_reasons")
    if not isinstance(values, list) or not values:
        raise ValueError("blocking_reasons must be a non-empty string list")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("blocking_reasons must be a non-empty string list")
    normalized = [value.strip() for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError("blocking_reasons must not contain duplicates")
    return normalized


def has_delete_evidence(record: dict[str, Any]) -> bool:
    if record.get("verdict") != "delete":
        return False
    evidence = record.get("evidence")
    return bool(record.get("promoted_from")) or (
        isinstance(evidence, list)
        and any(
            isinstance(item, dict) and item.get("type") in DELETE_EVIDENCE_TYPES
            for item in evidence
        )
    )


def validate_candidate_contract(candidates: list[dict[str, Any]]) -> None:
    candidate_map = index_by_candidate_id(candidates, "candidates")
    fingerprints: set[str] = set()
    scan_identity.validate_anchor_ids(candidates)
    for candidate_id, candidate in candidate_map.items():
        ad_decision_policy.validate_candidate_occurrences(candidate)
        if candidate.get("edit_plan") is not None:
            ad_decision_policy.normalize_edit_plan(candidate["edit_plan"], candidate)
        fingerprint = candidate.get("candidate_fingerprint")
        if not _sha256(fingerprint) or fingerprint in fingerprints:
            raise ValueError(f"candidate fingerprint is invalid or duplicated: {candidate_id}")
        fingerprints.add(fingerprint)
        truncated = candidate.get("anchors_truncated")
        if not isinstance(truncated, bool):
            raise ValueError(f"anchors_truncated must be boolean for {candidate_id}")
        anchors = candidate.get("anchors")
        if not isinstance(anchors, list):
            raise ValueError(f"anchors must be a list for {candidate_id}")
        for anchor in anchors:
            offset = anchor.get("offset")
            end = anchor.get("end")
            line = anchor.get("line")
            original = anchor.get("original")
            if (
                not isinstance(offset, int)
                or isinstance(offset, bool)
                or offset < 0
                or not isinstance(end, int)
                or isinstance(end, bool)
                or not isinstance(original, str)
                or not original
                or end != offset + len(original)
                or not isinstance(line, int)
                or isinstance(line, bool)
                or line < 1
            ):
                raise ValueError(f"anchor range is invalid for {candidate_id}")
            if not isinstance(anchor.get("prefix"), str) or not isinstance(
                anchor.get("suffix"), str
            ):
                raise ValueError(f"anchor prefix/suffix are invalid for {candidate_id}")
            chapter = anchor.get("chapter")
            locator = anchor.get("locator")
            if chapter is not None and locator is not None:
                raise ValueError(f"anchor chapter reference is ambiguous for {candidate_id}")
            reference = chapter if chapter is not None else locator
            if reference is not None:
                if not isinstance(reference, dict):
                    raise ValueError(f"anchor chapter reference is invalid for {candidate_id}")
                index = reference.get("index")
                title = reference.get("title")
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    or not isinstance(title, str)
                ):
                    raise ValueError(f"anchor chapter reference is invalid for {candidate_id}")
                if (
                    locator is not None
                    and locator.get("kind") not in scan_identity.NON_CHAPTER_LOCATOR_KINDS
                ):
                    raise ValueError(f"anchor locator is invalid for {candidate_id}")


def validate_review(
    review: dict[str, Any],
    candidate: dict[str, Any],
    scan_id: str,
) -> tuple[str, float, str, list[str], dict[str, Any] | None, str | None]:
    unknown = sorted(set(review) - REVIEW_KEYS)
    if unknown:
        raise ValueError(f"Agent review contains unknown fields: {unknown}")
    candidate_id = candidate["candidate_id"]
    if review.get("scan_id") != scan_id or not _sha256(review.get("scan_id")):
        raise ValueError(f"stale scan_id for {candidate_id}")
    if review.get("candidate_fingerprint") != candidate["candidate_fingerprint"]:
        raise ValueError(f"candidate fingerprint mismatch for {candidate_id}")

    verdict = review.get("verdict")
    if not isinstance(verdict, str) or verdict not in VALID_VERDICTS:
        raise ValueError(f"invalid verdict for {candidate_id}: {verdict!r}")
    reason = review.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"Agent review reason is required for {candidate_id}")
    confidence = review.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        raise ValueError(f"confidence must be a finite number between 0 and 1 for {candidate_id}")
    risk = review.get("risk")
    if risk is not None and (not isinstance(risk, str) or risk not in VALID_RISKS):
        raise ValueError(f"risk is invalid for {candidate_id}")
    action = review.get("action")
    normalized_keep_basis: dict[str, Any] | None = None
    edit_plan_id: str | None = None
    if verdict == "delete":
        if action is not None and action != "delete":
            raise ValueError(f"action is invalid for {candidate_id}")
        if "blocking_reasons" in review:
            raise ValueError(f"delete review cannot contain blocking_reasons: {candidate_id}")
        if "keep_basis" in review:
            raise ValueError(f"delete review cannot contain keep_basis: {candidate_id}")
        if "edit_plan_id" in review:
            value = review.get("edit_plan_id")
            if not isinstance(value, str) or not value:
                raise ValueError(f"edit_plan_id is invalid for {candidate_id}")
            edit_plan_id = value
        if "splice_strategy" in review:
            splice_strategy(candidate, {}, review)
        blockers: list[str] = []
    elif verdict == "uncertain":
        if (
            "action" in review
            or "splice_strategy" in review
            or "keep_basis" in review
            or "edit_plan_id" in review
        ):
            raise ValueError(
                f"uncertain review cannot contain action, strategy, or keep_basis: {candidate_id}"
            )
        blockers = normalized_blockers(review)
    else:
        if (
            "action" in review
            or "splice_strategy" in review
            or "blocking_reasons" in review
            or "edit_plan_id" in review
        ):
            raise ValueError(f"keep review cannot contain mutating or blocking fields: {candidate_id}")
        if "keep_basis" in review:
            normalized_keep_basis = ad_decision_policy.normalize_keep_basis(
                review["keep_basis"],
                candidate,
            )
        blockers = []
    return (
        verdict,
        float(confidence),
        reason.strip(),
        blockers,
        normalized_keep_basis,
        edit_plan_id,
    )


def validate_drafts(
    drafts: list[dict[str, Any]],
    candidate_map: dict[str, dict[str, Any]],
    scan_id: str,
) -> dict[str, dict[str, Any]]:
    draft_map = index_by_candidate_id(drafts, "draft decisions") if drafts else {}
    missing = sorted(set(candidate_map) - set(draft_map))
    extra = sorted(set(draft_map) - set(candidate_map))
    if missing or extra:
        raise ValueError(
            "draft decisions must cover the complete candidate set; "
            f"missing={missing[:8]}, extra={extra[:8]}"
        )
    for candidate_id, draft in draft_map.items():
        if draft.get("scan_id") != scan_id:
            raise ValueError(f"stale draft scan_id for {candidate_id}")
        if draft.get("candidate_fingerprint") != candidate_map[candidate_id][
            "candidate_fingerprint"
        ]:
            raise ValueError(f"draft candidate fingerprint mismatch for {candidate_id}")
    return draft_map


def compile_formal_decisions(
    candidates: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    drafts: list[dict[str, Any]],
    *,
    scan_id: str,
    provenance: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not _sha256(scan_id):
        raise ValueError("scan_id must be a lowercase SHA-256 value")
    validate_candidate_contract(candidates)
    candidate_map = index_by_candidate_id(candidates, "candidates")
    review_map = index_by_candidate_id(reviews, "Agent reviews")
    draft_map = validate_drafts(drafts, candidate_map, scan_id)

    missing = sorted(set(candidate_map) - set(review_map))
    extra = sorted(set(review_map) - set(candidate_map))
    if missing or extra:
        raise ValueError(
            "Agent reviews must cover the complete candidate set; "
            f"missing={missing[:8]}, extra={extra[:8]}"
        )
    review_fingerprints = [review.get("candidate_fingerprint") for review in reviews]
    if any(not _sha256(value) for value in review_fingerprints) or len(
        review_fingerprints
    ) != len(set(review_fingerprints)):
        raise ValueError("Agent reviews contain invalid or duplicate candidate fingerprints")

    decisions: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        review = review_map[candidate_id]
        draft = draft_map.get(candidate_id, {})
        verdict, confidence, reason, blockers, keep_basis, review_edit_plan_id = validate_review(
            review, candidate, scan_id
        )
        delete_evidence = has_delete_evidence(draft)
        if verdict == "keep" and delete_evidence and keep_basis is None:
            raise ValueError(
                "keep conflicts with bound delete evidence; set a structured keep_basis "
                "only after checking every occurrence: "
                f"{candidate_id}"
            )
        anchors = candidate["anchors"]
        occurrences = ad_decision_policy.validate_candidate_occurrences(candidate)

        decision: dict[str, Any] = {
            "scan_id": scan_id,
            "candidate_id": candidate_id,
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "anchor_ids": [record["anchor_id"] for record in occurrences],
            "anchor_text_sha256s": [record["text_sha256"] for record in occurrences],
            "occurrence_count": candidate["occurrence_count"],
            "anchors_truncated": candidate["anchors_truncated"],
            "verdict": verdict,
            "confidence": confidence,
            "reason": reason,
            "risk": review.get("risk") or draft.get("risk") or candidate.get("risk_hint", "medium"),
            "decision_source": "local_agent",
        }
        if provenance is not None:
            for key in FORMAL_IDENTITY_KEYS:
                if key not in provenance:
                    raise ValueError(f"formal provenance is missing {key}")
                decision[key] = copy.deepcopy(provenance[key])
        for key in AUDIT_METADATA_KEYS:
            value = draft.get(key)
            if value not in (None, "", [], {}):
                decision[key] = copy.deepcopy(value)
        if keep_basis is not None:
            decision["keep_basis"] = keep_basis

        if verdict == "uncertain":
            decision["blocking_reasons"] = blockers

        if verdict == "delete":
            candidate_plan = candidate.get("edit_plan")
            if (
                candidate.get("mutation_guard") == "long_line_mixed_content"
                and review.get("splice_strategy") != "exact_segment"
            ):
                raise ValueError(
                    f"long-line mixed-content candidate cannot delete the whole line: "
                    f"{candidate_id}"
                )
            if candidate_plan is not None:
                normalized_plan = ad_decision_policy.normalize_edit_plan(
                    candidate_plan,
                    candidate,
                )
                if review_edit_plan_id != normalized_plan["edit_plan_id"]:
                    raise ValueError(
                        f"segment delete must reference the current scanner edit_plan_id: "
                        f"{candidate_id}"
                    )
                if review.get("splice_strategy") != "exact_segment":
                    raise ValueError(
                        f"segment delete review must explicitly set "
                        f"splice_strategy=exact_segment: {candidate_id}"
                    )
                strategy = splice_strategy(candidate, draft, review)
                if strategy != "exact_segment":
                    raise ValueError(
                        f"segment delete requires splice_strategy=exact_segment: {candidate_id}"
                    )
                decision["action"] = "delete"
                decision["splice_strategy"] = "exact_segment"
                decision["edit_plan_id"] = normalized_plan["edit_plan_id"]
                decision["edit_plan"] = copy.deepcopy(normalized_plan)
                decision["anchors"] = copy.deepcopy(anchors)
                decisions.append(decision)
                continue
            if review_edit_plan_id is not None:
                raise ValueError(
                    f"whole-block delete cannot reference edit_plan_id: {candidate_id}"
                )
            if splice_strategy(candidate, draft, review) == "exact_segment":
                raise ValueError(
                    f"exact_segment requires a current scanner edit plan: {candidate_id}"
                )
            if candidate.get("mutation_guard") in ad_decision_policy.SEGMENT_REVIEW_GUARDS:
                raise ValueError(
                    f"mixed-content candidate cannot delete the whole line: "
                    f"{candidate_id}"
                )
            if candidate["anchors_truncated"]:
                raise ValueError(
                    f"truncated candidate must be rescanned before delete: {candidate_id}"
                )
            if not anchors:
                raise ValueError(f"delete review has no executable anchors: {candidate_id}")
            decision["action"] = "delete"
            decision["splice_strategy"] = splice_strategy(candidate, draft, review)
            decision["anchors"] = copy.deepcopy(anchors)

        decisions.append(decision)
    return decisions


def count_by_verdict(decisions: list[dict[str, Any]]) -> dict[str, int]:
    result = {verdict: 0 for verdict in sorted(VALID_VERDICTS)}
    for decision in decisions:
        result[str(decision["verdict"])] += 1
    return result


def validate_current_draft_provenance(
    workspace: Path,
    draft_path: Path,
    drafts: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    scan_report: dict[str, Any],
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind the complete rule draft to its committed generator run and report."""
    manifest = manifest or load_manifest(workspace)
    stages = manifest.get("stages")
    stage = stages.get("2_ads") if isinstance(stages, dict) else None
    artifacts = manifest.get("artifacts")
    if not isinstance(stage, dict) or not isinstance(artifacts, dict):
        raise ValueError("current rule draft provenance is missing; run make_ad_decisions first")

    draft_relative = draft_path.relative_to(workspace).as_posix()
    report_value = stage.get("draft_report")
    if not isinstance(report_value, str) or not report_value:
        raise ValueError("current rule draft report is missing; run make_ad_decisions first")
    report_path = resolve_in_workspace(workspace, report_value, role="read")
    report_relative = report_path.relative_to(workspace).as_posix()
    draft_run_id = stage.get("draft_run_id")
    if not draft_path.is_file() or not report_path.is_file():
        raise ValueError("current rule draft artifacts are missing; rerun make_ad_decisions")
    draft_sha256 = sha256_file(draft_path)
    report_sha256 = sha256_file(report_path)
    if (
        stage.get("status") not in DRAFT_PROVENANCE_STATUSES
        or str(stage.get("draft_decisions") or "").replace("\\", "/") != draft_relative
        or report_relative != "report/ad_decision_draft_report.json"
        or not isinstance(draft_run_id, str)
        or not draft_run_id
        or stage.get("draft_decisions_sha256") != draft_sha256
        or stage.get("draft_report_sha256") != report_sha256
        or stage.get("scan_id") != scan_report.get("scan_id")
        or stage.get("candidate_set_sha256") != scan_report.get("candidate_set_sha256")
    ):
        raise ValueError("current rule draft provenance is stale; rerun make_ad_decisions")

    for relative, path, expected_sha256 in (
        (draft_relative, draft_path, draft_sha256),
        (report_relative, report_path, report_sha256),
    ):
        record = artifacts.get(relative)
        if (
            not isinstance(record, dict)
            or record.get("path") != relative
            or record.get("stage") != "2_ads"
            or record.get("run_id") != draft_run_id
            or record.get("sha256") != expected_sha256
            or record.get("size_bytes") != path.stat().st_size
        ):
            raise ValueError("current rule draft artifact provenance is stale")

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("current rule draft report cannot be read") from exc
    if not isinstance(report, dict):
        raise ValueError("current rule draft report must be a JSON object")

    if stage.get("status") == "draft_decisions_ready":
        expected_stage_artifacts = {draft_relative, report_relative}
        review_manifest_value = report.get("review_pages_manifest")
        if isinstance(review_manifest_value, str) and review_manifest_value:
            review_manifest_path = resolve_in_workspace(
                workspace, review_manifest_value, role="read"
            )
            try:
                review_manifest = json.loads(
                    review_manifest_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValueError("current Agent review manifest cannot be read") from error
            declared_pages = review_manifest.get("pages") if isinstance(review_manifest, dict) else None
            if not isinstance(declared_pages, list):
                raise ValueError("current Agent review manifest pages are invalid")
            review_manifest_relative = review_manifest_path.relative_to(workspace).as_posix()
            review_manifest_sha256 = sha256_file(review_manifest_path)
            if (
                report.get("review_pages_manifest_sha256") != review_manifest_sha256
                or stage.get("review_pages_manifest_sha256") != review_manifest_sha256
            ):
                raise ValueError("current Agent review manifest hash is stale")
            expected_stage_artifacts.add(review_manifest_relative)
            review_paths = [(review_manifest_relative, review_manifest_path, review_manifest_sha256)]
            for entry in declared_pages:
                page_value = entry.get("file") if isinstance(entry, dict) else None
                page_sha256 = entry.get("sha256") if isinstance(entry, dict) else None
                if not isinstance(page_value, str) or not page_value or not _sha256(page_sha256):
                    raise ValueError("current Agent review manifest page is invalid")
                page_path = resolve_in_workspace(workspace, page_value, role="read")
                page_relative = page_path.relative_to(workspace).as_posix()
                if not page_path.is_file() or sha256_file(page_path) != page_sha256:
                    raise ValueError("current Agent review projection page is stale")
                expected_stage_artifacts.add(page_relative)
                review_paths.append((page_relative, page_path, page_sha256))
            for relative, path, expected_sha256 in review_paths:
                record = artifacts.get(relative)
                if (
                    not isinstance(record, dict)
                    or record.get("path") != relative
                    or record.get("stage") != "2_ads"
                    or record.get("run_id") != draft_run_id
                    or record.get("sha256") != expected_sha256
                    or record.get("size_bytes") != path.stat().st_size
                ):
                    raise ValueError("current Agent review artifact provenance is stale")
        if set(stage.get("artifacts", [])) != expected_stage_artifacts:
            raise ValueError("current rule draft artifacts are not stage-owned or are stale")

    profile_value = report.get("profile")
    if not isinstance(profile_value, str) or not profile_value:
        raise ValueError("current rule draft profile path is missing")
    profile_path = resolve_in_workspace(workspace, profile_value, role="read")
    profile_relative = profile_path.relative_to(workspace).as_posix()
    if profile_value.replace("\\", "/") != profile_relative:
        raise ValueError("current rule draft profile path is stale")

    current_draft_rule_pack = scan_identity.build_draft_rule_pack()
    current_draft_rule_pack_sha256 = scan_identity.canonical_json_sha256(
        current_draft_rule_pack
    )
    current_profile_identity = scan_identity.build_profile_identity(profile_path)
    scan_rule_pack_sha256 = scan_report.get("scan_rule_pack_sha256")
    if not _sha256(scan_rule_pack_sha256):
        raise ValueError("current scan rule pack identity is missing")
    provenance = {
        "profile": profile_relative,
        "scan_rule_pack_sha256": scan_rule_pack_sha256,
        "draft_rule_pack_sha256": current_draft_rule_pack_sha256,
        **current_profile_identity,
    }

    declared_report_pack = report.get("draft_rule_pack")
    declared_stage_pack = stage.get("draft_rule_pack")
    if (
        not isinstance(declared_report_pack, dict)
        or not isinstance(declared_stage_pack, dict)
        or declared_report_pack != current_draft_rule_pack
        or declared_stage_pack != current_draft_rule_pack
        or scan_identity.canonical_json_sha256(declared_report_pack)
        != current_draft_rule_pack_sha256
        or scan_identity.canonical_json_sha256(declared_stage_pack)
        != current_draft_rule_pack_sha256
    ):
        raise ValueError("current rule draft implementation rule pack is stale")

    for field in DRAFT_IDENTITY_KEYS:
        expected_value = provenance[field]
        if report.get(field) != expected_value:
            raise ValueError(f"current rule draft report {field} identity is stale")
        if stage.get(field) != expected_value:
            raise ValueError(f"current rule draft stage {field} identity is stale")
    for draft in drafts:
        candidate_id = draft.get("candidate_id")
        for field in DRAFT_IDENTITY_KEYS:
            if draft.get(field) != provenance[field]:
                raise ValueError(
                    f"current rule draft row {field} identity is stale: {candidate_id}"
                )

    counts: dict[str, int] = {}
    for draft in drafts:
        verdict = str(draft.get("verdict") or "unknown")
        counts[verdict] = counts.get(verdict, 0) + 1
    expected = {
        "scan_id": scan_report.get("scan_id"),
        "candidate_set_sha256": scan_report.get("candidate_set_sha256"),
        "output": draft_relative,
        "draft_sha256": draft_sha256,
        "candidate_count": len(candidates),
        "decision_count": len(drafts),
        "duplicate_candidate_count": 0,
        "by_verdict": counts,
        "delete_count": counts.get("delete", 0),
        "keep_count": counts.get("keep", 0),
        "uncertain_count": counts.get("uncertain", 0),
    }
    for field, expected_value in expected.items():
        actual_value = report.get(field)
        if field == "output":
            actual_value = str(actual_value or "").replace("\\", "/")
        if actual_value != expected_value:
            raise ValueError(f"current rule draft report {field} is stale")
    if (
        stage.get("draft_delete_count") != expected["delete_count"]
        or stage.get("draft_keep_count") != expected["keep_count"]
        or stage.get("draft_uncertain_count") != expected["uncertain_count"]
    ):
        raise ValueError("current rule draft stage counts are stale")
    return provenance


def reported_candidate_count(workspace: Path) -> int | None:
    path = workspace / "report" / "ads_scan_report.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    summary = data.get("summary", {}) if isinstance(data, dict) else {}
    value = summary.get("total_candidate_count") if isinstance(summary, dict) else None
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def should_mark_formal_ready(decisions_changed: bool, previous_stage_status: str) -> bool:
    return decisions_changed or previous_stage_status != "done"


def load_preserved_formal_report(
    workspace: Path,
    manifest: dict[str, Any],
    output_path: Path,
    report_path: Path,
    *,
    output_sha256: str,
    reviews_sha256: str,
    draft_sha256: str | None,
    scan_id: str,
    candidate_set_sha256: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    stages = manifest.get("stages")
    stage = stages.get("2_ads") if isinstance(stages, dict) else None
    artifacts = manifest.get("artifacts")
    output_relative = output_path.relative_to(workspace).as_posix()
    report_relative = report_path.relative_to(workspace).as_posix()
    if not isinstance(stage, dict) or not isinstance(artifacts, dict):
        raise ValueError("applied formal decision provenance is missing")
    formal_run_id = stage.get("formal_run_id")
    output_record = artifacts.get(output_relative)
    report_record = artifacts.get(report_relative)
    if (
        stage.get("status") != "done"
        or stage.get("formal_decisions") != output_relative
        or stage.get("formal_report") != report_relative
        or stage.get("formal_decisions_sha256") != output_sha256
        or stage.get("formal_reviews_sha256") != reviews_sha256
        or stage.get("formal_draft_sha256") != draft_sha256
        or stage.get("scan_id") != scan_id
        or stage.get("candidate_set_sha256") != candidate_set_sha256
        or not isinstance(formal_run_id, str)
        or not isinstance(output_record, dict)
        or output_record.get("run_id") != formal_run_id
        or output_record.get("sha256") != output_sha256
        or not isinstance(report_record, dict)
        or report_record.get("run_id") != formal_run_id
    ):
        raise ValueError("applied formal decision provenance is stale")
    expected_report_sha256 = stage.get("formal_report_sha256")
    if (
        not report_path.is_file()
        or report_record.get("sha256") != expected_report_sha256
        or sha256_file(report_path) != expected_report_sha256
    ):
        raise ValueError("applied formal decision report provenance is stale")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("applied formal decision report cannot be read") from exc
    if not isinstance(report, dict):
        raise ValueError("applied formal decision report must be a JSON object")
    for field in FORMAL_IDENTITY_KEYS:
        if report.get(field) != provenance.get(field) or stage.get(field) != provenance.get(
            field
        ):
            raise ValueError(f"applied formal decision {field} provenance is stale")
    return report


def run(
    workspace: Path,
    input_value: str,
    reviews_value: str,
    draft_value: str,
    output_value: str,
) -> dict[str, Any]:
    with workspace_transaction_lock(workspace):
        return _run_locked(
            workspace,
            input_value,
            reviews_value,
            draft_value,
            output_value,
        )


def _run_locked(
    workspace: Path,
    input_value: str,
    reviews_value: str,
    draft_value: str,
    output_value: str,
) -> dict[str, Any]:
    workspace, _, _ = resolve_workspace_paths(workspace)
    candidates, input_paths, scan_report = load_current_ad_candidates(
        workspace,
        input_value,
        all_pages=True,
        require_complete=True,
    )
    candidate_reads = {
        f"candidate_page_{index}": path.relative_to(workspace).as_posix()
        for index, path in enumerate(input_paths, 1)
    }
    workspace, reads, writes = resolve_workspace_paths(
        workspace,
        reads={
            "candidate_input": input_value,
            **candidate_reads,
            "reviews": reviews_value,
            "draft": draft_value,
            "scan_report": "report/ads_scan_report.json",
        },
        writes={
            "output": output_value,
            "report": "report/ad_decision_formal_report.json",
        },
    )
    input_paths = [reads[name] for name in candidate_reads]
    duplicate_count = 0
    expected_count = reported_candidate_count(workspace)
    if expected_count is not None and len(candidates) != expected_count:
        raise ValueError(
            "formal compilation requires the complete scanned candidate set; "
            f"loaded={len(candidates)}, reported_total={expected_count}"
        )
    reviews_path = reads["reviews"]
    draft_path = reads["draft"]
    output_path = writes["output"]

    if not draft_path.is_file():
        raise ValueError(
            "formal compilation requires the current rule draft; run make_ad_decisions first"
        )
    drafts = load_jsonl(draft_path)
    draft_sha256 = sha256_file(draft_path)
    scan_id = scan_report["scan_id"]
    manifest = load_manifest(workspace)
    provenance = validate_current_draft_provenance(
        workspace,
        draft_path,
        drafts,
        candidates,
        scan_report,
        manifest=manifest,
    )
    review_pages_dir = workspace / "decisions" / "ads_agent_reviews" / "pages"
    paged_review_files = (
        sorted(review_pages_dir.glob("*.jsonl")) if review_pages_dir.is_dir() else []
    )
    paged_reviews_used = bool(paged_review_files)
    review_manifest_path = workspace / "candidates" / "ads_review_pages" / "manifest.json"
    review_manifest: dict[str, Any] | None = None
    if paged_reviews_used:
        try:
            review_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("current Agent review manifest is missing or invalid") from error
        if not isinstance(review_manifest, dict):
            raise ValueError("current Agent review manifest must be an object")
        current_review_identity = scan_identity.build_review_protocol_identity(
            target_page_bytes=ad_review_protocol.TARGET_PAGE_BYTES,
            hard_page_bytes=ad_review_protocol.HARD_PAGE_BYTES,
        )
        expected_review_identity = {
            "scan_id": scan_id,
            "candidate_set_sha256": scan_report["candidate_set_sha256"],
            "scan_rule_pack_sha256": provenance["scan_rule_pack_sha256"],
            "draft_rule_pack_sha256": provenance["draft_rule_pack_sha256"],
            "profile_present": provenance["profile_present"],
            "book_profile_sha256": provenance["book_profile_sha256"],
            "book_profile_file_sha256": provenance["book_profile_file_sha256"],
            "review_protocol_identity": current_review_identity,
            "review_protocol_identity_sha256": scan_identity.canonical_json_sha256(
                current_review_identity
            ),
        }
        for field, expected_value in expected_review_identity.items():
            if review_manifest.get(field) != expected_value:
                raise ValueError(f"current Agent review manifest {field} identity is stale")
        reviews = ad_review_protocol.merge_review_pages(workspace, candidates)
        reviews_bytes = ad_review_protocol.jsonl_bytes(reviews)
        reviews_sha256 = hashlib.sha256(reviews_bytes).hexdigest()
    else:
        reviews = load_jsonl(reviews_path)
        reviews_sha256 = sha256_file(reviews_path)
    if review_manifest is None and review_manifest_path.is_file():
        try:
            loaded_review_manifest = json.loads(
                review_manifest_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("current Agent review manifest is invalid") from error
        if not isinstance(loaded_review_manifest, dict):
            raise ValueError("current Agent review manifest must be an object")
        review_manifest = loaded_review_manifest
    review_page_provenance = {
        "review_pages_manifest": (
            review_manifest_path.relative_to(workspace).as_posix()
            if review_manifest is not None
            else None
        ),
        "review_pages_manifest_sha256": (
            sha256_file(review_manifest_path) if review_manifest is not None else None
        ),
        "review_projection_set_sha256": (
            review_manifest.get("projection_set_sha256")
            if review_manifest is not None
            else None
        ),
        "review_protocol_identity_sha256": (
            review_manifest.get("review_protocol_identity_sha256")
            if review_manifest is not None
            else None
        ),
    }
    decisions = compile_formal_decisions(
        candidates,
        reviews,
        drafts,
        scan_id=scan_id,
        provenance=provenance,
    )
    previous_sha256 = sha256_file(output_path) if output_path.exists() else None
    merged_reviews_changed = paged_reviews_used and (
        not reviews_path.is_file() or sha256_file(reviews_path) != reviews_sha256
    )
    previous_stage_status = str(
        manifest.get("stages", {}).get("2_ads", {}).get("status") or ""
    )
    output_bytes = b"".join(
        (
            json.dumps(decision, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        for decision in decisions
    )
    compiled_output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    decisions_changed = previous_sha256 != compiled_output_sha256
    if not decisions_changed and not merged_reviews_changed and previous_stage_status == "done":
        return load_preserved_formal_report(
            workspace,
            manifest,
            output_path,
            writes["report"],
            output_sha256=compiled_output_sha256,
            reviews_sha256=reviews_sha256,
            draft_sha256=draft_sha256,
            scan_id=scan_id,
            candidate_set_sha256=scan_report["candidate_set_sha256"],
            provenance=provenance,
        )
    with WorkspaceTransaction(workspace) as transaction:
        if paged_reviews_used:
            staged_reviews = transaction.stage_path(reviews_path)
            write_jsonl(staged_reviews, reviews)
            if sha256_file(staged_reviews) != reviews_sha256:
                raise ValueError("merged Agent review serialization is not deterministic")
        staged_output = transaction.stage_path(output_path)
        write_jsonl(staged_output, decisions)
        output_sha256 = sha256_file(staged_output)
        if output_sha256 != compiled_output_sha256:
            raise ValueError("formal decision serialization is not deterministic")
        report = {
            "scan_id": scan_id,
            "candidate_set_sha256": scan_report["candidate_set_sha256"],
            **provenance,
            "inputs": [str(path.relative_to(workspace)) for path in input_paths],
            "reviews": str(reviews_path.relative_to(workspace)),
            "reviews_sha256": reviews_sha256,
            "draft": str(draft_path.relative_to(workspace)) if draft_path.exists() else None,
            "draft_sha256": draft_sha256,
            "output": str(output_path.relative_to(workspace)),
            "candidate_count": len(candidates),
            "reported_candidate_count": expected_count,
            "duplicate_candidate_count": duplicate_count,
            "review_count": len(reviews),
            "paged_reviews_used": paged_reviews_used,
            "review_page_count": len(paged_review_files),
            **review_page_provenance,
            "decision_count": len(decisions),
            "by_verdict": count_by_verdict(decisions),
            "formal_decisions_changed": decisions_changed,
            "formal_decisions_sha256": output_sha256,
            "previous_stage_status": previous_stage_status or None,
        }
        update_formal_stage = should_mark_formal_ready(decisions_changed, previous_stage_status)
        if not update_formal_stage:
            report["manifest_stage_preserved"] = "done"
        staged_report = transaction.stage_path(writes["report"])
        write_json(staged_report, report)
        report_sha256 = sha256_file(staged_report)
        transaction.commit(
            {
                "2_ads": (
                    "formal_decisions_ready" if update_formal_stage else previous_stage_status,
                    {
                        "formal_decisions": str(output_path.relative_to(workspace)),
                        "formal_report": "report/ad_decision_formal_report.json",
                        "formal_decisions_sha256": output_sha256,
                        "formal_report_sha256": report_sha256,
                        "formal_reviews_sha256": reviews_sha256,
                        "formal_draft_sha256": draft_sha256,
                        "formal_decision_count": len(decisions),
                        "formal_by_verdict": report["by_verdict"],
                        "scan_id": scan_id,
                        "candidate_set_sha256": scan_report["candidate_set_sha256"],
                        **review_page_provenance,
                        **provenance,
                    }
                    if update_formal_stage
                    else {},
                )
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile complete local-Agent reviews into executable formal ad decisions."
    )
    parser.add_argument("workspace", help="Path to the .cleanwork directory.")
    parser.add_argument("--input", default="candidates/ads_pages")
    parser.add_argument("--reviews", default="decisions/ads_agent_reviews.jsonl")
    parser.add_argument("--draft", default="decisions/ads_decisions.draft.jsonl")
    parser.add_argument("--output", default="decisions/ads_decisions.jsonl")
    args = parser.parse_args()
    report = run(
        workspace=Path(args.workspace).resolve(),
        input_value=args.input,
        reviews_value=args.reviews,
        draft_value=args.draft,
        output_value=args.output,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
