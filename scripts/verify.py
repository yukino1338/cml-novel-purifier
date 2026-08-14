from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from pathlib import Path
from typing import Any

from common import (
    WorkspaceTransaction,
    load_jsonl,
    load_jsonl_for_run,
    load_manifest,
    read_utf8,
    resolve_current_head,
    resolve_workspace_paths,
    sha256_file,
    validate_workspace,
    workspace_transaction_lock,
    write_json,
    write_utf8,
)
from apply_decisions import validate_formal_ad_provenance
import ad_decision_policy
from make_ad_decisions import load_current_ad_candidates, read_ads_scan_report
from parse_structure import parse as parse_chapters
from normalize_layout import normalize_text
from scan_ads import scan_candidates


MUTATING_VERDICTS = {"delete"}
MUTATING_ACTIONS = {"delete"}
VERIFY_RULE_VERSION = "9"
PROVENANCE_IDENTITY_FIELDS = (
    "scan_rule_pack_sha256",
    "draft_rule_pack_sha256",
    "profile",
    "profile_present",
    "book_profile_sha256",
    "book_profile_file_sha256",
)
RESIDUAL_KEEP_CONFLICT_EVIDENCE = frozenset(
    {"automatic_delete_gate", "family_similarity"}
)
REQUIRED_VERIFY_CHECKS = frozenset(
    {
        "apply_binding",
        "scan_decision_provenance",
        "operation_binding",
        "anchor_accounting",
        "formal_uncertain",
        "operation_replay",
        "segment_plan_replay",
        "apply_chapter_identity",
        "layout_binding",
        "layout_replay",
        "final_chapter_structure",
        "current_run_anomalies",
        "complete_residual_scan",
        "strong_residuals",
        "risk_warnings",
    }
)


def is_mutating(decision: dict[str, Any]) -> bool:
    verdict = str(decision.get("verdict", "")).strip()
    action = str(decision.get("action", "")).strip()
    return verdict in MUTATING_VERDICTS or action in MUTATING_ACTIONS


def count_chars(text: str) -> int:
    return len(text)


def chapter_summary(text: str) -> dict[str, Any]:
    chapters, report = parse_chapters(text)
    return {
        "chapters": chapters,
        "report": report,
        "count": len(chapters),
    }


def compare_chapters(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if len(before) != len(after):
        return warnings
    for left, right in zip(before, after):
        before_count = int(left.get("word_count", 0))
        after_count = int(right.get("word_count", 0))
        if before_count <= 0:
            continue
        shrink = (before_count - after_count) / before_count
        if shrink > 0.2 and before_count - after_count > 500:
            warnings.append(
                {
                    "index": left.get("index"),
                    "title": left.get("title"),
                    "before": before_count,
                    "after": after_count,
                    "shrink_ratio": round(shrink, 4),
                }
            )
    return warnings


def compare_chapter_identity(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> dict[str, Any]:
    before_titles = [str(chapter.get("title") or "") for chapter in before]
    after_titles = [str(chapter.get("title") or "") for chapter in after]
    before_indexes = [chapter.get("index") for chapter in before]
    after_indexes = [chapter.get("index") for chapter in after]
    ranges_valid = all(
        isinstance(chapter.get("start_offset"), int)
        and isinstance(chapter.get("end_offset"), int)
        and int(chapter["start_offset"]) <= int(chapter["end_offset"])
        for chapter in after
    )
    return {
        "passed": (
            len(before) == len(after)
            and before_titles == after_titles
            and before_indexes == after_indexes
            and ranges_valid
        ),
        "before_titles": before_titles,
        "after_titles": after_titles,
        "before_indexes": before_indexes,
        "after_indexes": after_indexes,
        "ranges_valid": ranges_valid,
    }


def compare_layout_chapters(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> dict[str, Any]:
    before_indexes = [chapter.get("index") for chapter in before]
    after_indexes = [chapter.get("index") for chapter in after]
    ranges_valid = all(
        isinstance(chapter.get("start_offset"), int)
        and not isinstance(chapter.get("start_offset"), bool)
        and isinstance(chapter.get("end_offset"), int)
        and not isinstance(chapter.get("end_offset"), bool)
        and int(chapter["start_offset"]) <= int(chapter["end_offset"])
        for chapter in after
    )
    return {
        "passed": (
            len(before) == len(after)
            and before_indexes == after_indexes
            and ranges_valid
        ),
        "before_titles": [str(chapter.get("title") or "") for chapter in before],
        "after_titles": [str(chapter.get("title") or "") for chapter in after],
        "before_indexes": before_indexes,
        "after_indexes": after_indexes,
        "ranges_valid": ranges_valid,
    }


def load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def verify_decision_accounting(
    decisions: list[dict[str, Any]],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    mutating_ids = [str(item.get("candidate_id", "")) for item in decisions if is_mutating(item)]
    non_mutating_ids = [str(item.get("candidate_id", "")) for item in decisions if not is_mutating(item)]
    expected_anchor_ids = [
        str(anchor.get("anchor_id") or "")
        for decision in decisions
        if is_mutating(decision)
        for anchor in decision.get("anchors", [])
        if isinstance(anchor, dict)
    ]
    operated_ids = [str(item.get("candidate_id", "")) for item in operations]
    operation_anchor_ids = [str(item.get("anchor_id") or "") for item in operations]
    operated_set = set(operated_ids)
    operation_anchor_set = {value for value in operation_anchor_ids if value}
    expected_anchor_set = {value for value in expected_anchor_ids if value}
    missing_candidates = sorted({item for item in mutating_ids if item and item not in operated_set})
    unexpected_candidates = sorted({item for item in non_mutating_ids if item and item in operated_set})
    return {
        "decision_count": len(decisions),
        "mutating_decision_count": len(mutating_ids),
        "operation_count": len(operations),
        "expected_anchor_ids": sorted(expected_anchor_set),
        "operation_anchor_ids": sorted(operation_anchor_set),
        "missing_operation_anchor_ids": sorted(expected_anchor_set - operation_anchor_set),
        "unexpected_operation_anchor_ids": sorted(operation_anchor_set - expected_anchor_set),
        "duplicate_operation_anchor_ids": sorted(
            {value for value in operation_anchor_ids if value and operation_anchor_ids.count(value) > 1}
        ),
        "missing_operation_candidate_ids": missing_candidates,
        "unexpected_operation_candidate_ids": unexpected_candidates,
    }


def replay_operations(text: str, operations: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    issues: list[str] = []
    spans: list[tuple[int, int, dict[str, Any]]] = []
    for operation in operations:
        start = operation.get("start")
        end = operation.get("end")
        original = operation.get("original")
        replacement = operation.get("replacement")
        anchor_id = str(operation.get("anchor_id") or "<missing>")
        if operation.get("action") != "delete":
            issues.append(f"operation {anchor_id} has unsupported action")
            continue
        if not isinstance(replacement, str) or replacement not in {"", "\n"}:
            issues.append(f"operation {anchor_id} has unsupported delete replacement")
            continue
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end < start
            or end > len(text)
            or not isinstance(original, str)
        ):
            issues.append(f"operation {anchor_id} has invalid replay fields")
            continue
        if text[start:end] != original:
            issues.append(f"operation {anchor_id} original does not match input")
            continue
        spans.append((start, end, operation))
    spans.sort(key=lambda item: item[0])
    for left, right in zip(spans, spans[1:]):
        if right[0] < left[1]:
            issues.append("operation spans overlap")
            break
    if issues:
        return None, issues
    replayed = text
    for start, end, operation in reversed(spans):
        replayed = replayed[:start] + str(operation["replacement"]) + replayed[end:]
    return replayed, []


def verify_segment_plan_replay(
    before_text: str,
    apply_output_text: str,
    decisions: list[dict[str, Any]],
    operations: list[dict[str, Any]],
) -> list[str]:
    """Independently prove exact segment removal and byte-exact narrative retention."""
    issues: list[str] = []
    operation_by_anchor: dict[str, list[dict[str, Any]]] = {}
    normalized_operations: list[tuple[int, int, int]] = []
    for operation in operations:
        anchor_id = str(operation.get("anchor_id") or "")
        operation_by_anchor.setdefault(anchor_id, []).append(operation)
        start = operation.get("start")
        end = operation.get("end")
        replacement = operation.get("replacement")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and isinstance(replacement, str)
        ):
            normalized_operations.append((start, end, len(replacement) - (end - start)))

    for decision in decisions:
        if decision.get("splice_strategy") != "exact_segment":
            continue
        candidate_id = str(decision.get("candidate_id") or "<missing>")
        try:
            plan = ad_decision_policy.normalize_edit_plan(
                decision.get("edit_plan"),
                decision,
            )
            previews = ad_decision_policy.edit_plan_preview(decision, plan)
        except ValueError as error:
            issues.append(f"segment plan {candidate_id} is invalid: {error}")
            continue
        if decision.get("edit_plan_id") != plan["edit_plan_id"]:
            issues.append(f"segment plan {candidate_id} has stale edit_plan_id")
            continue
        preview_by_anchor = {item["anchor_id"]: item for item in previews}
        for occurrence in plan["occurrence_plans"]:
            anchor_id = occurrence["anchor_id"]
            bound_operations = operation_by_anchor.get(anchor_id, [])
            if len(bound_operations) != 1:
                issues.append(
                    f"segment plan {candidate_id}/{anchor_id} must bind exactly one operation"
                )
                continue
            operation = bound_operations[0]
            deleted_ids = set(occurrence["delete_segment_ids"])
            deleted = [
                segment
                for segment in occurrence["segments"]
                if segment["segment_id"] in deleted_ids
            ]
            if len(deleted) != 1:
                issues.append(
                    f"segment plan {candidate_id}/{anchor_id} is not one contiguous deletion"
                )
                continue
            segment = deleted[0]
            parent = occurrence["parent"]
            expected_start = parent["offset"] + segment["relative_start"]
            expected_end = parent["offset"] + segment["relative_end"]
            preview = preview_by_anchor[anchor_id]
            expected_original = preview["delete_text"]
            for field, expected in (
                ("edit_plan_id", plan["edit_plan_id"]),
                ("start", expected_start),
                ("end", expected_end),
                ("original", expected_original),
                ("replacement", occurrence["joiner"]),
                ("parent_start", parent["offset"]),
                ("parent_end", parent["end"]),
                ("expected_after_sha256", occurrence["expected_after_sha256"]),
            ):
                if operation.get(field) != expected:
                    issues.append(
                        f"segment operation {candidate_id}/{anchor_id} has stale {field}"
                    )

            shift = sum(
                delta
                for start, end, delta in normalized_operations
                if end <= parent["offset"]
            )
            mapped_start = parent["offset"] + shift
            after_text = preview["after_text"]
            if apply_output_text[mapped_start : mapped_start + len(after_text)] != after_text:
                issues.append(
                    f"segment plan {candidate_id}/{anchor_id} did not preserve narrative bytes"
                )
            if hashlib.sha256(after_text.encode("utf-8")).hexdigest() != occurrence[
                "expected_after_sha256"
            ]:
                issues.append(
                    f"segment plan {candidate_id}/{anchor_id} expected-after hash is stale"
                )
    return issues


def _mapped_occurrence(
    anchor: dict[str, Any],
    operations: list[dict[str, Any]],
) -> tuple[int, int, str] | None:
    start = anchor.get("offset")
    end = anchor.get("end")
    original = anchor.get("original")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or not isinstance(original, str)
        or not original
        or end != start + len(original)
    ):
        return None

    normalized_operations: list[tuple[int, int, str]] = []
    for operation in operations:
        operation_start = operation.get("start")
        operation_end = operation.get("end")
        replacement = operation.get("replacement")
        operation_original = operation.get("original")
        if (
            operation.get("action") != "delete"
            or not isinstance(operation_start, int)
            or isinstance(operation_start, bool)
            or not isinstance(operation_end, int)
            or isinstance(operation_end, bool)
            or operation_start < 0
            or operation_end < operation_start
            or not isinstance(operation_original, str)
            or len(operation_original) != operation_end - operation_start
            or not isinstance(replacement, str)
            or replacement not in {"", "\n"}
        ):
            return None
        normalized_operations.append((operation_start, operation_end, replacement))

    shift = 0
    previous_end = -1
    for operation_start, operation_end, replacement in sorted(normalized_operations):
        if operation_start < previous_end:
            return None
        previous_end = operation_end
        if operation_end <= start:
            shift += len(replacement) - (operation_end - operation_start)
        elif operation_start < end:
            return None

    mapped_start = start + shift
    return (
        mapped_start,
        mapped_start + len(original),
        hashlib.sha256(original.encode("utf-8")).hexdigest(),
    )


def _structured_keep_authorizations(
    decisions: list[dict[str, Any]],
    source_candidates: list[dict[str, Any]],
    operations: list[dict[str, Any]],
) -> tuple[dict[tuple[int, int, str], int], set[str]]:
    source_by_id: dict[str, dict[str, Any]] = {}
    duplicate_source_ids: set[str] = set()
    for candidate in source_candidates:
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        if candidate_id in source_by_id:
            duplicate_source_ids.add(candidate_id)
        else:
            source_by_id[candidate_id] = candidate

    pending: list[tuple[str, list[str], list[tuple[int, int, str]]]] = []
    anchor_id_counts: dict[str, int] = {}
    invalid_basis_ids: set[str] = set()
    for decision in decisions:
        candidate_id = str(decision.get("candidate_id") or "")
        if decision.get("verdict") != "keep" or "keep_basis" not in decision:
            continue
        source = source_by_id.get(candidate_id)
        if source is None or candidate_id in duplicate_source_ids:
            invalid_basis_ids.add(candidate_id)
            continue
        try:
            occurrences = ad_decision_policy.validate_candidate_occurrences(source)
            basis = ad_decision_policy.normalize_keep_basis(
                decision.get("keep_basis"),
                source,
            )
        except ValueError:
            invalid_basis_ids.add(candidate_id)
            continue
        if (
            decision.get("candidate_fingerprint")
            != source.get("candidate_fingerprint")
            or decision.get("anchors_truncated") is not False
            or decision.get("anchors_truncated") != source.get("anchors_truncated")
            or decision.get("occurrence_count") != source.get("occurrence_count")
            or decision.get("anchor_ids")
            != [record["anchor_id"] for record in occurrences]
            or decision.get("anchor_text_sha256s")
            != [record["text_sha256"] for record in occurrences]
            or basis.get("reviewed_occurrences") != occurrences
        ):
            invalid_basis_ids.add(candidate_id)
            continue

        anchors = source.get("anchors")
        if not isinstance(anchors, list):
            invalid_basis_ids.add(candidate_id)
            continue
        mapped = [_mapped_occurrence(anchor, operations) for anchor in anchors]
        if any(item is None for item in mapped):
            invalid_basis_ids.add(candidate_id)
            continue
        anchor_ids = [record["anchor_id"] for record in occurrences]
        for anchor_id in anchor_ids:
            anchor_id_counts[anchor_id] = anchor_id_counts.get(anchor_id, 0) + 1
        pending.append(
            (
                candidate_id,
                anchor_ids,
                [item for item in mapped if item is not None],
            )
        )

    mapped_counts: dict[tuple[int, int, str], int] = {}
    for _, _, mapped in pending:
        for occurrence in mapped:
            mapped_counts[occurrence] = mapped_counts.get(occurrence, 0) + 1

    authorizations: dict[tuple[int, int, str], int] = {}
    for candidate_id, anchor_ids, mapped in pending:
        if any(anchor_id_counts[anchor_id] != 1 for anchor_id in anchor_ids) or any(
            mapped_counts[occurrence] != 1 for occurrence in mapped
        ):
            invalid_basis_ids.add(candidate_id)
            continue
        if len(set(mapped)) != len(mapped):
            invalid_basis_ids.add(candidate_id)
            continue
        for occurrence in mapped:
            authorizations[occurrence] = authorizations.get(occurrence, 0) + 1
    return authorizations, invalid_basis_ids


def residual_records(
    candidates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    source_candidates: list[dict[str, Any]] | None = None,
    operations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    by_text_sha256: dict[str, dict[str, str]] = {}
    formal_hash_counts: dict[str, dict[str, int]] = {}
    suppressible_keep_ids: set[str] = set()
    structured_basis_ids = {
        str(decision.get("candidate_id") or "")
        for decision in decisions
        if "keep_basis" in decision
    }
    structured_authorizations, invalid_basis_ids = _structured_keep_authorizations(
        decisions,
        source_candidates or [],
        operations or [],
    )
    for decision in decisions:
        candidate_id = str(decision.get("candidate_id") or "")
        if not candidate_id:
            continue
        verdict = str(decision.get("verdict") or "")
        evidence = decision.get("evidence")
        has_bound_evidence = isinstance(evidence, list) and bool(evidence)
        stored_hashes = decision.get("anchor_text_sha256s")
        valid_stored_hashes = (
            [
                value
                for value in stored_hashes
                if isinstance(value, str)
                and len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
            ]
            if isinstance(stored_hashes, list)
            else []
        )
        anchor_ids = decision.get("anchor_ids")
        hash_binding_complete = (
            isinstance(stored_hashes, list)
            and bool(stored_hashes)
            and len(valid_stored_hashes) == len(stored_hashes)
            and isinstance(anchor_ids, list)
            and len(anchor_ids) == len(stored_hashes)
            and all(isinstance(value, str) and value for value in anchor_ids)
        )
        has_delete_conflict = bool(decision.get("promoted_from")) or (
            isinstance(evidence, list)
            and any(
                isinstance(item, dict)
                and item.get("type") in RESIDUAL_KEEP_CONFLICT_EVIDENCE
                for item in evidence
            )
        )
        if (
            verdict == "keep"
            and decision.get("anchors_truncated") is False
            and hash_binding_complete
            and candidate_id not in invalid_basis_ids
            and (has_bound_evidence or "keep_basis" in decision)
            and (
                not has_delete_conflict
                or "keep_basis" in decision
            )
        ):
            suppressible_keep_ids.add(candidate_id)
        text_hashes = list(valid_stored_hashes)
        anchors = decision.get("anchors")
        if not text_hashes and isinstance(anchors, list):
            for anchor in anchors:
                if not isinstance(anchor, dict):
                    continue
                original = anchor.get("original")
                if isinstance(original, str) and original:
                    text_hashes.append(
                        hashlib.sha256(original.encode("utf-8")).hexdigest()
                    )
        for text_hash in text_hashes:
            by_text_sha256.setdefault(text_hash, {})[candidate_id] = verdict
            counts = formal_hash_counts.setdefault(text_hash, {})
            counts[candidate_id] = counts.get(candidate_id, 0) + 1

    records: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("priority") != "high" and candidate.get("risk_hint") != "low":
            continue
        residual_hash_counts: dict[str, int] = {}
        anchors = candidate.get("anchors")
        if isinstance(anchors, list):
            for anchor in anchors:
                if not isinstance(anchor, dict):
                    continue
                original = anchor.get("original")
                if isinstance(original, str) and original:
                    text_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
                    residual_hash_counts[text_hash] = (
                        residual_hash_counts.get(text_hash, 0) + 1
                    )
        matched = {
            candidate_id: verdict
            for text_hash in residual_hash_counts
            for candidate_id, verdict in by_text_sha256.get(text_hash, {}).items()
        }
        occurrence_count = candidate.get("occurrence_count")
        complete_residual_anchors = (
            candidate.get("anchors_truncated") is False
            and isinstance(occurrence_count, int)
            and not isinstance(occurrence_count, bool)
            and occurrence_count == sum(residual_hash_counts.values())
        )
        structured_matches: list[tuple[int, int, str]] = []
        structured_complete = complete_residual_anchors and bool(residual_hash_counts)
        if structured_complete and isinstance(anchors, list):
            for anchor in anchors:
                if not isinstance(anchor, dict):
                    structured_complete = False
                    break
                offset = anchor.get("offset")
                end = anchor.get("end")
                original = anchor.get("original")
                if (
                    not isinstance(offset, int)
                    or isinstance(offset, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or not isinstance(original, str)
                    or not original
                    or end != offset + len(original)
                ):
                    structured_complete = False
                    break
                occurrence = (
                    offset,
                    end,
                    hashlib.sha256(original.encode("utf-8")).hexdigest(),
                )
                if structured_authorizations.get(occurrence, 0) <= structured_matches.count(
                    occurrence
                ):
                    structured_complete = False
                    break
                structured_matches.append(occurrence)

        evidence_complete = complete_residual_anchors and bool(
            residual_hash_counts
        ) and all(
            text_hash in by_text_sha256
            and by_text_sha256[text_hash]
            and all(
                verdict == "keep"
                and candidate_id in suppressible_keep_ids
                and candidate_id not in structured_basis_ids
                for candidate_id, verdict in by_text_sha256[text_hash].items()
            )
            and sum(
                formal_hash_counts[text_hash].get(candidate_id, 0)
                for candidate_id in by_text_sha256[text_hash]
                if candidate_id in suppressible_keep_ids
            )
            >= residual_count
            for text_hash, residual_count in residual_hash_counts.items()
        )
        fully_reviewed_keep = structured_complete or evidence_complete
        if fully_reviewed_keep:
            if structured_complete:
                for occurrence in structured_matches:
                    structured_authorizations[occurrence] -= 1
            continue
        record = {
            "scan_candidate_id": candidate.get("candidate_id"),
            "matched_formal_candidate_ids": sorted(matched),
            "matched_formal_verdicts": dict(sorted(matched.items())),
            "sample": candidate.get("sample"),
            "signals": candidate.get("signals", []),
            "layer": candidate.get("layer"),
            "occurrence_count": candidate.get("occurrence_count"),
            "message": "verification scan still found a high-priority external-content candidate",
        }
        if len(matched) == 1:
            record["candidate_id"] = next(iter(matched))
        records.append(record)
    return records


def write_diff_html(path: Path, before_rel: str, after_rel: str, operations: list[dict[str, Any]]) -> None:
    rows = []
    for op in operations:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(op.get('candidate_id', '')))}</td>"
            f"<td>{html.escape(str(op.get('action', '')))}</td>"
            f"<td>{html.escape(str(op.get('start', '')))}</td>"
            f"<td><pre>{html.escape(str(op.get('original', '')))}</pre></td>"
            f"<td><pre>{html.escape(str(op.get('replacement', '')))}</pre></td>"
            f"<td>{html.escape(str(op.get('reason', '')))}</td>"
            "</tr>"
        )
    body = "\n".join(rows) if rows else "<tr><td colspan='6'>No operations recorded.</td></tr>"
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Novel Purifier Diff</title>
<style>
body {{ font-family: sans-serif; line-height: 1.5; margin: 24px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 6px; vertical-align: top; }}
pre {{ white-space: pre-wrap; margin: 0; }}
</style>
</head>
<body>
<h1>Diff Summary</h1>
<p>Before: {html.escape(before_rel)}<br>After: {html.escape(after_rel)}</p>
<table>
<thead><tr><th>Candidate</th><th>Action</th><th>Offset</th><th>Original</th><th>Replacement</th><th>Reason</th></tr></thead>
<tbody>
{body}
</tbody>
</table>
</body>
</html>
"""
    write_utf8(path, html_text)


def write_final_report(path: Path, report: dict[str, Any]) -> None:
    warnings = report["warnings"]
    lines = [
        "# Final Report",
        "",
        "## Summary",
        "",
        f"- Module: `{report['module']}`",
        f"- Before: `{report['before']}`",
        f"- After: `{report['after']}`",
        f"- Characters before: {report['char_counts']['before']}",
        f"- Characters after: {report['char_counts']['after']}",
        f"- Deletion ratio: {report['char_counts']['deletion_ratio']:.4%}",
        f"- Decisions: {report['decision_accounting']['decision_count']}",
        f"- Mutating decisions: {report['decision_accounting']['mutating_decision_count']}",
        f"- Operations: {report['decision_accounting']['operation_count']}",
        "",
        "## Warnings",
        "",
    ]
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Residual Ad Scan",
            "",
            f"- Candidate count: {report['residual_scan']['candidate_count']}",
            f"- By layer: `{report['residual_scan']['by_layer']}`",
            "",
            "## Rollback",
            "",
            '- Full rollback: run `python scripts/rollback.py "<workspace>" --level all`.',
            '- Ads module rollback: run `python scripts/rollback.py "<workspace>" --level module --module ads --overwrite`.',
            "",
        ]
    )
    write_utf8(path, "\n".join(lines))


def run(
    workspace: Path,
    module: str,
    before_value: str,
    after_value: str,
    decisions_value: str,
    skip_residual_scan: bool,
) -> dict[str, Any]:
    if module != "ads":
        raise ValueError("verification currently supports only the formally compiled ads module")
    with workspace_transaction_lock(workspace):
        return _run_locked(
            workspace,
            module,
            before_value,
            after_value,
            decisions_value,
            skip_residual_scan,
        )


def _run_locked(
    workspace: Path,
    module: str,
    before_value: str,
    after_value: str,
    decisions_value: str,
    skip_residual_scan: bool,
) -> dict[str, Any]:
    workspace = validate_workspace(workspace)
    manifest = load_manifest(workspace)
    stages = manifest.get("stages", {})
    apply_stage = stages.get("2_ads") if isinstance(stages, dict) else None
    if not isinstance(apply_stage, dict):
        apply_stage = {}
    current_path = resolve_current_head(workspace)
    current_rel = current_path.relative_to(workspace).as_posix()
    selected_after = current_rel if after_value == "auto" else after_value
    apply_output_value = apply_stage.get("output")
    if not isinstance(apply_output_value, str) or not apply_output_value:
        apply_output_value = selected_after
    layout_required = selected_after.replace("\\", "/") != apply_output_value.replace("\\", "/")
    layout_stage = stages.get("5_layout") if isinstance(stages, dict) else None
    if not isinstance(layout_stage, dict):
        layout_stage = {}
    layout_report_value = layout_stage.get("report")
    reads_values = {
        "before": before_value,
        "apply_output": apply_output_value,
        "after": selected_after,
        "decisions": decisions_value,
        "operations": "logs/operations.jsonl",
        "anomalies": "logs/anomalies.jsonl",
    }
    if layout_required and isinstance(layout_report_value, str) and layout_report_value:
        reads_values["layout_report"] = layout_report_value
    workspace, reads, writes = resolve_workspace_paths(
        workspace,
        reads=reads_values,
        writes={
            "verify_report": "report/verify_report.json",
            "diff": "report/diff_v1_v2.html",
            "final_report": "report/final_report.md",
        },
    )
    before_path = reads["before"]
    apply_output_path = reads["apply_output"]
    after_path = reads["after"]
    decisions_path = reads["decisions"]
    operations_path = reads["operations"]
    anomalies_path = reads["anomalies"]

    before_text = read_utf8(before_path)
    apply_output_text = read_utf8(apply_output_path)
    after_text = read_utf8(after_path)
    before_chapters = chapter_summary(before_text)
    apply_output_chapters = chapter_summary(apply_output_text)
    after_chapters = chapter_summary(after_text)
    before_rel = before_path.relative_to(workspace).as_posix()
    apply_output_rel = apply_output_path.relative_to(workspace).as_posix()
    after_rel = after_path.relative_to(workspace).as_posix()
    decisions_rel = decisions_path.relative_to(workspace).as_posix()

    before_count = count_chars(before_text)
    apply_output_count = count_chars(apply_output_text)
    after_count = count_chars(after_text)
    deletion_ratio = max(0, before_count - apply_output_count) / max(1, before_count)
    warnings: list[str] = []
    if deletion_ratio > 0.08:
        warnings.append("deletion ratio is above 8%")
    chapter_shrink = compare_chapters(
        before_chapters["chapters"],
        apply_output_chapters["chapters"],
    )
    if chapter_shrink:
        warnings.append(f"{len(chapter_shrink)} chapters shrank by more than 20% and 500 chars")

    decisions = load_jsonl(decisions_path)
    provenance: dict[str, Any] = {}
    provenance_issues: list[str] = []
    source_candidates: list[dict[str, Any]] = []
    try:
        provenance = validate_formal_ad_provenance(
            workspace,
            before_path,
            decisions_path,
            decisions,
            manifest=manifest,
            require_ready=False,
        )
        source_scan_report = read_ads_scan_report(workspace)
        source_pages = source_scan_report.get("pages")
        source_pages_dir = (
            source_pages.get("pages_dir") if isinstance(source_pages, dict) else None
        )
        if not isinstance(source_pages_dir, str) or not source_pages_dir:
            raise ValueError("verification source scan pages are missing")
        source_candidates, _, _ = load_current_ad_candidates(
            workspace,
            source_pages_dir,
            all_pages=True,
            require_complete=True,
        )
    except (OSError, ValueError) as error:
        provenance_issues.append(str(error))
    formal_uncertain = sorted(
        str(decision.get("candidate_id") or "<missing>")
        for decision in decisions
        if decision.get("verdict") == "uncertain"
    )
    active_run_id = apply_stage.get("active_run_id")
    operations = (
        load_jsonl_for_run(operations_path, active_run_id)
        if isinstance(active_run_id, str) and active_run_id
        else []
    )
    anomalies = (
        load_jsonl_for_run(anomalies_path, active_run_id)
        if isinstance(active_run_id, str) and active_run_id
        else []
    )
    accounting = verify_decision_accounting(decisions, operations)
    input_sha256 = sha256_file(before_path)
    decision_sha256 = sha256_file(decisions_path)
    apply_output_sha256 = sha256_file(apply_output_path)
    output_sha256 = sha256_file(after_path)
    current_head = str(manifest.get("current_head") or "")
    current_record = manifest.get("artifacts", {}).get(current_head, {})
    if not isinstance(current_record, dict):
        current_record = {}

    binding_issues: list[str] = []
    expected_bindings = {
        "input": before_rel,
        "decisions": decisions_rel,
        "output": apply_output_rel,
        "input_sha256": input_sha256,
        "decision_sha256": decision_sha256,
        "output_sha256": apply_output_sha256,
    }
    if apply_stage.get("status") != "done":
        binding_issues.append("apply stage is not done")
    if not isinstance(active_run_id, str) or not active_run_id:
        binding_issues.append("apply stage has no active_run_id")
    for field, expected in expected_bindings.items():
        actual = apply_stage.get(field)
        if actual != expected:
            binding_issues.append(f"apply {field} does not match the verified artifact")
    if current_head != after_rel or current_path != after_path:
        binding_issues.append("verified output is not the manifest current_head")

    operation_binding_issues: list[str] = []
    for operation in operations:
        anchor_id = str(operation.get("anchor_id") or "<missing>")
        for field, expected in (
            ("run_id", active_run_id),
            ("module", module),
            ("input", before_rel),
            ("decisions", decisions_rel),
            ("output", apply_output_rel),
            ("input_sha256", input_sha256),
            ("decision_sha256", decision_sha256),
            ("output_sha256", apply_output_sha256),
        ):
            if operation.get(field) != expected:
                operation_binding_issues.append(f"operation {anchor_id} has stale {field}")
        if not isinstance(operation.get("candidate_fingerprint"), str):
            operation_binding_issues.append(
                f"operation {anchor_id} has no candidate_fingerprint"
            )

    replayed, replay_issues = replay_operations(before_text, operations)
    replay_matches = replayed == apply_output_text if replayed is not None else False
    segment_plan_issues = verify_segment_plan_replay(
        before_text,
        apply_output_text,
        decisions,
        operations,
    )
    chapter_identity = compare_chapter_identity(
        before_chapters["chapters"],
        apply_output_chapters["chapters"],
    )
    final_chapter_identity = compare_layout_chapters(
        apply_output_chapters["chapters"],
        after_chapters["chapters"],
    )

    layout_binding_issues: list[str] = []
    layout_replay_issues: list[str] = []
    layout_report: dict[str, Any] = {}
    layout_run_id: str | None = None
    layout_config_sha256: str | None = None
    layout_idempotent = True
    if layout_required:
        layout_report_path = reads.get("layout_report")
        if layout_report_path is None:
            layout_binding_issues.append("layout stage has no committed report")
        else:
            try:
                loaded_layout_report = json.loads(layout_report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                layout_binding_issues.append(f"layout report is invalid: {error}")
            else:
                if isinstance(loaded_layout_report, dict):
                    layout_report = loaded_layout_report
                else:
                    layout_binding_issues.append("layout report is not an object")

        layout_run_id_value = layout_stage.get("active_run_id")
        if isinstance(layout_run_id_value, str) and layout_run_id_value:
            layout_run_id = layout_run_id_value
        else:
            layout_binding_issues.append("layout stage has no active_run_id")
        if layout_stage.get("status") != "done":
            layout_binding_issues.append("layout stage is not done")

        layout_config_value = layout_report.get("config_sha256")
        if isinstance(layout_config_value, str) and len(layout_config_value) == 64:
            layout_config_sha256 = layout_config_value
        else:
            layout_binding_issues.append("layout report has no valid config_sha256")

        expected_layout_bindings = {
            "input": apply_output_rel,
            "output": after_rel,
            "input_sha256": apply_output_sha256,
            "output_sha256": output_sha256,
            "config_sha256": layout_config_sha256,
            "active_run_id": layout_run_id,
        }
        for field, expected in expected_layout_bindings.items():
            if layout_stage.get(field) != expected:
                layout_binding_issues.append(f"layout stage has stale {field}")
            if layout_report.get(field) != expected:
                layout_binding_issues.append(f"layout report has stale {field}")

        layout_artifacts = layout_stage.get("artifacts")
        layout_report_rel = (
            layout_report_path.relative_to(workspace).as_posix()
            if layout_report_path is not None
            else None
        )
        if (
            not isinstance(layout_artifacts, list)
            or after_rel not in layout_artifacts
            or layout_report_rel not in layout_artifacts
        ):
            layout_binding_issues.append("layout stage does not own its current artifacts")
        if current_record.get("parent_path") != apply_output_rel:
            layout_binding_issues.append("layout output parent path is stale")
        if current_record.get("parent_sha256") != apply_output_sha256:
            layout_binding_issues.append("layout output parent SHA is stale")
        if current_record.get("config_sha256") != layout_config_sha256:
            layout_binding_issues.append("layout output config SHA is stale")

        layout_config = layout_report.get("config")
        if not isinstance(layout_config, dict):
            layout_replay_issues.append("layout report has no replayable config")
        else:
            calculated_config_sha256 = hashlib.sha256(
                json.dumps(
                    layout_config,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if calculated_config_sha256 != layout_config_sha256:
                layout_replay_issues.append("layout config content does not match config_sha256")
            try:
                replayed_layout, _ = normalize_text(apply_output_text, layout_config)
                rerun_layout, _ = normalize_text(after_text, layout_config)
            except (RuntimeError, ValueError) as error:
                layout_replay_issues.append(f"layout replay failed: {error}")
            else:
                if replayed_layout != after_text:
                    layout_replay_issues.append("layout replay differs from the current head")
                layout_idempotent = rerun_layout == after_text
                if not layout_idempotent:
                    layout_replay_issues.append("layout normalization is not idempotent")

    residual_summary = {
        "candidate_count": 0,
        "total_candidate_count": 0,
        "by_layer": {},
        "strong_candidate_count": 0,
        "skipped": True,
        "complete": False,
    }
    residuals: list[dict[str, Any]] = []
    if not skip_residual_scan:
        residual_candidates, residual_summary = scan_candidates(
            after_text,
            after_chapters["chapters"],
            max_candidates=300,
        )
        residuals = residual_records(
            residual_candidates,
            decisions,
            source_candidates=source_candidates,
            operations=operations,
        )
        residual_summary["candidate_count"] = len(residual_candidates)
        residual_summary["total_candidate_count"] = len(residual_candidates)
        residual_summary["strong_candidate_count"] = len(residuals)
        residual_summary["skipped"] = False
        residual_summary["complete"] = True

    checks = [
        {"name": "apply_binding", "passed": not binding_issues, "issues": binding_issues},
        {
            "name": "scan_decision_provenance",
            "passed": not provenance_issues,
            "issues": provenance_issues,
            "details": provenance,
        },
        {
            "name": "operation_binding",
            "passed": not operation_binding_issues,
            "issues": operation_binding_issues,
        },
        {
            "name": "anchor_accounting",
            "passed": not any(
                accounting[key]
                for key in (
                    "missing_operation_anchor_ids",
                    "unexpected_operation_anchor_ids",
                    "duplicate_operation_anchor_ids",
                    "missing_operation_candidate_ids",
                    "unexpected_operation_candidate_ids",
                )
            ),
            "details": accounting,
        },
        {
            "name": "formal_uncertain",
            "passed": not formal_uncertain,
            "candidate_ids": formal_uncertain,
        },
        {
            "name": "operation_replay",
            "passed": replay_matches and not replay_issues,
            "issues": replay_issues
            + ([] if replay_matches else ["replayed apply output differs"]),
        },
        {
            "name": "segment_plan_replay",
            "passed": not segment_plan_issues,
            "issues": segment_plan_issues,
        },
        {"name": "apply_chapter_identity", **chapter_identity},
        {
            "name": "layout_binding",
            "passed": not layout_binding_issues,
            "applied": layout_required,
            "issues": layout_binding_issues,
        },
        {
            "name": "layout_replay",
            "passed": not layout_replay_issues,
            "applied": layout_required,
            "idempotent": layout_idempotent,
            "issues": layout_replay_issues,
        },
        {"name": "final_chapter_structure", **final_chapter_identity},
        {"name": "current_run_anomalies", "passed": not anomalies, "count": len(anomalies)},
        {
            "name": "complete_residual_scan",
            "passed": not skip_residual_scan and bool(residual_summary.get("complete")),
            "skipped": skip_residual_scan,
        },
        {
            "name": "strong_residuals",
            "passed": not residuals,
            "count": len(residuals),
        },
        {"name": "risk_warnings", "passed": not warnings, "warnings": warnings},
    ]
    blocking_checks = [
        check
        for check in checks
        if check["name"] != "complete_residual_scan" and not check.get("passed")
    ]
    if blocking_checks:
        status = "blocked"
    elif skip_residual_scan:
        status = "incomplete"
    else:
        status = "passed"

    report = {
        "status": status,
        "module": module,
        "before": before_rel,
        "apply_output": apply_output_rel,
        "after": after_rel,
        "decisions": decisions_rel,
        "apply_run_id": active_run_id,
        "layout_run_id": layout_run_id,
        "layout_config_sha256": layout_config_sha256,
        "input_sha256": input_sha256,
        "decision_sha256": decision_sha256,
        "apply_output_sha256": apply_output_sha256,
        "output_sha256": output_sha256,
        "char_counts": {
            "before": before_count,
            "apply_output": apply_output_count,
            "after": after_count,
            "delta": after_count - before_count,
            "deletion_ratio": deletion_ratio,
        },
        "chapter_counts": {
            "before": before_chapters["count"],
            "apply_output": apply_output_chapters["count"],
            "after": after_chapters["count"],
            "apply_identity": chapter_identity,
            "final_structure": final_chapter_identity,
            "chapter_shrink_warnings": chapter_shrink,
        },
        "decision_accounting": accounting,
        "scan_decision_provenance": provenance,
        **{field: provenance.get(field) for field in PROVENANCE_IDENTITY_FIELDS},
        "anomaly_count": len(anomalies),
        "residual_scan": residual_summary,
        "residuals": residuals,
        "warnings": warnings,
        "checks": checks,
    }

    with WorkspaceTransaction(workspace) as transaction:
        attestation: dict[str, Any] | None = None
        if status == "passed":
            attestation = {
                "schema_version": 3,
                "status": "passed",
                "verification_run_id": transaction.run_id,
                "apply_run_id": active_run_id,
                "apply_output": apply_output_rel,
                "apply_output_sha256": apply_output_sha256,
                "layout_run_id": layout_run_id,
                "layout_config_sha256": layout_config_sha256,
                "current_head": current_head,
                "current_head_sha256": output_sha256,
                "parent_path": current_record.get("parent_path"),
                "parent_sha256": current_record.get("parent_sha256"),
                "input_sha256": input_sha256,
                "decision_sha256": decision_sha256,
                "formal_run_id": provenance.get("formal_run_id"),
                "formal_report_sha256": provenance.get("formal_report_sha256"),
                "scan_id": provenance.get("scan_id"),
                "candidate_set_sha256": provenance.get("candidate_set_sha256"),
                **{
                    field: provenance.get(field)
                    for field in PROVENANCE_IDENTITY_FIELDS
                },
                "rule_version": VERIFY_RULE_VERSION,
                "checks": checks,
            }
            report["attestation"] = attestation
        write_json(transaction.stage_path(writes["verify_report"]), report)
        write_diff_html(
            transaction.stage_path(writes["diff"]),
            str(before_path.relative_to(workspace)),
            str(apply_output_path.relative_to(workspace)),
            operations,
        )
        write_final_report(transaction.stage_path(writes["final_report"]), report)
        transaction.commit(
            {
                "6_verify": (
                    status,
                    {
                        "report": "report/verify_report.json",
                        "final_report": "report/final_report.md",
                        "diff": "report/diff_v1_v2.html",
                        "warnings": warnings,
                        "input": before_rel,
                        "apply_output": apply_output_rel,
                        "output": after_rel,
                        "input_sha256": input_sha256,
                        "decision_sha256": decision_sha256,
                        "formal_run_id": provenance.get("formal_run_id"),
                        "formal_report_sha256": provenance.get("formal_report_sha256"),
                        "scan_id": provenance.get("scan_id"),
                        "candidate_set_sha256": provenance.get("candidate_set_sha256"),
                        **{
                            field: provenance.get(field)
                            for field in PROVENANCE_IDENTITY_FIELDS
                        },
                        "apply_output_sha256": apply_output_sha256,
                        "output_sha256": output_sha256,
                        "apply_run_id": active_run_id,
                        "layout_run_id": layout_run_id,
                        "layout_config_sha256": layout_config_sha256,
                        **({"attestation": attestation} if attestation is not None else {}),
                    },
                )
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify stage outputs and generate reports.")
    parser.add_argument("workspace", help="Path to the .cleanwork directory.")
    parser.add_argument("--module", default="ads")
    parser.add_argument("--before", default="versions/v1_preprocessed.txt")
    parser.add_argument("--after", default="auto")
    parser.add_argument("--decisions", default="decisions/ads_decisions.jsonl")
    parser.add_argument("--skip-residual-scan", action="store_true")
    args = parser.parse_args()

    report = run(
        workspace=Path(args.workspace).resolve(),
        module=args.module,
        before_value=args.before,
        after_value=args.after,
        decisions_value=args.decisions,
        skip_residual_scan=args.skip_residual_scan,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "warnings": report["warnings"],
                "char_counts": report["char_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if report["status"] != "passed":
        sys.exit(1)


if __name__ == "__main__":
    main()
