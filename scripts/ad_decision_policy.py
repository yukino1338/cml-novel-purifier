from __future__ import annotations

import hashlib
import json
import re
import copy
from typing import Any

from ad_rules import (
    EMAIL_RE,
    SOURCE_MARKER_RE,
    domain_tokens,
    is_narrative_external_reference,
    site_entities,
)


KEEP_BASIS_SCHEMA = "cml.keep-basis.v1"
KEEP_BASIS_TYPES = frozenset(
    {"narrative_context", "plot_dependency", "rule_false_positive"}
)
KEEP_BASIS_KEYS = frozenset(
    {
        "schema",
        "type",
        "reviewed_occurrences",
        "occurrence_coverage_sha256",
        "note",
    }
)
REVIEWED_OCCURRENCE_KEYS = frozenset({"anchor_id", "text_sha256"})

EDIT_PLAN_SCHEMA = "cml.ad-edit-plan.v1"
EDIT_PLAN_KEYS = frozenset(
    {
        "schema",
        "edit_plan_id",
        "candidate_id",
        "candidate_fingerprint",
        "occurrence_plans",
        "occurrence_coverage_sha256",
    }
)
OCCURRENCE_PLAN_KEYS = frozenset(
    {
        "anchor_id",
        "parent",
        "boundary_kind",
        "segments",
        "delete_segment_ids",
        "joiner",
        "expected_after_sha256",
    }
)
PARENT_BINDING_KEYS = frozenset(
    {"offset", "end", "text_sha256", "prefix_sha256", "suffix_sha256"}
)
SEGMENT_KEYS = frozenset(
    {"segment_id", "kind", "relative_start", "relative_end", "text_sha256"}
)
EDIT_BOUNDARY_KINDS = frozenset(
    {"external_prefix", "external_suffix", "standalone_clause"}
)
SEGMENT_KINDS = frozenset({"narrative", "external_ad"})
SEGMENT_REVIEW_GUARDS = frozenset(
    {"segment_review_required", "long_line_mixed_content"}
)

# These cues are deliberately narrower than general ad detection.  They are
# mutation authorization evidence only when the same proposed segment also
# contains an independently recognized locator.
_SEGMENT_PROMOTION_RE = re.compile(
    r"(?:防止失联|避免失联|备用(?:域名|网址|地址|站点)|请记住|"
    r"永久(?:域名|网址|地址)|最新(?:域名|网址|地址)|"
    r"请(?:访问|收藏|关注|加入|下载|联系)|"
    r"下载地址|获取(?:后续|更新|全文)|更新最快|无弹窗|"
    r"求(?:收藏|推荐|月票|打赏))",
    re.I,
)
_CONTACT_LOCATOR_RE = re.compile(
    r"(?:QQ|QQ群|微信|VX|vx|wx|公众号|书友群)\s*[:：]?\s*[A-Za-z0-9_\-]{4,}",
    re.I,
)
_AUTHOR_LABEL_RE = re.compile(
    r"(?:作者(?:有话(?:要)?说|的话)|PS|P\.S\.|提示)\s*[:：]?",
    re.I,
)
_GENERIC_HINT_LABEL_RE = re.compile(r"提示\s*[:：]?", re.I)
_CLAUSE_SEPARATOR_RE = re.compile(r"[。！？；]")
_NARRATIVE_ENDINGS = frozenset("。！？；")
_SUBSTANTIVE_RE = re.compile(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff]")


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _exact_keys(value: dict[str, Any], expected: frozenset[str], label: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} contains a non-string field name")
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown or missing:
        raise ValueError(f"{label} fields are invalid; missing={missing}, unknown={unknown}")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _has_external_locator(value: str) -> bool:
    return bool(
        domain_tokens(value)
        or site_entities(value)
        or EMAIL_RE.search(value)
        or _CONTACT_LOCATOR_RE.search(value)
    )


def _has_promotion_intent(value: str) -> bool:
    return bool(_SEGMENT_PROMOTION_RE.search(value))


def safe_external_segment(value: str) -> bool:
    """Return whether one exact segment has both mutation authorization gates."""
    return bool(
        value.strip()
        and _has_external_locator(value)
        and _has_promotion_intent(value)
        and not is_narrative_external_reference(value)
    )


def _substantive(value: str) -> bool:
    return len(_SUBSTANTIVE_RE.findall(value)) >= 2


def _move_left_over_space(value: str, index: int) -> int:
    while index > 0 and value[index - 1].isspace():
        index -= 1
    return index


def _move_right_over_space(value: str, index: int) -> int:
    while index < len(value) and value[index].isspace():
        index += 1
    return index


def _external_suffix_has_no_trailing_narrative(value: str) -> bool:
    for separator in _CLAUSE_SEPARATOR_RE.finditer(value):
        remainder = value[separator.end() :]
        if _substantive(remainder) and not safe_external_segment(remainder):
            return False
    return True


def _starts_with_external_frame(value: str) -> bool:
    intent = _SEGMENT_PROMOTION_RE.search(value)
    if intent is None:
        return False
    leading = value[: intent.start()]
    label = _AUTHOR_LABEL_RE.fullmatch(leading.strip(" \t,，:："))
    return not _substantive(leading) or label is not None


def propose_edit_segments(value: str) -> tuple[str, list[tuple[str, int, int]]] | None:
    """Conservatively propose one of the three supported mixed-line shapes.

    The returned ranges cover ``value`` exactly.  No range returned here is an
    execution authority until it is bound into a validated scanner ledger plan.
    """
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        return None
    if not safe_external_segment(value):
        return None

    # A complete source attribution plus an external locator is itself the
    # promotion.  Its punctuation must not turn a source-watermark line into a
    # fictional prefix with only the closing visit cue removed.  Narrative or
    # quoted uses still flow to the existing context guard before any mutation.
    if SOURCE_MARKER_RE.search(value):
        return None

    intent = _SEGMENT_PROMOTION_RE.search(value)
    if intent is None:
        return None

    # External suffix.  A recognized author label is included only when the
    # bytes between it and the first promotion cue contain no narrative text.
    boundary = intent.start()
    explicit_suffix_boundary = False
    labels = list(_AUTHOR_LABEL_RE.finditer(value, 0, intent.start()))
    if labels:
        label = labels[-1]
        bridge = value[label.end() : intent.start()]
        before_label = value[: label.start()].rstrip()
        generic_hint_has_narrative_boundary = bool(
            before_label and before_label[-1] in _NARRATIVE_ENDINGS
        )
        if (
            not bridge.strip(" \t,，:：")
            and (
                _GENERIC_HINT_LABEL_RE.fullmatch(label.group()) is None
                or generic_hint_has_narrative_boundary
            )
        ):
            boundary = label.start()
            explicit_suffix_boundary = True
    if not explicit_suffix_boundary:
        preceding = value[: intent.start()].rstrip()
        explicit_suffix_boundary = bool(
            preceding and preceding[-1] in _NARRATIVE_ENDINGS
        )
    boundary = _move_left_over_space(value, boundary)
    if (
        explicit_suffix_boundary
        and boundary > 0
        and _substantive(value[:boundary])
        and safe_external_segment(value[boundary:])
        and _external_suffix_has_no_trailing_narrative(value[boundary:])
    ):
        return (
            "external_suffix",
            [
                ("narrative", 0, boundary),
                ("external_ad", boundary, len(value)),
            ],
        )

    # External prefix.  Only a complete punctuation-delimited first clause may
    # be removed; ASCII dots are intentionally not treated as delimiters.
    for separator in _CLAUSE_SEPARATOR_RE.finditer(value):
        boundary = _move_right_over_space(value, separator.end())
        prefix = value[:boundary]
        suffix = value[boundary:]
        if (
            safe_external_segment(prefix)
            and _starts_with_external_frame(prefix)
            and _substantive(suffix)
            and not safe_external_segment(suffix)
        ):
            return (
                "external_prefix",
                [
                    ("external_ad", 0, boundary),
                    ("narrative", boundary, len(value)),
                ],
            )

    # Standalone middle clause.  Retain the right delimiter and include the
    # left delimiter in the external segment, so no replacement punctuation is
    # invented by the scanner.
    separators = list(_CLAUSE_SEPARATOR_RE.finditer(value))
    for left, right in zip(separators, separators[1:]):
        delete_start = left.start()
        delete_end = right.start()
        if (
            delete_start > 0
            and delete_end > delete_start
            and _substantive(value[:delete_start])
            and _substantive(value[delete_end:])
            and safe_external_segment(value[delete_start:delete_end])
        ):
            return (
                "standalone_clause",
                [
                    ("narrative", 0, delete_start),
                    ("external_ad", delete_start, delete_end),
                    ("narrative", delete_end, len(value)),
                ],
            )
    return None


def _segment_records(
    original: str,
    proposed: list[tuple[str, int, int]],
) -> list[dict[str, Any]]:
    return [
        {
            "segment_id": f"S{index}",
            "kind": kind,
            "relative_start": start,
            "relative_end": end,
            "text_sha256": _text_sha256(original[start:end]),
        }
        for index, (kind, start, end) in enumerate(proposed, 1)
    ]


def _coverage_payload(occurrence_plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return stable occurrence coverage before candidate/anchor IDs are bound."""
    return [
        {
            "parent": occurrence["parent"],
            "boundary_kind": occurrence["boundary_kind"],
            "segments": occurrence["segments"],
            "delete_segment_ids": occurrence["delete_segment_ids"],
            "joiner": occurrence["joiner"],
            "expected_after_sha256": occurrence["expected_after_sha256"],
        }
        for occurrence in occurrence_plans
    ]


def edit_plan_occurrence_coverage_sha256(
    occurrence_plans: list[dict[str, Any]],
) -> str:
    return _canonical_json_sha256(_coverage_payload(occurrence_plans))


def _edit_plan_id_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": plan["schema"],
        "candidate_id": plan["candidate_id"],
        "candidate_fingerprint": plan["candidate_fingerprint"],
        "occurrence_plans": _coverage_payload(plan["occurrence_plans"]),
        "occurrence_coverage_sha256": plan["occurrence_coverage_sha256"],
    }


def build_edit_plan(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Build an unforgeable-by-review mixed edit plan for every saved anchor."""
    if candidate.get("anchors_truncated") is not False:
        return None
    candidate_id = candidate.get("candidate_id")
    anchors = candidate.get("anchors")
    if not isinstance(candidate_id, str) or not candidate_id:
        return None
    if not isinstance(anchors, list) or not anchors:
        return None
    occurrence_plans: list[dict[str, Any]] = []
    for anchor in anchors:
        if not isinstance(anchor, dict):
            return None
        original = anchor.get("original")
        offset = anchor.get("offset")
        end = anchor.get("end")
        prefix = anchor.get("prefix")
        suffix = anchor.get("suffix")
        if (
            not isinstance(original, str)
            or not original
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or end != offset + len(original)
            or not isinstance(prefix, str)
            or not isinstance(suffix, str)
        ):
            return None
        proposal = propose_edit_segments(original)
        if proposal is None:
            return None
        boundary_kind, proposed_segments = proposal
        segments = _segment_records(original, proposed_segments)
        after = "".join(
            original[item["relative_start"] : item["relative_end"]]
            for item in segments
            if item["kind"] == "narrative"
        )
        occurrence_plans.append(
            {
                "anchor_id": str(anchor.get("anchor_id") or ""),
                "parent": {
                    "offset": offset,
                    "end": end,
                    "text_sha256": _text_sha256(original),
                    "prefix_sha256": _text_sha256(prefix),
                    "suffix_sha256": _text_sha256(suffix),
                },
                "boundary_kind": boundary_kind,
                "segments": segments,
                "delete_segment_ids": [
                    item["segment_id"]
                    for item in segments
                    if item["kind"] == "external_ad"
                ],
                "joiner": "",
                "expected_after_sha256": _text_sha256(after),
            }
        )
    coverage = edit_plan_occurrence_coverage_sha256(occurrence_plans)
    plan: dict[str, Any] = {
        "schema": EDIT_PLAN_SCHEMA,
        "edit_plan_id": "",
        "candidate_id": candidate_id,
        "candidate_fingerprint": str(candidate.get("candidate_fingerprint") or ""),
        "occurrence_plans": occurrence_plans,
        "occurrence_coverage_sha256": coverage,
    }
    plan["edit_plan_id"] = "EP-" + _canonical_json_sha256(_edit_plan_id_payload(plan))
    return plan


def bind_edit_plan(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Bind a scanner-created plan to the attached candidate and anchor identities."""
    plan = candidate.get("edit_plan")
    if plan is None:
        return None
    if not isinstance(plan, dict):
        raise ValueError("candidate edit_plan must be an object")
    fingerprint = candidate.get("candidate_fingerprint")
    anchors = candidate.get("anchors")
    occurrences = plan.get("occurrence_plans")
    if not _is_lower_sha256(fingerprint):
        raise ValueError("candidate fingerprint must be bound before edit_plan")
    if not isinstance(anchors, list) or not isinstance(occurrences, list) or len(anchors) != len(occurrences):
        raise ValueError("edit_plan occurrence count does not match candidate anchors")
    plan["candidate_fingerprint"] = fingerprint
    for anchor, occurrence in zip(anchors, occurrences):
        if not isinstance(anchor, dict) or not isinstance(occurrence, dict):
            raise ValueError("edit_plan occurrence binding is invalid")
        occurrence["anchor_id"] = anchor.get("anchor_id")
    plan["edit_plan_id"] = "EP-" + _canonical_json_sha256(_edit_plan_id_payload(plan))
    normalized = normalize_edit_plan(plan, candidate)
    candidate["edit_plan"] = normalized
    return normalized


def _validate_occurrence_plan(
    value: object,
    anchor: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    _exact_keys(value, OCCURRENCE_PLAN_KEYS, label)
    if value.get("anchor_id") != anchor.get("anchor_id"):
        raise ValueError(f"{label} anchor_id is stale")
    original = anchor.get("original")
    prefix = anchor.get("prefix")
    suffix = anchor.get("suffix")
    offset = anchor.get("offset")
    end = anchor.get("end")
    if (
        not isinstance(original, str)
        or not original
        or not isinstance(prefix, str)
        or not isinstance(suffix, str)
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or end != offset + len(original)
    ):
        raise ValueError(f"{label} parent anchor is invalid")
    parent = value.get("parent")
    if not isinstance(parent, dict):
        raise ValueError(f"{label}.parent must be an object")
    _exact_keys(parent, PARENT_BINDING_KEYS, f"{label}.parent")
    expected_parent = {
        "offset": offset,
        "end": end,
        "text_sha256": _text_sha256(original),
        "prefix_sha256": _text_sha256(prefix),
        "suffix_sha256": _text_sha256(suffix),
    }
    if parent != expected_parent:
        raise ValueError(f"{label}.parent binding is stale")
    boundary_kind = value.get("boundary_kind")
    if boundary_kind not in EDIT_BOUNDARY_KINDS:
        raise ValueError(f"{label}.boundary_kind is invalid")
    segments = value.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError(f"{label}.segments must be a non-empty array")
    normalized_segments: list[dict[str, Any]] = []
    cursor = 0
    seen_ids: set[str] = set()
    for index, segment in enumerate(segments, 1):
        segment_label = f"{label}.segments[{index - 1}]"
        if not isinstance(segment, dict):
            raise ValueError(f"{segment_label} must be an object")
        _exact_keys(segment, SEGMENT_KEYS, segment_label)
        segment_id = segment.get("segment_id")
        kind = segment.get("kind")
        start = segment.get("relative_start")
        segment_end = segment.get("relative_end")
        if segment_id != f"S{index}" or segment_id in seen_ids:
            raise ValueError(f"{segment_label}.segment_id is invalid")
        if kind not in SEGMENT_KINDS:
            raise ValueError(f"{segment_label}.kind is invalid")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(segment_end, int)
            or isinstance(segment_end, bool)
            or start != cursor
            or not start < segment_end <= len(original)
        ):
            raise ValueError(f"{segment_label} range must continuously cover the parent")
        fragment = original[start:segment_end]
        if segment.get("text_sha256") != _text_sha256(fragment):
            raise ValueError(f"{segment_label}.text_sha256 is stale")
        seen_ids.add(segment_id)
        cursor = segment_end
        normalized_segments.append(copy.deepcopy(segment))
    if cursor != len(original):
        raise ValueError(f"{label}.segments do not cover the parent")
    kinds = [segment["kind"] for segment in normalized_segments]
    expected_shapes = {
        "external_prefix": ["external_ad", "narrative"],
        "external_suffix": ["narrative", "external_ad"],
        "standalone_clause": ["narrative", "external_ad", "narrative"],
    }
    if kinds != expected_shapes[boundary_kind]:
        raise ValueError(f"{label}.segments do not match boundary_kind")
    delete_ids = value.get("delete_segment_ids")
    expected_delete_ids = [
        segment["segment_id"]
        for segment in normalized_segments
        if segment["kind"] == "external_ad"
    ]
    if delete_ids != expected_delete_ids:
        raise ValueError(f"{label}.delete_segment_ids are invalid")
    deleted = "".join(
        original[segment["relative_start"] : segment["relative_end"]]
        for segment in normalized_segments
        if segment["kind"] == "external_ad"
    )
    if not safe_external_segment(deleted):
        raise ValueError(f"{label} external segment lacks locator plus promotion intent")
    joiner = value.get("joiner")
    if joiner != "":
        raise ValueError(f"{label}.joiner is unsupported")
    after = joiner.join(
        original[segment["relative_start"] : segment["relative_end"]]
        for segment in normalized_segments
        if segment["kind"] == "narrative"
    )
    if not _substantive(after):
        raise ValueError(f"{label} must preserve substantive narrative")
    if value.get("expected_after_sha256") != _text_sha256(after):
        raise ValueError(f"{label}.expected_after_sha256 is stale")
    return {
        "anchor_id": value["anchor_id"],
        "parent": copy.deepcopy(parent),
        "boundary_kind": boundary_kind,
        "segments": normalized_segments,
        "delete_segment_ids": list(expected_delete_ids),
        "joiner": joiner,
        "expected_after_sha256": value["expected_after_sha256"],
    }


def normalize_edit_plan(value: object, candidate: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a scanner ledger edit plan against one candidate."""
    candidate_id = candidate.get("candidate_id") or "<unknown>"
    if not isinstance(value, dict):
        raise ValueError(f"edit_plan must be an object for {candidate_id}")
    _exact_keys(value, EDIT_PLAN_KEYS, f"edit_plan for {candidate_id}")
    if value.get("schema") != EDIT_PLAN_SCHEMA:
        raise ValueError(f"edit_plan schema is invalid for {candidate_id}")
    if value.get("candidate_id") != candidate.get("candidate_id"):
        raise ValueError(f"edit_plan candidate_id is stale for {candidate_id}")
    fingerprint = candidate.get("candidate_fingerprint")
    if not _is_lower_sha256(fingerprint) or value.get("candidate_fingerprint") != fingerprint:
        raise ValueError(f"edit_plan candidate fingerprint is stale for {candidate_id}")
    if candidate.get("anchors_truncated") is not False:
        raise ValueError(f"truncated candidate cannot carry edit_plan: {candidate_id}")
    anchors = candidate.get("anchors")
    occurrence_count = candidate.get("occurrence_count")
    occurrences = value.get("occurrence_plans")
    if (
        not isinstance(anchors, list)
        or not anchors
        or not isinstance(occurrence_count, int)
        or isinstance(occurrence_count, bool)
        or occurrence_count != len(anchors)
        or not isinstance(occurrences, list)
        or len(occurrences) != len(anchors)
    ):
        raise ValueError(f"edit_plan occurrence coverage is incomplete for {candidate_id}")
    normalized_occurrences = [
        _validate_occurrence_plan(
            occurrence,
            anchor,
            label=f"edit_plan occurrence {index} for {candidate_id}",
        )
        for index, (occurrence, anchor) in enumerate(zip(occurrences, anchors), 1)
        if isinstance(anchor, dict)
    ]
    if len(normalized_occurrences) != len(anchors):
        raise ValueError(f"edit_plan candidate anchors are invalid for {candidate_id}")
    coverage = edit_plan_occurrence_coverage_sha256(normalized_occurrences)
    if value.get("occurrence_coverage_sha256") != coverage:
        raise ValueError(f"edit_plan occurrence coverage hash is stale for {candidate_id}")
    normalized = {
        "schema": EDIT_PLAN_SCHEMA,
        "edit_plan_id": value.get("edit_plan_id"),
        "candidate_id": candidate["candidate_id"],
        "candidate_fingerprint": fingerprint,
        "occurrence_plans": normalized_occurrences,
        "occurrence_coverage_sha256": coverage,
    }
    expected_id = "EP-" + _canonical_json_sha256(_edit_plan_id_payload(normalized))
    if normalized["edit_plan_id"] != expected_id:
        raise ValueError(f"edit_plan_id is stale for {candidate_id}")
    return normalized


def edit_plan_preview(
    candidate: dict[str, Any],
    value: object | None = None,
) -> list[dict[str, str]]:
    plan = normalize_edit_plan(candidate.get("edit_plan") if value is None else value, candidate)
    anchors = candidate["anchors"]
    previews: list[dict[str, str]] = []
    for anchor, occurrence in zip(anchors, plan["occurrence_plans"]):
        original = anchor["original"]
        deleted_ids = set(occurrence["delete_segment_ids"])
        kept: list[str] = []
        deleted: list[str] = []
        for segment in occurrence["segments"]:
            fragment = original[segment["relative_start"] : segment["relative_end"]]
            (deleted if segment["segment_id"] in deleted_ids else kept).append(fragment)
        after = occurrence["joiner"].join(kept)
        if _text_sha256(after) != occurrence["expected_after_sha256"]:
            raise ValueError("edit_plan preview does not match expected_after_sha256")
        previews.append(
            {
                "anchor_id": occurrence["anchor_id"],
                "boundary_kind": occurrence["boundary_kind"],
                "keep_text": "".join(kept),
                "delete_text": "".join(deleted),
                "after_text": after,
            }
        )
    return previews


def delete_eligibility(
    candidate: dict[str, Any],
    *,
    module: str = "ads",
    protection_conflict: bool = False,
    formal_blockers: list[str] | tuple[str, ...] = (),
    projection_truncated: bool = False,
) -> dict[str, Any]:
    """Return the sole Python authority projected into review UIs."""
    blockers: list[str] = []
    if module != "ads":
        blockers.append("report_only_module")
    anchors = candidate.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        blockers.append("zero_anchor")
    truncated = candidate.get("anchors_truncated")
    occurrence_count = candidate.get("occurrence_count")
    if truncated is not False:
        blockers.append("anchors_truncated")
    elif (
        not isinstance(occurrence_count, int)
        or isinstance(occurrence_count, bool)
        or not isinstance(anchors, list)
        or occurrence_count != len(anchors)
    ):
        blockers.append("occurrence_coverage_incomplete")
    if protection_conflict:
        blockers.append("protection_conflict")
    if formal_blockers:
        blockers.append("formal_blocker")
    if projection_truncated:
        blockers.append("review_projection_truncated")

    guard = candidate.get("mutation_guard")
    plan = candidate.get("edit_plan")
    plan_valid = False
    if plan is not None:
        try:
            normalize_edit_plan(plan, candidate)
            plan_valid = True
        except ValueError:
            blockers.append("edit_plan_invalid")
    mixed = guard in SEGMENT_REVIEW_GUARDS or plan is not None
    whole_blockers = list(blockers)
    if mixed:
        whole_blockers.append("mixed_whole_block")
    elif guard:
        whole_blockers.append("mutation_guard")
    delete_allowed = module == "ads" and not whole_blockers
    segment_blockers = [
        blocker
        for blocker in blockers
        if blocker not in {"mixed_whole_block", "mutation_guard"}
    ]
    if not plan_valid:
        segment_blockers.append("edit_plan_missing_or_invalid")
    segment_delete_allowed = bool(
        module == "ads"
        and mixed
        and plan_valid
        and not segment_blockers
    )
    return {
        "delete_allowed": delete_allowed,
        "delete_blockers": list(dict.fromkeys(whole_blockers)),
        "batch_delete_allowed": delete_allowed,
        "batch_delete_blockers": list(dict.fromkeys(whole_blockers)),
        "segment_delete_allowed": segment_delete_allowed,
        "segment_delete_blockers": list(dict.fromkeys(segment_blockers)),
        "segment_support_message": (
            "可请求只删除 Python 已验证的标出片段。"
            if segment_delete_allowed
            else "当前尚未支持安全的子段删除；整块删除已禁用。"
            if mixed
            else ""
        ),
    }


def subset_edit_plan_for_rollback(
    value: object,
    candidate: dict[str, Any],
    kept_anchor_ids: list[str],
) -> dict[str, Any]:
    """Create the exact plan subset used by deterministic targeted rollback."""
    original = normalize_edit_plan(value, candidate)
    keep = set(kept_anchor_ids)
    if len(keep) != len(kept_anchor_ids):
        raise ValueError("rollback edit_plan anchor IDs must be unique")
    occurrences = [
        copy.deepcopy(item)
        for item in original["occurrence_plans"]
        if item["anchor_id"] in keep
    ]
    if {item["anchor_id"] for item in occurrences} != keep or not occurrences:
        raise ValueError("rollback edit_plan subset does not match kept anchors")
    subset = {
        **original,
        "occurrence_plans": occurrences,
        "occurrence_coverage_sha256": edit_plan_occurrence_coverage_sha256(occurrences),
    }
    subset["edit_plan_id"] = "EP-" + _canonical_json_sha256(_edit_plan_id_payload(subset))
    subset_candidate = {
        **candidate,
        "anchors": [
            anchor
            for anchor in candidate["anchors"]
            if anchor.get("anchor_id") in keep
        ],
        "occurrence_count": len(keep),
        "anchors_truncated": False,
    }
    return normalize_edit_plan(subset, subset_candidate)


def _validate_reviewed_occurrences(
    value: object,
    *,
    label: str,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")

    normalized: list[dict[str, str]] = []
    seen_anchor_ids: set[str] = set()
    for index, record in enumerate(value):
        if not isinstance(record, dict):
            raise ValueError(f"{label}[{index}] must be an object")
        if any(not isinstance(key, str) for key in record):
            raise ValueError(f"{label}[{index}] contains a non-string field name")
        unknown = sorted(set(record) - REVIEWED_OCCURRENCE_KEYS)
        missing = sorted(REVIEWED_OCCURRENCE_KEYS - set(record))
        if unknown or missing:
            raise ValueError(
                f"{label}[{index}] fields are invalid; "
                f"missing={missing}, unknown={unknown}"
            )
        anchor_id = record.get("anchor_id")
        text_sha256 = record.get("text_sha256")
        if not isinstance(anchor_id, str) or not anchor_id:
            raise ValueError(f"{label}[{index}].anchor_id must be a non-empty string")
        if anchor_id in seen_anchor_ids:
            raise ValueError(f"{label} contains duplicate anchor_id: {anchor_id}")
        if not _is_lower_sha256(text_sha256):
            raise ValueError(
                f"{label}[{index}].text_sha256 must be a lowercase SHA-256 value"
            )
        seen_anchor_ids.add(anchor_id)
        normalized.append(
            {"anchor_id": anchor_id, "text_sha256": str(text_sha256)}
        )
    return normalized


def occurrence_coverage_sha256(
    reviewed_occurrences: list[dict[str, str]],
) -> str:
    """Hash a validated occurrence array in its current ledger order."""
    normalized = _validate_reviewed_occurrences(
        reviewed_occurrences,
        label="reviewed_occurrences",
    )
    return _canonical_json_sha256(normalized)


def validate_candidate_occurrences(
    candidate: dict[str, Any],
) -> list[dict[str, str]]:
    """Validate occurrence closure and return anchor/hash pairs in ledger order."""
    candidate_id = candidate.get("candidate_id") or "<unknown>"
    anchors = candidate.get("anchors")
    if not isinstance(anchors, list):
        raise ValueError(f"anchors must be a list for {candidate_id}")
    truncated = candidate.get("anchors_truncated")
    if not isinstance(truncated, bool):
        raise ValueError(f"anchors_truncated must be boolean for {candidate_id}")
    occurrence_count = candidate.get("occurrence_count")
    if (
        not isinstance(occurrence_count, int)
        or isinstance(occurrence_count, bool)
        or occurrence_count < 0
    ):
        raise ValueError(f"occurrence_count must be a non-negative integer for {candidate_id}")
    if truncated:
        if occurrence_count <= len(anchors):
            raise ValueError(
                "truncated occurrence_count must exceed saved anchors for "
                f"{candidate_id}"
            )
    elif occurrence_count != len(anchors):
        raise ValueError(
            "non-truncated occurrence_count must equal saved anchors for "
            f"{candidate_id}"
        )

    occurrences: list[dict[str, str]] = []
    for index, anchor in enumerate(anchors):
        if not isinstance(anchor, dict):
            raise ValueError(f"anchor {index} must be an object for {candidate_id}")
        anchor_id = anchor.get("anchor_id")
        original = anchor.get("original")
        if not isinstance(anchor_id, str) or not anchor_id:
            raise ValueError(f"anchor_id is invalid for {candidate_id}")
        if not isinstance(original, str) or not original:
            raise ValueError(f"anchor text is invalid for {candidate_id}")
        occurrences.append(
            {
                "anchor_id": anchor_id,
                "text_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            }
        )
    return _validate_reviewed_occurrences(
        occurrences,
        label=f"candidate occurrences for {candidate_id}",
    )


def normalize_keep_basis(
    value: object,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Validate a keep authorization and bind it to every current occurrence."""
    candidate_id = candidate.get("candidate_id") or "<unknown>"
    if isinstance(value, str):
        raise ValueError(
            "legacy scalar keep_basis is unsupported; re-review candidate "
            f"{candidate_id} with {KEEP_BASIS_SCHEMA}"
        )
    if not isinstance(value, dict):
        raise ValueError(f"keep_basis must be an object for {candidate_id}")

    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"keep_basis contains a non-string field name for {candidate_id}")
    unknown = sorted(set(value) - KEEP_BASIS_KEYS)
    missing = sorted(KEEP_BASIS_KEYS - set(value))
    if unknown or missing:
        raise ValueError(
            f"keep_basis fields are invalid for {candidate_id}; "
            f"missing={missing}, unknown={unknown}"
        )
    if value.get("schema") != KEEP_BASIS_SCHEMA:
        raise ValueError(f"keep_basis schema is invalid for {candidate_id}")
    basis_type = value.get("type")
    if not isinstance(basis_type, str) or basis_type not in KEEP_BASIS_TYPES:
        raise ValueError(f"keep_basis type is invalid for {candidate_id}")
    note = value.get("note")
    if (
        not isinstance(note, str)
        or not 1 <= len(note) <= 500
        or not note.strip()
    ):
        raise ValueError(
            f"keep_basis note must contain 1..500 code points for {candidate_id}"
        )

    expected = validate_candidate_occurrences(candidate)
    if candidate.get("anchors_truncated"):
        raise ValueError(f"truncated candidate cannot use keep_basis: {candidate_id}")
    if not expected:
        raise ValueError(f"keep_basis requires at least one occurrence: {candidate_id}")
    reviewed = _validate_reviewed_occurrences(
        value.get("reviewed_occurrences"),
        label=f"keep_basis.reviewed_occurrences for {candidate_id}",
    )
    reviewed_by_id = {record["anchor_id"]: record for record in reviewed}
    expected_by_id = {record["anchor_id"]: record for record in expected}
    missing_ids = sorted(set(expected_by_id) - set(reviewed_by_id))
    extra_ids = sorted(set(reviewed_by_id) - set(expected_by_id))
    if missing_ids or extra_ids:
        raise ValueError(
            f"keep_basis occurrence coverage is incomplete for {candidate_id}; "
            f"missing={missing_ids}, extra={extra_ids}"
        )
    for anchor_id, occurrence in expected_by_id.items():
        if reviewed_by_id[anchor_id]["text_sha256"] != occurrence["text_sha256"]:
            raise ValueError(
                f"keep_basis occurrence hash mismatch for {candidate_id}: {anchor_id}"
            )

    supplied_coverage = value.get("occurrence_coverage_sha256")
    if not _is_lower_sha256(supplied_coverage):
        raise ValueError(
            "keep_basis occurrence_coverage_sha256 must be a lowercase SHA-256 "
            f"value for {candidate_id}"
        )
    expected_coverage = occurrence_coverage_sha256(expected)
    if supplied_coverage != expected_coverage:
        raise ValueError(f"keep_basis occurrence coverage hash mismatch for {candidate_id}")

    return {
        "schema": KEEP_BASIS_SCHEMA,
        "type": basis_type,
        "reviewed_occurrences": expected,
        "occurrence_coverage_sha256": expected_coverage,
        "note": note,
    }
