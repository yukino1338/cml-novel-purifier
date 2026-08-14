from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from common import (
    WorkspaceTransaction,
    append_jsonl,
    load_jsonl,
    load_manifest,
    read_utf8,
    resolve_in_workspace,
    resolve_workspace_paths,
    sha256_file,
    workspace_transaction_lock,
    write_json,
    write_utf8,
)
from finalize_ad_decisions import (
    compile_formal_decisions,
    validate_current_draft_provenance,
)
from make_ad_decisions import load_current_ad_candidates, read_ads_scan_report
import ad_decision_policy


MUTATING_VERDICTS = {"delete"}
MUTATING_ACTIONS = {"delete"}
ALLOWED_SPLICE_STRATEGIES = {
    "",
    "exact",
    "exact_segment",
    "fallback_newline",
    "remove_paragraph",
}
MODULE_STAGES = {"ads": "2_ads"}
FORMAL_IDENTITY_FIELDS = (
    "profile",
    "scan_rule_pack_sha256",
    "draft_rule_pack_sha256",
    "book_profile_sha256",
    "book_profile_file_sha256",
    "profile_present",
)


@dataclass
class Operation:
    decision: dict[str, Any]
    candidate_id: str
    action: str
    start: int
    end: int
    replacement: str
    original: str
    strategy: str
    anchor_id: str
    candidate_fingerprint: str
    scan_id: str
    edit_plan_id: str | None = None
    parent_start: int | None = None
    parent_end: int | None = None
    expected_after_sha256: str | None = None


def decision_action(decision: dict[str, Any]) -> str | None:
    verdict = str(decision.get("verdict", "")).strip()
    action = str(decision.get("action", "")).strip()
    if verdict in MUTATING_VERDICTS:
        return verdict
    if action in MUTATING_ACTIONS:
        return action
    return None


def decision_anchors(decision: dict[str, Any]) -> list[dict[str, Any]]:
    anchors = decision.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        raise ValueError("formal decision anchors must be a non-empty list")
    if not all(isinstance(anchor, dict) for anchor in anchors):
        raise ValueError("every formal decision anchor must be an object")
    return anchors


def _required_string(record: dict[str, Any], field: str, label: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} {field} must be a non-empty string")
    return value


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _portable_relative_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\\" not in value
        and not PurePosixPath(value).is_absolute()
        and PurePosixPath(value).as_posix() == value
        and all(part not in {"", ".", ".."} for part in PurePosixPath(value).parts)
    )


def validate_decision_set(
    decisions: list[dict[str, Any]],
    *,
    require_identity: bool = False,
) -> None:
    candidate_ids: set[str] = set()
    fingerprints: set[str] = set()
    anchor_ids: set[str] = set()
    scan_ids: set[str] = set()
    identity_values = {field: set() for field in FORMAL_IDENTITY_FIELDS}
    for decision in decisions:
        candidate_id = _required_string(decision, "candidate_id", "decision")
        fingerprint = _required_string(decision, "candidate_fingerprint", "decision")
        scan_id = _required_string(decision, "scan_id", "decision")
        if candidate_id in candidate_ids:
            raise ValueError(f"duplicate candidate_id in formal decisions: {candidate_id}")
        if fingerprint in fingerprints:
            raise ValueError(f"duplicate candidate fingerprint in formal decisions: {fingerprint}")
        candidate_ids.add(candidate_id)
        fingerprints.add(fingerprint)
        scan_ids.add(scan_id)

        if require_identity:
            profile = decision.get("profile")
            if not _portable_relative_path(profile):
                raise ValueError(
                    "decision profile must be a workspace-relative POSIX path"
                )
            identity_values["profile"].add(profile)

            for field in (
                "scan_rule_pack_sha256",
                "draft_rule_pack_sha256",
                "book_profile_sha256",
            ):
                value = decision.get(field)
                if not _sha256(value):
                    raise ValueError(f"decision {field} must be a lowercase SHA-256 value")
                identity_values[field].add(value)
            profile_present = decision.get("profile_present")
            if not isinstance(profile_present, bool):
                raise ValueError("decision profile_present must be boolean")
            identity_values["profile_present"].add(profile_present)
            profile_file_sha256 = decision.get("book_profile_file_sha256")
            if profile_present:
                if not _sha256(profile_file_sha256):
                    raise ValueError(
                        "decision book_profile_file_sha256 must bind the present profile"
                    )
            elif profile_file_sha256 is not None:
                raise ValueError(
                    "decision book_profile_file_sha256 must be null when the profile is absent"
                )
            identity_values["book_profile_file_sha256"].add(profile_file_sha256)

        action = decision_action(decision)
        truncated = decision.get("anchors_truncated", False)
        if not isinstance(truncated, bool):
            raise ValueError(f"candidate {candidate_id} anchors_truncated must be boolean")
        if action is not None and truncated:
            raise ValueError(f"candidate {candidate_id} has truncated anchors and cannot mutate text")

        anchors = decision.get("anchors")
        if action is not None:
            anchors = decision_anchors(decision)
        elif anchors is None:
            anchors = []
        elif not isinstance(anchors, list) or not all(
            isinstance(anchor, dict) for anchor in anchors
        ):
            raise ValueError("non-mutating formal decision anchors must be an array of objects")

        for anchor in anchors:
            anchor_id = _required_string(anchor, "anchor_id", "anchor")
            if anchor_id in anchor_ids:
                raise ValueError(f"duplicate anchor_id in formal decisions: {anchor_id}")
            anchor_ids.add(anchor_id)
            strategy = anchor.get("splice_strategy", decision.get("splice_strategy", ""))
            if strategy is None:
                strategy = ""
            if not isinstance(strategy, str) or strategy not in ALLOWED_SPLICE_STRATEGIES:
                raise ValueError(f"anchor {anchor_id} has an unsupported splice strategy")
        if action is not None:
            strategy = decision.get("splice_strategy", "")
            has_plan = decision.get("edit_plan") is not None or decision.get("edit_plan_id") is not None
            if strategy == "exact_segment":
                if not isinstance(decision.get("edit_plan_id"), str) or not isinstance(
                    decision.get("edit_plan"), dict
                ):
                    raise ValueError(
                        f"candidate {candidate_id} exact_segment has no bound edit plan"
                    )
                ad_decision_policy.normalize_edit_plan(decision["edit_plan"], decision)
            elif has_plan:
                raise ValueError(
                    f"candidate {candidate_id} edit plan requires exact_segment"
                )

    if len(scan_ids) > 1:
        raise ValueError("formal decisions contain more than one scan_id")
    if require_identity:
        mixed = sorted(field for field, values in identity_values.items() if len(values) > 1)
        if mixed:
            raise ValueError(f"formal decisions contain mixed provenance identity: {mixed}")


def validate_formal_identity(
    record: dict[str, Any],
    expected: dict[str, Any],
    *,
    label: str,
) -> None:
    for field in FORMAL_IDENTITY_FIELDS:
        if record.get(field) != expected.get(field):
            raise ValueError(f"formal decision provenance {label} {field} is stale")


def _portable(value: object) -> str:
    return str(value or "").replace("\\", "/")


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _formal_artifact_record(
    manifest: dict[str, Any],
    relative: str,
    *,
    run_id: str,
    sha256: str,
) -> None:
    artifacts = manifest.get("artifacts")
    record = artifacts.get(relative) if isinstance(artifacts, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("stage") != "2_ads"
        or record.get("run_id") != run_id
        or record.get("sha256") != sha256
    ):
        raise ValueError(f"formal decision provenance artifact is stale: {relative}")


def validate_formal_ad_provenance(
    workspace: Path,
    input_path: Path,
    decisions_path: Path,
    decisions: list[dict[str, Any]],
    *,
    manifest: dict[str, Any] | None = None,
    require_ready: bool,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    manifest = load_manifest(workspace) if manifest is None else manifest
    stages = manifest.get("stages")
    stage = stages.get("2_ads") if isinstance(stages, dict) else None
    if not isinstance(stage, dict):
        raise ValueError("formal decision provenance stage is missing")

    status = stage.get("status")
    allowed_statuses = (
        {"formal_decisions_ready", "done"}
        if require_ready
        else {"done"}
    )
    if status not in allowed_statuses:
        allowed = ", ".join(sorted(allowed_statuses))
        raise ValueError(f"formal decision provenance requires 2_ads status in: {allowed}")

    input_rel = input_path.relative_to(workspace).as_posix()
    decisions_rel = decisions_path.relative_to(workspace).as_posix()
    formal_decisions_rel = _portable(stage.get("formal_decisions"))
    formal_report_rel = _portable(stage.get("formal_report"))
    if formal_decisions_rel != decisions_rel:
        raise ValueError("formal decision provenance path does not match the selected decisions")
    if not formal_report_rel:
        raise ValueError("formal decision provenance report path is missing")

    formal_stage_owned = status == "formal_decisions_ready"
    formal_run_id = stage.get("run_id") if formal_stage_owned else stage.get("formal_run_id")
    if not isinstance(formal_run_id, str) or not formal_run_id:
        raise ValueError("formal decision provenance run_id is missing")

    decisions_sha256 = sha256_file(decisions_path)
    formal_decisions_sha256 = stage.get("formal_decisions_sha256")
    formal_report_sha256 = stage.get("formal_report_sha256")
    if formal_decisions_sha256 != decisions_sha256:
        raise ValueError("formal decision provenance SHA does not match the decisions")
    if not isinstance(formal_report_sha256, str) or not formal_report_sha256:
        raise ValueError("formal decision provenance report SHA is missing")
    if not formal_stage_owned:
        if _portable(stage.get("decisions")) != decisions_rel:
            raise ValueError("applied decision path does not match formal provenance")
        if stage.get("decision_sha256") != decisions_sha256:
            raise ValueError("applied decision SHA does not match formal provenance")

    report_path = resolve_in_workspace(workspace, formal_report_rel, role="read")
    if sha256_file(report_path) != formal_report_sha256:
        raise ValueError("formal decision provenance report SHA is stale")
    _formal_artifact_record(
        manifest,
        decisions_rel,
        run_id=formal_run_id,
        sha256=decisions_sha256,
    )
    _formal_artifact_record(
        manifest,
        formal_report_rel,
        run_id=formal_run_id,
        sha256=formal_report_sha256,
    )
    if formal_stage_owned:
        owned = stage.get("artifacts")
        if (
            not isinstance(owned, list)
            or decisions_rel not in owned
            or formal_report_rel not in owned
        ):
            raise ValueError("formal decision provenance artifacts are not stage-owned")

    formal_report = _load_json_object(report_path, "formal decision report")
    scan_report = read_ads_scan_report(workspace)
    pages = scan_report.get("pages")
    pages_dir = pages.get("pages_dir") if isinstance(pages, dict) else None
    if not isinstance(pages_dir, str) or not pages_dir:
        raise ValueError("formal decision provenance scan pages are missing")
    candidates, candidate_paths, scan_report = load_current_ad_candidates(
        workspace,
        pages_dir,
        all_pages=True,
        require_complete=True,
    )
    expected_inputs = [
        path.relative_to(workspace).as_posix()
        for path in candidate_paths
    ]
    report_inputs = formal_report.get("inputs")
    if (
        not isinstance(report_inputs, list)
        or [_portable(value) for value in report_inputs] != expected_inputs
    ):
        raise ValueError("formal decision provenance candidate pages are stale")
    if _portable(scan_report.get("input")) != input_rel:
        raise ValueError("formal decision provenance scan input does not match apply input")

    scan_id = scan_report.get("scan_id")
    candidate_set_sha256 = scan_report.get("candidate_set_sha256")
    for label, actual, expected in (
        ("scan_id", formal_report.get("scan_id"), scan_id),
        (
            "candidate_set_sha256",
            formal_report.get("candidate_set_sha256"),
            candidate_set_sha256,
        ),
        ("output", _portable(formal_report.get("output")), decisions_rel),
        ("formal_decisions_sha256", formal_report.get("formal_decisions_sha256"), decisions_sha256),
        ("candidate_count", formal_report.get("candidate_count"), len(candidates)),
        ("reported_candidate_count", formal_report.get("reported_candidate_count"), len(candidates)),
        ("decision_count", formal_report.get("decision_count"), len(decisions)),
        ("duplicate_candidate_count", formal_report.get("duplicate_candidate_count"), 0),
    ):
        if actual != expected:
            raise ValueError(f"formal decision provenance {label} is stale")
    if stage.get("scan_id") != scan_id or stage.get("candidate_set_sha256") != candidate_set_sha256:
        raise ValueError("formal decision provenance does not match the committed scan")
    if stage.get("formal_decision_count") != len(decisions):
        raise ValueError("formal decision provenance count is stale")

    reviews_value = formal_report.get("reviews")
    if not isinstance(reviews_value, str) or not reviews_value:
        raise ValueError("formal decision provenance reviews path is missing")
    reviews_path = resolve_in_workspace(workspace, reviews_value, role="read")
    reviews_sha256 = sha256_file(reviews_path)
    if (
        formal_report.get("reviews_sha256") != reviews_sha256
        or stage.get("formal_reviews_sha256") != reviews_sha256
    ):
        raise ValueError("formal decision provenance reviews SHA is stale")
    reviews = load_jsonl(reviews_path)

    draft_value = formal_report.get("draft")
    if not isinstance(draft_value, str) or not draft_value:
        raise ValueError("formal decision provenance draft path is missing")
    draft_path = resolve_in_workspace(workspace, draft_value, role="read")
    draft_sha256 = sha256_file(draft_path)
    drafts = load_jsonl(draft_path)
    if (
        formal_report.get("draft_sha256") != draft_sha256
        or stage.get("formal_draft_sha256") != draft_sha256
    ):
        raise ValueError("formal decision provenance draft SHA is stale")
    draft_provenance = validate_current_draft_provenance(
        workspace,
        draft_path,
        drafts,
        candidates,
        scan_report,
        manifest=manifest,
    )

    validate_formal_identity(formal_report, draft_provenance, label="report")
    validate_formal_identity(stage, draft_provenance, label="stage")

    validate_decision_set(decisions, require_identity=True)
    for decision in decisions:
        validate_formal_identity(decision, draft_provenance, label="row")
    try:
        compiled = compile_formal_decisions(
            candidates,
            reviews,
            drafts,
            scan_id=str(scan_id),
            provenance=draft_provenance,
        )
    except ValueError as error:
        raise ValueError("formal decision provenance cannot be recompiled") from error
    if compiled != decisions:
        raise ValueError("formal decisions do not match the current formal compilation")

    return {
        "formal_run_id": formal_run_id,
        "formal_report": formal_report_rel,
        "formal_report_sha256": formal_report_sha256,
        "formal_decisions": decisions_rel,
        "formal_decisions_sha256": decisions_sha256,
        "formal_reviews_sha256": reviews_sha256,
        "formal_draft_sha256": draft_sha256,
        "scan_id": scan_id,
        "candidate_set_sha256": candidate_set_sha256,
        **draft_provenance,
    }


def find_anchor(text: str, anchor: dict[str, Any]) -> tuple[int, int] | str:
    original = anchor.get("original")
    if not isinstance(original, str) or not original:
        return "missing non-empty original anchor"

    prefix = anchor.get("prefix", "")
    suffix = anchor.get("suffix", "")
    if prefix is None:
        prefix = ""
    if suffix is None:
        suffix = ""
    if not isinstance(prefix, str) or not isinstance(suffix, str):
        return "prefix and suffix must be strings"

    offset = anchor.get("offset")
    if offset is not None:
        if not isinstance(offset, int) or isinstance(offset, bool):
            return "anchor offset must be an integer"
        if offset < 0 or offset + len(original) > len(text):
            return "anchor offset is outside the input text"
        start = offset
        end = start + len(original)
        declared_end = anchor.get("end")
        if declared_end is not None and (
            not isinstance(declared_end, int)
            or isinstance(declared_end, bool)
            or declared_end != end
        ):
            return "anchor end does not match offset and original"
        if text[start:end] != original:
            return "offset anchor does not match original text"
        if prefix and text[max(0, start - len(prefix)) : start] != prefix:
            return "offset prefix does not match current text"
        if suffix and text[end : end + len(suffix)] != suffix:
            return "offset suffix does not match current text"
        return start, end

    if prefix or suffix:
        needle = prefix + original + suffix
        first = text.find(needle)
        if first < 0:
            return "combined prefix/original/suffix anchor not found"
        second = text.find(needle, first + 1)
        if second >= 0:
            return "combined anchor is not unique"
        start = first + len(prefix)
        return start, start + len(original)

    first = text.find(original)
    if first < 0:
        return "original anchor not found"
    second = text.find(original, first + 1)
    if second >= 0:
        return "original anchor is not unique; provide prefix and suffix"
    return first, first + len(original)


def paragraph_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    p_start = text.rfind("\n", 0, start) + 1
    p_end = text.find("\n", end)
    if p_end < 0:
        p_end = len(text)
    else:
        p_end += 1
    return p_start, p_end


def build_operation(
    text: str,
    decision: dict[str, Any],
    anchor: dict[str, Any],
    action: str,
) -> Operation | str:
    if action not in MUTATING_ACTIONS:
        return f"unsupported action: {action}"

    found = find_anchor(text, anchor)
    if isinstance(found, str):
        return found
    start, end = found
    anchor_original = text[start:end]
    strategy = str(anchor.get("splice_strategy") or decision.get("splice_strategy") or "")

    replacement = ""
    edit_plan_id: str | None = None
    parent_start: int | None = None
    parent_end: int | None = None
    expected_after_sha256: str | None = None
    if strategy == "exact_segment":
        plan = ad_decision_policy.normalize_edit_plan(decision.get("edit_plan"), decision)
        if decision.get("edit_plan_id") != plan["edit_plan_id"]:
            return "exact_segment edit_plan_id is stale"
        matching = [
            occurrence
            for occurrence in plan["occurrence_plans"]
            if occurrence["anchor_id"] == anchor.get("anchor_id")
        ]
        if len(matching) != 1:
            return "exact_segment occurrence binding is missing or duplicated"
        occurrence = matching[0]
        deleted_ids = set(occurrence["delete_segment_ids"])
        deleted = [
            segment
            for segment in occurrence["segments"]
            if segment["segment_id"] in deleted_ids
        ]
        if len(deleted) != 1:
            return "exact_segment must contain one contiguous external segment"
        segment = deleted[0]
        parent_start, parent_end = start, end
        start = parent_start + int(segment["relative_start"])
        end = parent_start + int(segment["relative_end"])
        replacement = occurrence["joiner"]
        edit_plan_id = plan["edit_plan_id"]
        expected_after_sha256 = occurrence["expected_after_sha256"]
        if text[start:end] != anchor_original[
            int(segment["relative_start"]):int(segment["relative_end"])
        ]:
            return "exact_segment text does not match the bound parent"
    elif strategy == "fallback_newline":
        replacement = "\n"
    elif strategy == "remove_paragraph":
        p_start, p_end = paragraph_bounds(text, start, end)
        paragraph = text[p_start:p_end].strip()
        if paragraph != anchor_original.strip():
            return "remove_paragraph refused because original is not the full paragraph"
        start, end = p_start, p_end

    original = text[start:end]
    return Operation(
        decision=decision,
        candidate_id=str(decision.get("candidate_id") or ""),
        action=action,
        start=start,
        end=end,
        replacement=replacement,
        original=original,
        strategy=strategy,
        anchor_id=str(anchor["anchor_id"]),
        candidate_fingerprint=str(decision["candidate_fingerprint"]),
        scan_id=str(decision["scan_id"]),
        edit_plan_id=edit_plan_id,
        parent_start=parent_start,
        parent_end=parent_end,
        expected_after_sha256=expected_after_sha256,
    )


def collect_operations(
    text: str,
    decisions: list[dict[str, Any]],
    _anomalies_path: Path,
    _module: str,
) -> list[Operation]:
    validate_decision_set(decisions)
    operations: list[Operation] = []
    for decision in decisions:
        action = decision_action(decision)
        if not action:
            continue
        for anchor in decision_anchors(decision):
            result = build_operation(text, decision, anchor, action)
            if isinstance(result, str):
                raise ValueError(
                    f"anchor {anchor.get('anchor_id', '<missing>')} failed preflight: {result}"
                )
            operations.append(result)

    operations.sort(key=lambda op: op.start)
    filtered: list[Operation] = []
    last_end = -1
    for op in operations:
        if op.start < last_end:
            raise ValueError(
                f"overlapping anchor operation refused: {op.anchor_id} at {op.start}:{op.end}"
            )
        filtered.append(op)
        last_end = op.end
    return filtered


def apply_operations(text: str, operations: list[Operation]) -> str:
    updated = text
    for op in sorted(operations, key=lambda item: item.start, reverse=True):
        updated = updated[: op.start] + op.replacement + updated[op.end :]
    return updated


def log_operations(
    workspace: Path,
    operations_path: Path,
    module: str,
    input_path: Path,
    decisions_path: Path,
    output_path: Path,
    operations: list[Operation],
    *,
    run_id: str,
    input_sha256: str,
    decision_sha256: str,
    output_sha256: str,
) -> None:
    for op in operations:
        append_jsonl(
            operations_path,
            {
                "run_id": run_id,
                "module": module,
                "candidate_id": op.candidate_id,
                "candidate_fingerprint": op.candidate_fingerprint,
                "scan_id": op.scan_id,
                "anchor_id": op.anchor_id,
                "action": op.action,
                "strategy": op.strategy,
                "start": op.start,
                "end": op.end,
                "original": op.original,
                "replacement": op.replacement,
                "edit_plan_id": op.edit_plan_id,
                "parent_start": op.parent_start,
                "parent_end": op.parent_end,
                "expected_after_sha256": op.expected_after_sha256,
                "confidence": op.decision.get("confidence"),
                "reason": op.decision.get("reason"),
                "input": input_path.relative_to(workspace).as_posix(),
                "decisions": decisions_path.relative_to(workspace).as_posix(),
                "output": output_path.relative_to(workspace).as_posix(),
                "input_sha256": input_sha256,
                "decision_sha256": decision_sha256,
                "output_sha256": output_sha256,
            },
        )


def run(
    workspace: Path,
    module: str,
    input_value: str,
    decisions_value: str,
    output_value: str,
    stage: str,
) -> dict[str, Any]:
    expected_stage = MODULE_STAGES.get(module)
    if expected_stage is None:
        raise ValueError(f"unsupported apply module: {module}")
    if stage != expected_stage:
        raise ValueError(f"apply stage must be {expected_stage} for module {module}")
    with workspace_transaction_lock(workspace):
        return _run_locked(
            workspace,
            module,
            input_value,
            decisions_value,
            output_value,
            stage,
        )


def _run_locked(
    workspace: Path,
    module: str,
    input_value: str,
    decisions_value: str,
    output_value: str,
    stage: str,
) -> dict[str, Any]:
    expected_stage = MODULE_STAGES.get(module)
    if expected_stage is None:
        raise ValueError(f"unsupported apply module: {module}")
    if stage != expected_stage:
        raise ValueError(f"apply stage must be {expected_stage} for module {module}")
    workspace, reads, writes = resolve_workspace_paths(
        workspace,
        reads={"input": input_value, "decisions": decisions_value},
        writes={
            "output": output_value,
            "anomalies": "logs/anomalies.jsonl",
            "operations": "logs/operations.jsonl",
            "report": "report/apply_report.json",
        },
    )
    input_path = reads["input"]
    decisions_path = reads["decisions"]
    output_path = writes["output"]

    text = read_utf8(input_path)
    decisions = load_jsonl(decisions_path)
    manifest = load_manifest(workspace)
    provenance = validate_formal_ad_provenance(
        workspace,
        input_path,
        decisions_path,
        decisions,
        manifest=manifest,
        require_ready=True,
    )
    uncertain_ids = sorted(
        str(decision.get("candidate_id") or "<missing>")
        for decision in decisions
        if decision.get("verdict") == "uncertain"
    )
    if uncertain_ids:
        raise ValueError(
            "formal decisions contain uncertain candidates; resolve every blocker before apply: "
            + ", ".join(uncertain_ids)
        )
    operations = collect_operations(
        text,
        decisions,
        writes["anomalies"],
        module,
    )
    updated = apply_operations(text, operations)
    input_sha256 = sha256_file(input_path)
    decision_sha256 = sha256_file(decisions_path)
    expected_anchor_ids = sorted(
        anchor["anchor_id"]
        for decision in decisions
        if decision_action(decision) is not None
        for anchor in decision_anchors(decision)
    )
    with WorkspaceTransaction(workspace) as transaction:
        operations_path = transaction.stage_path(writes["operations"], copy_existing=True)
        staged_output = transaction.stage_path(output_path)
        write_utf8(staged_output, updated)
        output_sha256 = sha256_file(staged_output)
        log_operations(
            workspace,
            operations_path,
            module,
            input_path,
            decisions_path,
            output_path,
            operations,
            run_id=transaction.run_id,
            input_sha256=input_sha256,
            decision_sha256=decision_sha256,
            output_sha256=output_sha256,
        )
        transaction.discard_unwritten_stage(writes["operations"])
        summary = {
            "module": module,
            "input": input_path.relative_to(workspace).as_posix(),
            "decisions": decisions_path.relative_to(workspace).as_posix(),
            "output": output_path.relative_to(workspace).as_posix(),
            **provenance,
            "decision_count": len(decisions),
            "operation_count": len(operations),
            "candidate_fingerprints": sorted(
                str(decision["candidate_fingerprint"]) for decision in decisions
            ),
            "expected_anchor_ids": expected_anchor_ids,
            "operation_anchor_ids": sorted(operation.anchor_id for operation in operations),
            "active_run_id": transaction.run_id,
            "input_sha256": input_sha256,
            "decision_sha256": decision_sha256,
            "output_sha256": output_sha256,
            "report": "report/apply_report.json",
        }
        write_json(transaction.stage_path(writes["report"]), summary)
        updates: dict[str, tuple[str, dict[str, Any]]] = {stage: ("done", summary)}
        transaction.commit(updates)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply JSONL decisions with exact anchor checks.")
    parser.add_argument("--workspace", required=True, help="Path to the .cleanwork directory.")
    parser.add_argument("--module", required=True, help="Logical module name, such as ads.")
    parser.add_argument("--input", required=True, help="Input version path relative to workspace.")
    parser.add_argument("--decisions", required=True, help="Decision JSONL path relative to workspace.")
    parser.add_argument("--output", required=True, help="Output version path relative to workspace.")
    parser.add_argument("--stage", help="Must match the fixed stage for the selected module.")
    args = parser.parse_args()

    summary = run(
        workspace=Path(args.workspace).resolve(),
        module=args.module,
        input_value=args.input,
        decisions_value=args.decisions,
        output_value=args.output,
        stage=args.stage or MODULE_STAGES.get(args.module, ""),
    )
    print(summary)


if __name__ == "__main__":
    main()
