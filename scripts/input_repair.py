from __future__ import annotations

import argparse
import codecs
import hashlib
import json
from pathlib import Path
from typing import Any

from ad_rules import (
    has_bound_visit_locator,
    is_narrative_external_reference,
    promotion_intents,
    site_entities,
)
from common import (
    WorkspaceTransaction,
    load_manifest,
    resolve_workspace_paths,
    sha256_file,
    stage_invalidation_targets,
    workspace_transaction_lock,
    write_bytes,
    write_json,
)
import preprocess


REPAIR_SCHEMA = "cml.input-repair-candidates.v1"
PLAN_SCHEMA = "cml.input-repair-plan.v1"
REPORT_SCHEMA = "cml.input-repair-report.v1"
PRIMARY_ENCODINGS = ("utf-8", "gb18030", "big5", "ascii")
DEFAULT_CANDIDATE_ENCODINGS = ("utf-8", "gb18030", "big5")
PREVIEW_CODEPOINTS = 240
CONFIRMATION = "DROP_FULL_PHYSICAL_LINE"
LIMITATION = (
    "Some foreign-encoding bytes are legal under the primary decoder and may not "
    "be detected; inspection is evidence, not a complete mixed-encoding detector."
)


def _canonical_encoding(value: str) -> str:
    try:
        canonical = codecs.lookup(value).name
    except (LookupError, TypeError) as exc:
        raise ValueError(f"unsupported repair encoding: {value}") from exc
    aliases = {"utf-8": "utf-8", "iso8859-1": "latin-1"}
    canonical = aliases.get(canonical, canonical)
    if canonical not in PRIMARY_ENCODINGS:
        raise ValueError(
            "input repair supports only byte-line-safe primary encodings: "
            + ", ".join(PRIMARY_ENCODINGS)
        )
    return canonical


def _physical_lines(raw: bytes) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    start = 0
    index = 0
    line_number = 1
    while index < len(raw):
        byte = raw[index]
        if byte not in {10, 13}:
            index += 1
            continue
        content_end = index
        if byte == 13 and index + 1 < len(raw) and raw[index + 1] == 10:
            end = index + 2
            newline = b"\r\n"
        else:
            end = index + 1
            newline = raw[index:end]
        lines.append(
            {
                "line_number": line_number,
                "byte_start": start,
                "content_end": content_end,
                "byte_end": end,
                "newline_hex": newline.hex(),
            }
        )
        line_number += 1
        start = end
        index = end
    if start < len(raw) or not lines:
        lines.append(
            {
                "line_number": line_number,
                "byte_start": start,
                "content_end": len(raw),
                "byte_end": len(raw),
                "newline_hex": "",
            }
        )
    return lines


def _decode_candidate(raw: bytes, encoding: str) -> dict[str, Any]:
    try:
        text = raw.decode(encoding, errors="strict")
    except (UnicodeDecodeError, UnicodeError):
        return {
            "encoding": encoding,
            "strict_decode": False,
            "score": None,
            "preview": "",
            "text_sha256": None,
        }
    score, metrics = preprocess.text_quality(text)
    healthy = bool(
        score >= preprocess.MIN_AUTO_SCORE
        and metrics["replacement_char_count"] == 0
        and metrics["control_char_count"] == 0
        and metrics["invalid_unicode_count"] == 0
        and metrics["non_whitespace_char_count"] > 0
        and metrics["text_char_count"] > 0
    )
    return {
        "encoding": encoding,
        "strict_decode": True,
        "healthy": healthy,
        "score": score,
        "preview": text[:PREVIEW_CODEPOINTS],
        "preview_truncated": len(text) > PREVIEW_CODEPOINTS,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


def _drop_allowed(text: str) -> tuple[bool, list[str]]:
    intents = sorted(promotion_intents(text))
    entities = sorted(site_entities(text))
    bound_locator = has_bound_visit_locator(text)
    narrative = is_narrative_external_reference(text)
    blockers: list[str] = []
    if not intents:
        blockers.append("no_promotion_intent")
    if not entities and not bound_locator:
        blockers.append("no_external_locator")
    if narrative:
        blockers.append("narrative_context")
    if len(text) > 500:
        blockers.append("line_too_long")
    return not blockers, blockers


def build_candidates(
    raw: bytes,
    primary_encoding: str,
    candidate_encodings: tuple[str, ...],
) -> dict[str, Any]:
    primary = _canonical_encoding(primary_encoding)
    alternatives = tuple(
        dict.fromkeys(_canonical_encoding(value) for value in candidate_encodings)
    )
    alternatives = tuple(value for value in alternatives if value != primary)
    candidates: list[dict[str, Any]] = []
    for line in _physical_lines(raw):
        start = int(line["byte_start"])
        content_end = int(line["content_end"])
        end = int(line["byte_end"])
        content = raw[start:content_end]
        if not content:
            continue
        primary_result = _decode_candidate(content, primary)
        alternate_results = [_decode_candidate(content, value) for value in alternatives]
        healthy_alternatives = [
            item for item in alternate_results if item.get("healthy") is True
        ]
        healthy_alternatives.sort(
            key=lambda item: (-float(item["score"]), str(item["encoding"]))
        )
        primary_failed = primary_result["strict_decode"] is False
        primary_score = primary_result.get("score")
        suspicious_quality = bool(
            healthy_alternatives
            and isinstance(primary_score, (int, float))
            and float(healthy_alternatives[0]["score"]) - float(primary_score) >= 20.0
        )
        if not primary_failed and not suspicious_quality:
            continue

        selected = healthy_alternatives[0] if healthy_alternatives else None
        decoded_text = str(selected.get("text")) if selected is not None else ""
        drop_allowed, drop_blockers = (
            _drop_allowed(decoded_text)
            if selected is not None
            else (False, ["no_healthy_alternate_decode"])
        )
        line_bytes = raw[start:end]
        identity = hashlib.sha256(
            f"{start}:{end}:".encode("ascii") + line_bytes
        ).hexdigest()
        public_alternates = [
            {key: value for key, value in item.items() if key != "text"}
            for item in alternate_results
        ]
        candidates.append(
            {
                "candidate_id": f"IR-{identity[:16]}",
                "line_number": line["line_number"],
                "byte_start": start,
                "content_end": content_end,
                "byte_end": end,
                "newline_hex": line["newline_hex"],
                "line_sha256": hashlib.sha256(line_bytes).hexdigest(),
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "primary": {
                    key: value
                    for key, value in primary_result.items()
                    if key != "text"
                },
                "anomaly_type": (
                    "primary_decode_failed"
                    if primary_failed
                    else "alternate_quality_advantage"
                ),
                "alternatives": public_alternates,
                "selected_encoding": (
                    selected.get("encoding") if selected is not None else None
                ),
                "decoded_text_sha256": (
                    selected.get("text_sha256") if selected is not None else None
                ),
                "drop_full_physical_line_allowed": drop_allowed,
                "drop_blockers": drop_blockers,
            }
        )
    return {
        "schema": REPAIR_SCHEMA,
        "source": {
            "path": "versions/v0_original.txt",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "primary_encoding": primary,
        "candidate_encodings": list(alternatives),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "limitations": [LIMITATION],
    }


def _pending_updates(extra: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    updates: dict[str, tuple[str, dict[str, Any]]] = {
        "0_preprocess": ("blocked", extra)
    }
    for stage in stage_invalidation_targets("0_preprocess"):
        updates[stage] = ("pending", {"invalidated_by": "0_preprocess"})
    return updates


def inspect_workspace(
    workspace: Path,
    primary_encoding: str,
    candidate_encodings: tuple[str, ...] = DEFAULT_CANDIDATE_ENCODINGS,
) -> dict[str, Any]:
    with workspace_transaction_lock(workspace):
        workspace, reads, writes = resolve_workspace_paths(
            workspace,
            reads={
                "source": "versions/v0_original.txt",
                "preprocess_report": "report/preprocess_report.json",
            },
            writes={
                "candidates": "input_repair/repair_candidates.json",
                "report": "report/input_repair_report.json",
            },
        )
        raw = reads["source"].read_bytes()
        _validate_repair_entry(
            workspace,
            reads["preprocess_report"],
            raw,
            allowed_reasons=None,
        )
        candidates = build_candidates(raw, primary_encoding, candidate_encodings)
        report = {
            "schema": REPORT_SCHEMA,
            "status": "inspection_ready",
            "source": candidates["source"],
            "primary_encoding": candidates["primary_encoding"],
            "candidate_count": candidates["candidate_count"],
            "drop_allowed_count": sum(
                item["drop_full_physical_line_allowed"]
                for item in candidates["candidates"]
            ),
            "candidates": "input_repair/repair_candidates.json",
            "plan": "input_repair/repair_plan.json",
            "limitations": [LIMITATION],
        }
        with WorkspaceTransaction(workspace) as transaction:
            write_json(transaction.stage_path(writes["candidates"]), candidates)
            write_json(transaction.stage_path(writes["report"]), report)
            transaction.commit(
                _pending_updates(
                    {
                        "input": "versions/v0_original.txt",
                        "report": "report/input_repair_report.json",
                        "repair_candidates": "input_repair/repair_candidates.json",
                        "blocked_reason": "input_repair_inspection_ready",
                        "_current_head": "versions/v0_original.txt",
                    }
                )
            )
        return report


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _validate_repair_entry(
    workspace: Path,
    preprocess_report_path: Path,
    raw: bytes,
    *,
    allowed_reasons: set[str] | None,
) -> None:
    manifest = load_manifest(workspace)
    stage = manifest.get("stages", {}).get("0_preprocess", {})
    if (
        not isinstance(stage, dict)
        or stage.get("status") != "blocked"
        or stage.get("input") != "versions/v0_original.txt"
        or manifest.get("current_head") != "versions/v0_original.txt"
    ):
        raise ValueError("input repair requires a blocked original-input preprocess stage")
    if allowed_reasons is not None and stage.get("blocked_reason") not in allowed_reasons:
        raise ValueError("input repair stage is not ready for this operation")
    report = _load_json_object(preprocess_report_path, "preprocess report")
    source = report.get("source_identity")
    preprocess_input = report.get("preprocess_input")
    detection = report.get("encoding_detection")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        not isinstance(source, dict)
        or source.get("path") != "versions/v0_original.txt"
        or source.get("size_bytes") != len(raw)
        or source.get("sha256") != source_sha256
        or not isinstance(preprocess_input, dict)
        or preprocess_input.get("path") != "versions/v0_original.txt"
        or preprocess_input.get("size_bytes") != len(raw)
        or preprocess_input.get("sha256") != source_sha256
        or preprocess_input.get("prepared") is not False
        or not isinstance(detection, dict)
        or detection.get("blocked") is not True
    ):
        raise ValueError("blocked preprocess evidence does not match the immutable input")


def _validate_plan(
    plan: dict[str, Any],
    candidates: dict[str, Any],
    raw: bytes,
    candidates_sha256: str,
) -> list[tuple[int, int, dict[str, Any]]]:
    allowed_root = {
        "schema",
        "source_sha256",
        "candidates_report_sha256",
        "primary_encoding",
        "actions",
    }
    if set(plan) != allowed_root or plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("repair plan schema or fields are invalid")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if plan.get("source_sha256") != source_sha256:
        raise ValueError("repair plan source_sha256 is stale")
    if plan.get("candidates_report_sha256") != candidates_sha256:
        raise ValueError("repair plan candidates_report_sha256 is stale")
    if plan.get("primary_encoding") != candidates.get("primary_encoding"):
        raise ValueError("repair plan primary_encoding is stale")
    actions = plan.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("repair plan actions must be a non-empty array")
    by_id = {
        item.get("candidate_id"): item
        for item in candidates.get("candidates", [])
        if isinstance(item, dict) and isinstance(item.get("candidate_id"), str)
    }
    normalized: list[tuple[int, int, dict[str, Any]]] = []
    seen: set[str] = set()
    allowed_action = {
        "candidate_id",
        "action",
        "byte_start",
        "byte_end",
        "newline_hex",
        "line_sha256",
        "decoded_text_sha256",
        "decoded_encoding",
        "user_confirmed",
        "confirmation",
    }
    for action in actions:
        if not isinstance(action, dict) or set(action) != allowed_action:
            raise ValueError("repair plan action fields are invalid")
        candidate_id = action.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in seen:
            raise ValueError("repair plan candidate IDs must be unique")
        seen.add(candidate_id)
        candidate = by_id.get(candidate_id)
        if candidate is None:
            raise ValueError(f"repair plan has unknown candidate: {candidate_id}")
        if candidate.get("drop_full_physical_line_allowed") is not True:
            raise ValueError(f"repair candidate is not safe for full-line deletion: {candidate_id}")
        expected = {
            "action": "drop_full_physical_line",
            "byte_start": candidate.get("byte_start"),
            "byte_end": candidate.get("byte_end"),
            "newline_hex": candidate.get("newline_hex"),
            "line_sha256": candidate.get("line_sha256"),
            "decoded_text_sha256": candidate.get("decoded_text_sha256"),
            "decoded_encoding": candidate.get("selected_encoding"),
            "user_confirmed": True,
            "confirmation": CONFIRMATION,
        }
        for field, value in expected.items():
            if action.get(field) != value:
                raise ValueError(
                    f"repair plan action has stale or invalid {field}: {candidate_id}"
                )
        start = action["byte_start"]
        end = action["byte_end"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 0 <= start < end <= len(raw)
            or hashlib.sha256(raw[start:end]).hexdigest() != action["line_sha256"]
        ):
            raise ValueError(f"repair plan byte range is invalid: {candidate_id}")
        normalized.append((start, end, candidate))
    normalized.sort(key=lambda item: item[0])
    if any(left[1] > right[0] for left, right in zip(normalized, normalized[1:])):
        raise ValueError("repair plan byte ranges overlap")
    return normalized


def apply_plan(
    workspace: Path,
    plan_value: str = "input_repair/repair_plan.json",
) -> dict[str, Any]:
    with workspace_transaction_lock(workspace):
        workspace, reads, writes = resolve_workspace_paths(
            workspace,
            reads={
                "source": "versions/v0_original.txt",
                "preprocess_report": "report/preprocess_report.json",
                "candidates": "input_repair/repair_candidates.json",
                "plan": plan_value,
            },
            writes={
                "prepared": "versions/v0_prepared_input.txt",
                "report": "report/input_repair_report.json",
            },
        )
        raw = reads["source"].read_bytes()
        _validate_repair_entry(
            workspace,
            reads["preprocess_report"],
            raw,
            allowed_reasons={"input_repair_inspection_ready"},
        )
        candidates = _load_json_object(reads["candidates"], "repair candidates")
        primary_encoding = candidates.get("primary_encoding")
        candidate_encodings = candidates.get("candidate_encodings")
        if (
            candidates.get("schema") != REPAIR_SCHEMA
            or not isinstance(primary_encoding, str)
            or not isinstance(candidate_encodings, list)
            or not all(isinstance(value, str) for value in candidate_encodings)
        ):
            raise ValueError("repair candidates schema is invalid")
        rebuilt_candidates = build_candidates(
            raw,
            primary_encoding,
            tuple(candidate_encodings),
        )
        if candidates != rebuilt_candidates:
            raise ValueError("repair candidates do not match a fresh inspection")
        plan = _load_json_object(reads["plan"], "repair plan")
        ranges = _validate_plan(
            plan,
            candidates,
            raw,
            sha256_file(reads["candidates"]),
        )
        chunks: list[bytes] = []
        cursor = 0
        for start, end, _candidate in ranges:
            chunks.append(raw[cursor:start])
            cursor = end
        chunks.append(raw[cursor:])
        prepared = b"".join(chunks)
        if not prepared:
            raise ValueError("repair plan would remove the complete source")
        decoded, detection = preprocess.detect_and_decode(
            prepared,
            str(candidates["primary_encoding"]),
        )
        if decoded is None:
            raise ValueError(
                "prepared input is still blocked: "
                + str(detection.get("blocked_reason"))
            )
        prepared_identity = {
            "path": "versions/v0_prepared_input.txt",
            "size_bytes": len(prepared),
            "sha256": hashlib.sha256(prepared).hexdigest(),
        }
        report = {
            "schema": REPORT_SCHEMA,
            "status": "prepared",
            "source": candidates["source"],
            "prepared_input": prepared_identity,
            "primary_encoding": candidates["primary_encoding"],
            "plan": reads["plan"].relative_to(workspace).as_posix(),
            "plan_sha256": sha256_file(reads["plan"]),
            "candidate_report_sha256": sha256_file(reads["candidates"]),
            "action_count": len(ranges),
            "removed_bytes": sum(end - start for start, end, _ in ranges),
            "removed_candidates": [item[2]["candidate_id"] for item in ranges],
            "strict_decode": detection,
            "limitations": [LIMITATION],
            "next_action": "rerun preprocess with --use-prepared-input",
        }
        with WorkspaceTransaction(workspace) as transaction:
            write_bytes(transaction.stage_path(writes["prepared"]), prepared)
            write_json(transaction.stage_path(writes["report"]), report)
            transaction.commit(
                _pending_updates(
                    {
                        "input": "versions/v0_original.txt",
                        "output": "versions/v0_prepared_input.txt",
                        "report": "report/input_repair_report.json",
                        "repair_candidates": "input_repair/repair_candidates.json",
                        "repair_plan": reads["plan"].relative_to(workspace).as_posix(),
                        "blocked_reason": "prepared_input_requires_preprocess",
                    }
                )
            )
        return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect and apply an explicit byte-preserving input repair plan."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("workspace")
    inspect_parser.add_argument("--primary-encoding", required=True)
    inspect_parser.add_argument(
        "--candidate-encoding",
        action="append",
        dest="candidate_encodings",
        choices=PRIMARY_ENCODINGS,
    )
    apply_parser = subparsers.add_parser("apply-plan")
    apply_parser.add_argument("workspace")
    apply_parser.add_argument(
        "--plan",
        default="input_repair/repair_plan.json",
    )
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    if args.command == "inspect":
        report = inspect_workspace(
            workspace,
            args.primary_encoding,
            tuple(args.candidate_encodings or DEFAULT_CANDIDATE_ENCODINGS),
        )
    else:
        report = apply_plan(workspace, args.plan)
    print(
        json.dumps(
            {
                "status": report["status"],
                "candidate_count": report.get("candidate_count"),
                "action_count": report.get("action_count"),
                "next_action": report.get("next_action"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
