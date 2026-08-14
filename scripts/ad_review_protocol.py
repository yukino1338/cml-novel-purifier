from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

import ad_decision_policy


REVIEW_PAGE_SCHEMA = "cml.ad-review-page.v1"
REVIEW_MANIFEST_SCHEMA = "cml.ad-review-manifest.v1"
REVIEW_ATTESTATION_SCHEMA = "cml.ad-review-attestation.v1"
REVIEW_GROUP_SCHEMA = "cml.review-group.v1"
TARGET_PAGE_BYTES = 32 * 1024
HARD_PAGE_BYTES = 48 * 1024
CONTEXT_CHARS = 160
EXPANDED_CONTEXT_CHARS = 320
ORIGINAL_PREVIEW_CHARS = 512
SIGNAL_LABELS = {
    "domain": "域名或网址",
    "email": "邮箱联系方式",
    "reader_site": "站点或阅读平台",
    "promotion": "推广引导",
    "watermark": "来源或水印文字",
    "copy_marker": "转载或整理标记",
}


class ReviewProtocolError(ValueError):
    """Raised when a review projection or paged response is incomplete or stale."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def json_file_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        for record in records
    )


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _identity_payload(identity: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "scan_id",
        "candidate_set_sha256",
        "scan_rule_pack_sha256",
        "draft_rule_pack_sha256",
        "profile_present",
        "book_profile_sha256",
        "book_profile_file_sha256",
        "review_protocol_identity",
        "review_protocol_identity_sha256",
    )
    result: dict[str, Any] = {}
    for key in required:
        if key not in identity:
            raise ReviewProtocolError(f"review identity is missing {key}")
        result[key] = identity[key]
    for key in (
        "scan_id",
        "candidate_set_sha256",
        "scan_rule_pack_sha256",
        "draft_rule_pack_sha256",
        "book_profile_sha256",
        "review_protocol_identity_sha256",
    ):
        if not _sha256(result[key]):
            raise ReviewProtocolError(f"review identity {key} is invalid")
    if not isinstance(result["profile_present"], bool):
        raise ReviewProtocolError("review identity profile_present is invalid")
    if result["profile_present"]:
        if not _sha256(result["book_profile_file_sha256"]):
            raise ReviewProtocolError("review identity profile file hash is invalid")
    elif result["book_profile_file_sha256"] is not None:
        raise ReviewProtocolError("absent review profile must not have a file hash")
    if not isinstance(result["review_protocol_identity"], dict):
        raise ReviewProtocolError("review protocol identity must be an object")
    if canonical_sha256(result["review_protocol_identity"]) != result[
        "review_protocol_identity_sha256"
    ]:
        raise ReviewProtocolError("review protocol identity hash is stale")
    return result


def _location(anchor: Mapping[str, Any]) -> dict[str, Any]:
    reference = anchor.get("chapter")
    kind = "chapter"
    if not isinstance(reference, Mapping):
        reference = anchor.get("locator")
        kind = str(reference.get("kind") or "location") if isinstance(reference, Mapping) else "location"
    result: dict[str, Any] = {"kind": kind}
    if isinstance(reference, Mapping):
        for key in ("index", "title"):
            value = reference.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                result[key] = value
    return result


def _wide_context(
    candidate: Mapping[str, Any],
    draft: Mapping[str, Any],
    previous_formal: Mapping[str, Any] | None,
) -> bool:
    if candidate.get("mutation_guard") or draft.get("protected_terms"):
        return True
    if previous_formal is None:
        return False
    return (
        previous_formal.get("candidate_fingerprint") == candidate.get("candidate_fingerprint")
        and previous_formal.get("verdict") != draft.get("verdict")
    )


def _occurrence_projection(
    source_text: str,
    anchor: Mapping[str, Any],
    ordinal: int,
    width: int,
) -> dict[str, Any]:
    start = anchor.get("offset")
    end = anchor.get("end")
    original = anchor.get("original")
    anchor_id = anchor.get("anchor_id")
    line = anchor.get("line")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start <= end <= len(source_text)
        or not isinstance(original, str)
        or source_text[start:end] != original
        or not isinstance(anchor_id, str)
        or not anchor_id
        or not isinstance(line, int)
        or isinstance(line, bool)
        or line < 1
    ):
        raise ReviewProtocolError("review occurrence does not match the immutable source")
    before = source_text[max(0, start - width) : start]
    after = source_text[end : min(len(source_text), end + width)]
    preview = original[:ORIGINAL_PREVIEW_CHARS]
    context_payload = {
        "before": before,
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "after": after,
        "line": line,
        "location": _location(anchor),
        "ordinal": ordinal,
    }
    return {
        "ordinal": ordinal,
        "anchor_id": anchor_id,
        "text_sha256": context_payload["original_sha256"],
        "physical_line": line,
        "location": context_payload["location"],
        "context_before": before,
        "original_preview": preview,
        "original_truncated": len(preview) != len(original),
        "context_after": after,
        "context_sha256": canonical_sha256(context_payload),
    }


def _shape(candidate: Mapping[str, Any], draft: Mapping[str, Any]) -> dict[str, Any]:
    anchors = candidate.get("anchors")
    location_kinds = []
    if isinstance(anchors, list):
        location_kinds = [_location(anchor).get("kind") for anchor in anchors if isinstance(anchor, Mapping)]
    return {
        "splice_strategy": draft.get("splice_strategy"),
        "scope": "segment" if candidate.get("mutation_guard") else "whole",
        "layer": candidate.get("layer"),
        "detector": candidate.get("detector"),
        "location_kinds": location_kinds,
    }


def _delete_exact_allowed(candidate: Mapping[str, Any], draft: Mapping[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    anchors = candidate.get("anchors")
    occurrence_count = candidate.get("occurrence_count")
    evidence = draft.get("evidence")
    automatic = [
        item.get("value")
        for item in evidence
        if isinstance(item, Mapping) and item.get("type") == "automatic_delete_gate"
    ] if isinstance(evidence, list) else []
    segment_plan = candidate.get("edit_plan")
    segment_allowed = False
    if isinstance(candidate, dict) and segment_plan is not None:
        eligibility = ad_decision_policy.delete_eligibility(
            candidate,
            protection_conflict=bool(draft.get("protected_terms")),
        )
        segment_allowed = bool(eligibility["segment_delete_allowed"])
        if not segment_allowed:
            blockers.extend(eligibility["segment_delete_blockers"])
        if draft.get("splice_strategy") != "exact_segment":
            blockers.append("splice_strategy_invalid")
        plan_id = segment_plan.get("edit_plan_id") if isinstance(segment_plan, Mapping) else None
        if draft.get("edit_plan_id") != plan_id:
            blockers.append("edit_plan_id_stale")
    else:
        if draft.get("verdict") != "delete":
            blockers.append("system_draft_not_delete")
        if not any(
            isinstance(value, Mapping)
            and value.get("locator") is True
            and value.get("promotion_intent") is True
            for value in automatic
        ):
            blockers.append("locator_intent_gate_missing")
    if not isinstance(anchors, list) or not anchors:
        blockers.append("anchors_missing")
    elif (
        not isinstance(occurrence_count, int)
        or isinstance(occurrence_count, bool)
        or occurrence_count != len(anchors)
    ):
        blockers.append("occurrence_coverage_incomplete")
    if candidate.get("anchors_truncated") is not False:
        blockers.append("anchors_truncated")
    if draft.get("protected_terms"):
        blockers.append("protected_terms")
    if (candidate.get("mutation_guard") or draft.get("mutation_guard")) and not segment_allowed:
        blockers.append("mutation_guard")
    if not segment_plan and draft.get("splice_strategy") not in {"exact", "fallback_newline", "remove_paragraph"}:
        blockers.append("splice_strategy_invalid")
    return not blockers, sorted(set(blockers))


def build_review_groups(
    candidates: list[dict[str, Any]], drafts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if len(candidates) != len(drafts):
        raise ReviewProtocolError("review groups require one draft per candidate")
    buckets: dict[str, list[tuple[dict[str, Any], dict[str, Any], list[str]]]] = defaultdict(list)
    for candidate, draft in zip(candidates, drafts):
        if candidate.get("candidate_id") != draft.get("candidate_id"):
            raise ReviewProtocolError("review group candidate and draft order differ")
        delete_allowed, blockers = _delete_exact_allowed(candidate, draft)
        if delete_allowed:
            group_kind = "delete_exact"
            key_payload = {"kind": group_kind, "shape": _shape(candidate, draft)}
        else:
            suggested = draft.get("verdict")
            group_kind = "keep_review" if suggested == "keep" else "uncertain_review"
            source = draft.get("source_candidate")
            source = source if isinstance(source, Mapping) else {}
            key_payload = {
                "kind": group_kind,
                "task": candidate.get("detector"),
                "signals": sorted(str(value) for value in source.get("signals", []) if isinstance(value, str)),
                "scope": _shape(candidate, draft)["scope"],
                "system_suggestion": suggested,
            }
        buckets[canonical_sha256(key_payload)].append((candidate, draft, blockers))

    groups: list[dict[str, Any]] = []
    for bucket_key in sorted(buckets):
        members = buckets[bucket_key]
        kind = "delete_exact" if all(_delete_exact_allowed(item, draft)[0] for item, draft, _ in members) else (
            "keep_review" if all(draft.get("verdict") == "keep" for _, draft, _ in members) else "uncertain_review"
        )
        coverage = [
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "anchor_ids": [anchor["anchor_id"] for anchor in candidate.get("anchors", [])],
                **(
                    {"edit_plan_id": candidate["edit_plan"]["edit_plan_id"]}
                    if isinstance(candidate.get("edit_plan"), Mapping)
                    else {}
                ),
            }
            for candidate, _, _ in members
        ]
        member_edit_plan_ids = {
            candidate["candidate_id"]: candidate["edit_plan"]["edit_plan_id"]
            for candidate, _, _ in members
            if isinstance(candidate.get("edit_plan"), Mapping)
        }
        coverage_sha256 = canonical_sha256(coverage)
        group_id = "RG-" + canonical_sha256(
            {"group_kind": kind, "coverage_sha256": coverage_sha256}
        )[:20]
        blockers = sorted({reason for _, _, reasons in members for reason in reasons})
        groups.append(
            {
                "schema": REVIEW_GROUP_SCHEMA,
                "review_group_id": group_id,
                "group_kind": kind,
                "member_candidate_ids": [item["candidate_id"] for item, _, _ in members],
                "member_fingerprints": [item["candidate_fingerprint"] for item, _, _ in members],
                "member_coverage_sha256": coverage_sha256,
                "member_edit_plan_ids": member_edit_plan_ids,
                "shared_reason_code": bucket_key,
                "delete_group_allowed": kind == "delete_exact" and not blockers,
                "delete_group_blockers": blockers,
                "delete_shape": (
                    _shape(members[0][0], members[0][1])
                    if kind == "delete_exact" and not blockers
                    else None
                ),
            }
        )
    return groups


def _record(
    candidate: dict[str, Any],
    draft: dict[str, Any],
    occurrences: list[dict[str, Any]],
    start: int,
    end: int,
    group_id: str,
    *,
    formal_conflict: bool,
) -> dict[str, Any]:
    signals = candidate.get("signals")
    signal_values = (
        sorted(str(value) for value in signals if isinstance(value, str))
        if isinstance(signals, list)
        else []
    )
    segment_identity: dict[str, Any] | None = None
    edit_plan = candidate.get("edit_plan")
    if isinstance(edit_plan, dict):
        normalized_plan = ad_decision_policy.normalize_edit_plan(edit_plan, candidate)
        segment_identity = {
            "schema": normalized_plan["schema"],
            "edit_plan_id": normalized_plan["edit_plan_id"],
            "occurrence_coverage_sha256": normalized_plan[
                "occurrence_coverage_sha256"
            ],
        }
    elif isinstance(candidate.get("segment_id"), str):
        segment_identity = {"segment_id": candidate["segment_id"]}
        for key in ("segment_fingerprint", "segment_text_sha256"):
            if _sha256(candidate.get(key)):
                segment_identity[key] = candidate[key]
    return {
        "record_type": "candidate_occurrences",
        "candidate_id": candidate["candidate_id"],
        "candidate_fingerprint": candidate["candidate_fingerprint"],
        "review_group_id": group_id,
        "system_suggestion": draft.get("verdict"),
        "risk": draft.get("risk") or candidate.get("risk_hint"),
        "segment_identity": segment_identity,
        "signals": signal_values,
        "signal_labels": [SIGNAL_LABELS.get(value, value) for value in signal_values],
        "signal_summary": str(candidate.get("reason") or "")[:300],
        "formal_conflict": formal_conflict,
        "occurrence_range": [start + 1, end],
        "candidate_occurrence_count": candidate.get("occurrence_count"),
        "projected_occurrence_count": len(occurrences[start:end]),
        "occurrence_coverage_sha256": canonical_sha256(
            [
                {"anchor_id": item["anchor_id"], "context_sha256": item["context_sha256"]}
                for item in occurrences[start:end]
            ]
        ),
        "occurrences": occurrences[start:end],
    }


def _oversize_record(record: Mapping[str, Any], size_bytes: int) -> dict[str, Any]:
    occurrences = record.get("occurrences")
    occurrence = occurrences[0] if isinstance(occurrences, list) and occurrences else {}
    return {
        "record_type": "review_record_oversize",
        "candidate_id": record.get("candidate_id"),
        "candidate_fingerprint": record.get("candidate_fingerprint"),
        "review_group_id": record.get("review_group_id"),
        "occurrence_range": record.get("occurrence_range"),
        "anchor_id": occurrence.get("anchor_id") if isinstance(occurrence, Mapping) else None,
        "text_sha256": occurrence.get("text_sha256") if isinstance(occurrence, Mapping) else None,
        "required_record_bytes": size_bytes,
        "blocking_reason": "review_record_oversize",
    }


def _page_payload(
    number: int,
    records: list[dict[str, Any]],
    identity: Mapping[str, Any],
    projection_set_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": REVIEW_PAGE_SCHEMA,
        "page_number": number,
        "projection_set_sha256": projection_set_sha256,
        **identity,
        "records": records,
    }


def build_review_projection(
    candidates: list[dict[str, Any]],
    drafts: list[dict[str, Any]],
    source_text: str,
    identity: Mapping[str, Any],
    *,
    previous_formal: list[dict[str, Any]] | None = None,
    target_bytes: int = TARGET_PAGE_BYTES,
    hard_limit_bytes: int = HARD_PAGE_BYTES,
) -> dict[str, Any]:
    if (
        not isinstance(target_bytes, int)
        or isinstance(target_bytes, bool)
        or not isinstance(hard_limit_bytes, int)
        or isinstance(hard_limit_bytes, bool)
        or target_bytes < 1024
        or hard_limit_bytes < target_bytes
    ):
        raise ReviewProtocolError("review page byte limits are invalid")
    identity_payload = _identity_payload(identity)
    if len(candidates) != len(drafts):
        raise ReviewProtocolError("review projection requires one draft per candidate")
    groups = build_review_groups(candidates, drafts)
    group_by_candidate = {
        candidate_id: group["review_group_id"]
        for group in groups
        for candidate_id in group["member_candidate_ids"]
    }
    prior = {
        item.get("candidate_id"): item
        for item in (previous_formal or [])
        if isinstance(item, dict) and isinstance(item.get("candidate_id"), str)
    }
    projected: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], bool]] = []
    occurrence_identity: list[dict[str, Any]] = []
    for candidate, draft in zip(candidates, drafts):
        if candidate.get("candidate_id") != draft.get("candidate_id"):
            raise ReviewProtocolError("review projection candidate and draft order differ")
        anchors = candidate.get("anchors")
        if not isinstance(anchors, list) or not all(isinstance(anchor, Mapping) for anchor in anchors):
            raise ReviewProtocolError("review projection anchors are invalid")
        conflict = _wide_context(candidate, draft, prior.get(candidate["candidate_id"]))
        width = EXPANDED_CONTEXT_CHARS if conflict else CONTEXT_CHARS
        occurrences = [
            _occurrence_projection(source_text, anchor, ordinal, width)
            for ordinal, anchor in enumerate(anchors, 1)
        ]
        if candidate.get("edit_plan") is not None:
            previews = {
                item["anchor_id"]: item
                for item in ad_decision_policy.edit_plan_preview(candidate)
            }
            plan = ad_decision_policy.normalize_edit_plan(
                candidate["edit_plan"],
                candidate,
            )
            occurrence_plan_by_anchor = {
                item["anchor_id"]: item for item in plan["occurrence_plans"]
            }
            for occurrence in occurrences:
                anchor_id = occurrence["anchor_id"]
                preview = previews[anchor_id]
                occurrence_plan = occurrence_plan_by_anchor[anchor_id]
                occurrence["edit_plan_id"] = plan["edit_plan_id"]
                occurrence["boundary_kind"] = occurrence_plan["boundary_kind"]
                for key in ("keep_text", "delete_text", "after_text"):
                    value = preview[key]
                    occurrence[key + "_preview"] = value[:ORIGINAL_PREVIEW_CHARS]
                    occurrence[key + "_sha256"] = hashlib.sha256(
                        value.encode("utf-8")
                    ).hexdigest()
                    occurrence[key + "_truncated"] = len(value) > ORIGINAL_PREVIEW_CHARS
        occurrence_identity.extend(
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "anchor_id": item["anchor_id"],
                "context_sha256": item["context_sha256"],
            }
            for item in occurrences
        )
        projected.append((candidate, draft, occurrences, conflict))
    projection_set_sha256 = canonical_sha256(
        {"occurrences": occurrence_identity, "groups": groups}
    )

    records: list[dict[str, Any]] = []
    oversize_count = 0
    for candidate, draft, occurrences, conflict in projected:
        start = 0
        if not occurrences:
            records.append(
                _record(
                    candidate,
                    draft,
                    occurrences,
                    0,
                    0,
                    group_by_candidate[candidate["candidate_id"]],
                    formal_conflict=conflict,
                )
            )
        while start < len(occurrences):
            best_end = start
            end = start + 1
            while end <= len(occurrences):
                candidate_record = _record(
                    candidate,
                    draft,
                    occurrences,
                    start,
                    end,
                    group_by_candidate[candidate["candidate_id"]],
                    formal_conflict=conflict,
                )
                probe = _page_payload(1, [candidate_record], identity_payload, projection_set_sha256)
                if len(json_file_bytes(probe)) > target_bytes and best_end > start:
                    break
                if len(json_file_bytes(probe)) > hard_limit_bytes:
                    break
                best_end = end
                end += 1
            if best_end == start:
                raw = _record(
                    candidate,
                    draft,
                    occurrences,
                    start,
                    start + 1,
                    group_by_candidate[candidate["candidate_id"]],
                    formal_conflict=conflict,
                )
                size = len(json_file_bytes(_page_payload(1, [raw], identity_payload, projection_set_sha256)))
                marker = _oversize_record(raw, size)
                if len(json_file_bytes(_page_payload(1, [marker], identity_payload, projection_set_sha256))) > hard_limit_bytes:
                    raise ReviewProtocolError("review_record_oversize marker exceeds the hard page limit")
                records.append(marker)
                oversize_count += 1
                start += 1
            else:
                records.append(
                    _record(
                        candidate,
                        draft,
                        occurrences,
                        start,
                        best_end,
                        group_by_candidate[candidate["candidate_id"]],
                        formal_conflict=conflict,
                    )
                )
                start = best_end

    pages: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    for record in records:
        probe = _page_payload(
            len(pages) + 1,
            [*page_records, record],
            identity_payload,
            projection_set_sha256,
        )
        if page_records and len(json_file_bytes(probe)) > target_bytes:
            pages.append(_page_payload(len(pages) + 1, page_records, identity_payload, projection_set_sha256))
            page_records = []
            probe = _page_payload(len(pages) + 1, [record], identity_payload, projection_set_sha256)
        if len(json_file_bytes(probe)) > hard_limit_bytes:
            raise ReviewProtocolError("review page exceeds the hard byte limit")
        page_records.append(record)
    if page_records or not pages:
        pages.append(_page_payload(len(pages) + 1, page_records, identity_payload, projection_set_sha256))

    page_manifest: list[dict[str, Any]] = []
    for page in pages:
        encoded = json_file_bytes(page)
        candidates_on_page = sorted(
            {
                str(record.get("candidate_id"))
                for record in page["records"]
                if isinstance(record.get("candidate_id"), str)
            }
        )
        page_occurrences = [
            {"candidate_id": record["candidate_id"], "anchor_id": occurrence["anchor_id"], "context_sha256": occurrence["context_sha256"]}
            for record in page["records"]
            if record.get("record_type") == "candidate_occurrences"
            for occurrence in record["occurrences"]
        ]
        page_manifest.append(
            {
                "page_number": page["page_number"],
                "file": f"candidates/ads_review_pages/page_{page['page_number']:04d}.json",
                "review_file": f"decisions/ads_agent_reviews/pages/page_{page['page_number']:04d}.jsonl",
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "candidate_ids": candidates_on_page,
                "candidate_count": len(candidates_on_page),
                "occurrence_count": len(page_occurrences),
                "occurrence_coverage_sha256": canonical_sha256(page_occurrences),
                "record_count": len(page["records"]),
            }
        )
    manifest = {
        "schema": REVIEW_MANIFEST_SCHEMA,
        "target_page_bytes": target_bytes,
        "hard_page_bytes": hard_limit_bytes,
        **identity_payload,
        "projection_set_sha256": projection_set_sha256,
        "candidate_count": len(candidates),
        "occurrence_count": sum(
            int(candidate.get("occurrence_count") or 0) for candidate in candidates
        ),
        "projected_occurrence_count": len(occurrence_identity),
        "truncated_candidate_count": sum(
            candidate.get("anchors_truncated") is True for candidate in candidates
        ),
        "record_count": len(records),
        "review_group_count": len(groups),
        "review_record_oversize_count": oversize_count,
        "review_groups": groups,
        "pages": page_manifest,
    }
    return {"manifest": manifest, "pages": pages}


def projection_artifacts(projection: Mapping[str, Any]) -> dict[str, bytes]:
    manifest = projection.get("manifest")
    pages = projection.get("pages")
    if not isinstance(manifest, dict) or not isinstance(pages, list):
        raise ReviewProtocolError("review projection is invalid")
    result = {"candidates/ads_review_pages/manifest.json": json_file_bytes(manifest)}
    declared = manifest.get("pages")
    if not isinstance(declared, list) or len(declared) != len(pages):
        raise ReviewProtocolError("review projection manifest page count is invalid")
    for entry, page in zip(declared, pages):
        if not isinstance(entry, dict) or not isinstance(page, dict):
            raise ReviewProtocolError("review projection page is invalid")
        encoded = json_file_bytes(page)
        if len(encoded) != entry.get("bytes") or hashlib.sha256(encoded).hexdigest() != entry.get("sha256"):
            raise ReviewProtocolError("review projection page bytes drifted")
        result[str(entry["file"])] = encoded
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReviewProtocolError(f"review protocol JSON is unreadable: {path.name}") from error
    if not isinstance(value, dict):
        raise ReviewProtocolError(f"review protocol JSON must be an object: {path.name}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise ReviewProtocolError(f"review page is unreadable: {path.name}") from error
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ReviewProtocolError(f"review page has invalid JSON at {path.name}:{number}") from error
        if not isinstance(value, dict):
            raise ReviewProtocolError("review page records must be objects")
        records.append(value)
    return records


_REVIEW_BINDING_KEYS = {
    "record_type",
    "review_group_id",
    "member_candidate_ids",
    "member_fingerprints",
    "member_coverage_sha256",
    "member_edit_plan_ids",
    "member_keep_bases",
}


def _compiler_review(record: Mapping[str, Any], candidate: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in record.items() if key not in _REVIEW_BINDING_KEYS}
    result["scan_id"] = manifest["scan_id"]
    result["candidate_id"] = candidate["candidate_id"]
    result["candidate_fingerprint"] = candidate["candidate_fingerprint"]
    return result


def merge_review_pages(
    workspace: Path,
    candidates: list[dict[str, Any]],
    *,
    manifest_value: str = "candidates/ads_review_pages/manifest.json",
    reviews_dir_value: str = "decisions/ads_agent_reviews/pages",
) -> list[dict[str, Any]]:
    workspace = workspace.resolve()
    manifest_path = (workspace / manifest_value).resolve()
    reviews_dir = (workspace / reviews_dir_value).resolve()
    try:
        manifest_path.relative_to(workspace)
        reviews_dir.relative_to(workspace)
    except ValueError as error:
        raise ReviewProtocolError("review protocol path escapes the workspace") from error
    manifest = _load_json(manifest_path)
    if manifest.get("schema") != REVIEW_MANIFEST_SCHEMA:
        raise ReviewProtocolError("review manifest schema is stale")
    if manifest.get("review_record_oversize_count"):
        raise ReviewProtocolError("review_record_oversize blocks review merge")
    pages = manifest.get("pages")
    groups = manifest.get("review_groups")
    if not isinstance(pages, list) or not isinstance(groups, list):
        raise ReviewProtocolError("review manifest pages or groups are invalid")
    candidate_map = {item.get("candidate_id"): item for item in candidates}
    declared_member_ids = [
        candidate_id
        for group in groups
        if isinstance(group, dict)
        for candidate_id in group.get("member_candidate_ids", [])
    ]
    if (
        len(candidate_map) != len(candidates)
        or len(declared_member_ids) != len(set(declared_member_ids))
        or set(candidate_map) != set(declared_member_ids)
    ):
        raise ReviewProtocolError("review manifest candidate set is stale")
    candidate_set_sha256 = canonical_sha256(
        sorted(str(item.get("candidate_fingerprint")) for item in candidates)
    )
    if (
        manifest.get("candidate_count") != len(candidates)
        or manifest.get("candidate_set_sha256") != candidate_set_sha256
        or manifest.get("review_group_count") != len(groups)
    ):
        raise ReviewProtocolError("review manifest counts or candidate hash are stale")
    group_map: dict[str, dict[str, Any]] = {}
    for group in groups:
        if (
            not isinstance(group, dict)
            or group.get("schema") != REVIEW_GROUP_SCHEMA
            or not isinstance(group.get("review_group_id"), str)
            or group["review_group_id"] in group_map
            or not isinstance(group.get("member_candidate_ids"), list)
            or not isinstance(group.get("member_fingerprints"), list)
            or len(group["member_candidate_ids"]) != len(group["member_fingerprints"])
            or not isinstance(group.get("member_edit_plan_ids"), dict)
            or not set(group["member_edit_plan_ids"]).issubset(
                set(group["member_candidate_ids"])
            )
        ):
            raise ReviewProtocolError("review manifest group schema is invalid")
        expected_member_edit_plan_ids = {
            candidate_id: candidate_map[candidate_id]["edit_plan"]["edit_plan_id"]
            for candidate_id in group["member_candidate_ids"]
            if isinstance(candidate_map[candidate_id].get("edit_plan"), Mapping)
        }
        if group["member_edit_plan_ids"] != expected_member_edit_plan_ids:
            raise ReviewProtocolError("review manifest edit plan coverage is stale")
        group_map[group["review_group_id"]] = group
    group_by_candidate = {
        candidate_id: group_id
        for group_id, group in group_map.items()
        for candidate_id in group.get("member_candidate_ids", [])
    }
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    expected_names = {f"page_{index:04d}.jsonl" for index in range(1, len(pages) + 1)}
    actual_names = {path.name for path in reviews_dir.glob("*.jsonl")} if reviews_dir.is_dir() else set()
    if actual_names != expected_names:
        raise ReviewProtocolError(
            f"review page set is incomplete or has extras; missing={sorted(expected_names-actual_names)}, extra={sorted(actual_names-expected_names)}"
        )

    individual: dict[str, dict[str, Any]] = {}
    group_verdicts: dict[str, dict[str, Any]] = {}
    seen_attestations: set[int] = set()
    projected_occurrences: list[dict[str, Any]] = []
    for expected_number, entry in enumerate(pages, 1):
        if not isinstance(entry, dict) or entry.get("page_number") != expected_number:
            raise ReviewProtocolError("review manifest page order is invalid")
        projection_path = (workspace / str(entry.get("file") or "")).resolve()
        try:
            projection_path.relative_to(workspace)
        except ValueError as error:
            raise ReviewProtocolError("review projection page escapes the workspace") from error
        encoded = projection_path.read_bytes() if projection_path.is_file() else b""
        if len(encoded) != entry.get("bytes") or hashlib.sha256(encoded).hexdigest() != entry.get("sha256"):
            raise ReviewProtocolError("review projection page hash drifted")
        projection = _load_json(projection_path)
        if projection.get("page_number") != expected_number or projection.get("projection_set_sha256") != manifest.get("projection_set_sha256"):
            raise ReviewProtocolError("review projection page identity is stale")
        for field in (
            "scan_id",
            "candidate_set_sha256",
            "scan_rule_pack_sha256",
            "draft_rule_pack_sha256",
            "profile_present",
            "book_profile_sha256",
            "book_profile_file_sha256",
            "review_protocol_identity",
            "review_protocol_identity_sha256",
        ):
            if projection.get(field) != manifest.get(field):
                raise ReviewProtocolError(f"review projection page {field} identity is stale")
        projection_records = projection.get("records")
        if not isinstance(projection_records, list):
            raise ReviewProtocolError("review projection records are invalid")
        page_occurrences = [
            {
                "candidate_id": projection_record.get("candidate_id"),
                "anchor_id": occurrence.get("anchor_id"),
                "context_sha256": occurrence.get("context_sha256"),
            }
            for projection_record in projection_records
            if isinstance(projection_record, dict)
            and projection_record.get("record_type") == "candidate_occurrences"
            for occurrence in projection_record.get("occurrences", [])
            if isinstance(occurrence, dict)
        ]
        if (
            canonical_sha256(page_occurrences) != entry.get("occurrence_coverage_sha256")
            or len(page_occurrences) != entry.get("occurrence_count")
        ):
            raise ReviewProtocolError("review projection occurrence coverage drifted")
        projected_occurrences.extend(
            {
                "candidate_id": occurrence["candidate_id"],
                "candidate_fingerprint": candidate_map.get(occurrence["candidate_id"], {}).get(
                    "candidate_fingerprint"
                ),
                "anchor_id": occurrence["anchor_id"],
                "context_sha256": occurrence["context_sha256"],
            }
            for occurrence in page_occurrences
        )
        records = _load_jsonl(reviews_dir / f"page_{expected_number:04d}.jsonl")
        if not records or records[0].get("record_type") != "page_attestation":
            raise ReviewProtocolError("review page attestation is missing")
        attestation = records[0]
        expected_attestation = {
            "record_type": "page_attestation",
            "schema": REVIEW_ATTESTATION_SCHEMA,
            "page_number": expected_number,
            "page_sha256": entry["sha256"],
            "manifest_sha256": manifest_sha256,
            "projection_set_sha256": manifest["projection_set_sha256"],
            "occurrence_coverage_sha256": entry["occurrence_coverage_sha256"],
        }
        if attestation != expected_attestation or expected_number in seen_attestations:
            raise ReviewProtocolError("review page attestation is stale, reordered, or duplicated")
        seen_attestations.add(expected_number)
        visible_ids = set(entry.get("candidate_ids", []))
        for record in records[1:]:
            record_type = record.get("record_type")
            if record_type == "candidate_verdict":
                candidate_id = record.get("candidate_id")
                if candidate_id not in visible_ids or candidate_id not in candidate_map:
                    raise ReviewProtocolError("review contains an extra or misplaced candidate")
                if (
                    record.get("candidate_fingerprint")
                    != candidate_map[candidate_id].get("candidate_fingerprint")
                    or record.get("review_group_id") != group_by_candidate.get(candidate_id)
                ):
                    raise ReviewProtocolError("candidate review identity or hash drifted")
                edit_plan = candidate_map[candidate_id].get("edit_plan")
                expected_edit_plan_id = (
                    edit_plan.get("edit_plan_id")
                    if isinstance(edit_plan, Mapping)
                    else None
                )
                if record.get("verdict") == "delete" and expected_edit_plan_id is not None:
                    if (
                        record.get("edit_plan_id") != expected_edit_plan_id
                        or record.get("splice_strategy") != "exact_segment"
                    ):
                        raise ReviewProtocolError(
                            "candidate verdict edit plan identity is stale"
                        )
                elif "edit_plan_id" in record or record.get("splice_strategy") == "exact_segment":
                    raise ReviewProtocolError(
                        "candidate verdict cannot reference an edit plan"
                    )
                if candidate_id in individual:
                    previous = individual[candidate_id]
                    reason = "conflicting verdict" if previous.get("verdict") != record.get("verdict") else "duplicate candidate verdict"
                    raise ReviewProtocolError(reason)
                individual[candidate_id] = record
            elif record_type == "group_verdict":
                group_id = record.get("review_group_id")
                if not isinstance(group_id, str):
                    raise ReviewProtocolError("group verdict has no review_group_id")
                declared_group = group_map.get(group_id)
                if declared_group is None or not visible_ids.intersection(
                    declared_group.get("member_candidate_ids", [])
                ):
                    raise ReviewProtocolError("group verdict is extra or misplaced")
                if group_id in group_verdicts:
                    previous = group_verdicts[group_id]
                    reason = "conflicting verdict" if previous.get("verdict") != record.get("verdict") else "duplicate group verdict"
                    raise ReviewProtocolError(reason)
                group_verdicts[group_id] = record
            else:
                raise ReviewProtocolError("review page contains an unknown record type")

    expected_anchor_pairs = [
        (str(candidate["candidate_id"]), str(anchor.get("anchor_id")))
        for candidate in candidates
        for anchor in candidate.get("anchors", [])
        if isinstance(anchor, Mapping)
    ]
    actual_anchor_pairs = [
        (str(item.get("candidate_id")), str(item.get("anchor_id")))
        for item in projected_occurrences
    ]
    if (
        actual_anchor_pairs != expected_anchor_pairs
        or manifest.get("projected_occurrence_count") != len(expected_anchor_pairs)
        or manifest.get("record_count")
        != sum(int(entry.get("record_count") or 0) for entry in pages)
        or canonical_sha256({"occurrences": projected_occurrences, "groups": groups})
        != manifest.get("projection_set_sha256")
    ):
        raise ReviewProtocolError("review projection omits, duplicates, or reorders occurrences")

    expanded: dict[str, dict[str, Any]] = dict(individual)
    for group_id, record in group_verdicts.items():
        group = group_map.get(group_id)
        if group is None:
            raise ReviewProtocolError("review contains an extra group")
        for key in ("member_candidate_ids", "member_fingerprints", "member_coverage_sha256"):
            if record.get(key) != group.get(key):
                raise ReviewProtocolError("group verdict member coverage hash drifted")
        verdict = record.get("verdict")
        if verdict == "delete" and not group.get("delete_group_allowed"):
            raise ReviewProtocolError("non-delete_exact group cannot be batch deleted")
        if verdict == "delete" and (
            record.get("action") != "delete"
            or record.get("splice_strategy")
            != group.get("delete_shape", {}).get("splice_strategy")
        ):
            raise ReviewProtocolError("delete_exact group verdict shape drifted")
        member_edit_plan_ids = group.get("member_edit_plan_ids", {})
        if (
            verdict == "delete"
            and member_edit_plan_ids
            and record.get("member_edit_plan_ids") != member_edit_plan_ids
        ):
            raise ReviewProtocolError("group verdict edit plan coverage drifted")
        member_keep_bases = record.get("member_keep_bases", {})
        if verdict == "keep" and not isinstance(member_keep_bases, dict):
            raise ReviewProtocolError("group keep bases must be an object")
        for candidate_id in group["member_candidate_ids"]:
            if candidate_id in expanded:
                raise ReviewProtocolError("candidate has both individual and group verdicts")
            expanded_record = dict(record)
            if verdict == "keep" and candidate_id in member_keep_bases:
                expanded_record["keep_basis"] = member_keep_bases[candidate_id]
            if verdict == "delete" and candidate_id in member_edit_plan_ids:
                expanded_record["edit_plan_id"] = member_edit_plan_ids[candidate_id]
            expanded[candidate_id] = expanded_record

    missing = [item["candidate_id"] for item in candidates if item["candidate_id"] not in expanded]
    if missing:
        raise ReviewProtocolError(f"review verdict coverage is incomplete: {missing[:8]}")
    return [_compiler_review(expanded[item["candidate_id"]], item, manifest) for item in candidates]
