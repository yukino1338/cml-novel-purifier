from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence


PREREGISTRATION_SCHEMA = "cml.forward-preregistration.v1"
RESULTS_SCHEMA = "cml.forward-inference-evidence.v1"
CONTRACT_SCHEMA = "cml.framed-path-contract.v1"
PACKAGE_SOURCES_SCHEMA = "cml.forward-package-sources.v1"
PACKAGE_MANIFEST_SCHEMA = "cml.forward-package-manifest.v1"
AGENT_PACKAGE_SCHEMA = "cml.forward-agent-package.v1"
CONTRACT_FRAMING = (
    "sorted POSIX relative path; 8-byte big-endian path length; UTF-8 path; "
    "8-byte big-endian payload length; original payload bytes"
)
CONTRACT_NAMES = ("runtime", "guidance", "schema")
REQUIRED_STRATA = {
    "encoding-block",
    "encoding-repair",
    "external-ad",
    "formal-keep",
    "large-review",
    "mixed-segment",
    "narrative-negative",
    "publisher-terminal",
    "web-ui",
    "zero-candidate",
}
REQUIRED_WEB_OPERATIONS = {
    "batch-verdict",
    "export-request",
    "mixed-note",
    "refresh-restore",
    "single-verdict",
}
FAILURE_ATTRIBUTIONS = (
    "protocol-contamination",
    "fixture-or-gold",
    "host-or-tooling",
    "product-runtime",
    "guidance-ambiguity",
    "agent-judgment",
)
HOST_LANES = {"codex-required", "opencode-conditional"}
PRIVACY_CLASSES = {"public-anonymous", "private-local-aggregate-only"}
SLOT_STATUSES = {"pending", "completed", "protocol-invalid", "host-unavailable"}
OUTCOMES = {"success", "expected-product-stop", "failure", "unexpected-product-stop"}
SUCCESS_OUTCOMES = {"success", "expected-product-stop"}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
TASK_ID_RE = re.compile(r"FT-[0-9]{3}")


class EvidenceContractError(ValueError):
    """Raised when public forward evidence violates its frozen protocol."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                EvidenceContractError(f"non-finite JSON number: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceContractError(f"cannot read strict JSON: {path}") from error
    if not isinstance(value, dict):
        raise EvidenceContractError(f"JSON root must be an object: {path}")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise EvidenceContractError(
            f"{label} keys are invalid; missing={missing}, unknown={unknown}"
        )


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceContractError(f"{label} must be a non-empty string")
    return value


def _string_list(
    value: Any,
    label: str,
    *,
    non_empty: bool = True,
) -> list[str]:
    if not isinstance(value, list) or (non_empty and not value):
        raise EvidenceContractError(f"{label} must be a string list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise EvidenceContractError(f"{label} contains a non-string or empty item")
    if len(value) != len(set(value)):
        raise EvidenceContractError(f"{label} contains duplicates")
    return value


def _non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceContractError(f"{label} must be a non-negative integer")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise EvidenceContractError(f"{label} must be a lowercase SHA-256")
    return value


def _rfc3339(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceContractError(f"{label} must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise EvidenceContractError(
            f"{label} must be an RFC 3339 UTC timestamp"
        ) from error
    return parsed


def _rooted_file(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative or relative.startswith("/"):
        raise EvidenceContractError(f"contract path is not portable: {relative!r}")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise EvidenceContractError(f"contract path escapes root: {relative}") from error
    return path


def collect_scope_files(root: Path, scope: Sequence[str]) -> list[Path]:
    root = root.resolve()
    files: set[Path] = set()
    for entry in scope:
        _non_empty_string(entry, "contract scope entry")
        if entry.endswith("/**"):
            directory = _rooted_file(root, entry[:-3])
            if not directory.is_dir():
                raise EvidenceContractError(f"contract directory is missing: {entry}")
            matched = {
                path.resolve()
                for path in directory.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix.casefold() not in {".pyc", ".pyo"}
            }
            if not matched:
                raise EvidenceContractError(f"contract scope is empty: {entry}")
            files.update(matched)
        elif "*" in entry or "?" in entry or "[" in entry:
            raise EvidenceContractError(
                f"only exact files and directory/** scopes are supported: {entry}"
            )
        else:
            path = _rooted_file(root, entry)
            if not path.is_file():
                raise EvidenceContractError(f"contract file is missing: {entry}")
            files.add(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def framed_contract(root: Path, scope: Sequence[str]) -> dict[str, Any]:
    root = root.resolve()
    scope_list = list(scope)
    _string_list(scope_list, "contract scope")
    files = collect_scope_files(root, scope_list)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return {
        "schema": CONTRACT_SCHEMA,
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "scope": scope_list,
        "framing": CONTRACT_FRAMING,
    }


def _validate_contract(value: Any, expected_scope: Sequence[str], label: str) -> None:
    if not isinstance(value, dict):
        raise EvidenceContractError(f"{label} must be an object")
    _exact_keys(
        value,
        {"schema", "sha256", "file_count", "scope", "framing"},
        label,
    )
    if value["schema"] != CONTRACT_SCHEMA or value["framing"] != CONTRACT_FRAMING:
        raise EvidenceContractError(f"{label} framing contract is unsupported")
    _sha256(value["sha256"], f"{label}.sha256")
    _non_negative_int(value["file_count"], f"{label}.file_count")
    if value["file_count"] == 0:
        raise EvidenceContractError(f"{label}.file_count must be positive")
    if value["scope"] != list(expected_scope):
        raise EvidenceContractError(f"{label}.scope does not match preregistration")


def validate_preregistration(value: Mapping[str, Any]) -> dict[str, Any]:
    _exact_keys(
        value,
        {
            "schema",
            "study_id",
            "state",
            "evidence_tracks",
            "contract_scopes",
            "isolation",
            "agent_visible_inputs",
            "gold_reveal",
            "failure_attribution",
            "stopping_conditions",
            "statistics",
            "tasks",
        },
        "preregistration",
    )
    if value["schema"] != PREREGISTRATION_SCHEMA:
        raise EvidenceContractError("unsupported preregistration schema")
    _non_empty_string(value["study_id"], "study_id")
    if value["state"] != "registered-awaiting-fresh-agent":
        raise EvidenceContractError("preregistration state must remain pre-inference")

    tracks = value["evidence_tracks"]
    if not isinstance(tracks, dict):
        raise EvidenceContractError("evidence_tracks must be an object")
    _exact_keys(tracks, {"deterministic_replay", "fresh_agent_inference"}, "tracks")
    replay = tracks["deterministic_replay"]
    inference = tracks["fresh_agent_inference"]
    for track, label in ((replay, "deterministic replay"), (inference, "inference")):
        if not isinstance(track, dict):
            raise EvidenceContractError(f"{label} track must be an object")
        _exact_keys(track, {"evidence_class", "supports", "does_not_support"}, label)
        _non_empty_string(track["evidence_class"], f"{label}.evidence_class")
        _string_list(track["supports"], f"{label}.supports")
        _string_list(track["does_not_support"], f"{label}.does_not_support")
    if replay["evidence_class"] != "deterministic-replay":
        raise EvidenceContractError("script replay must be labeled deterministic-replay")
    if inference["evidence_class"] != "fresh-agent-inference":
        raise EvidenceContractError("Agent trials must be labeled fresh-agent-inference")
    if "agent-inference-quality" not in replay["does_not_support"]:
        raise EvidenceContractError("replay must disclaim Agent inference quality")
    if "cross-work-generalization" not in inference["does_not_support"]:
        raise EvidenceContractError("inference track must disclaim cross-work generalization")

    scopes = value["contract_scopes"]
    if not isinstance(scopes, dict):
        raise EvidenceContractError("contract_scopes must be an object")
    _exact_keys(scopes, set(CONTRACT_NAMES), "contract_scopes")
    for name in CONTRACT_NAMES:
        entries = _string_list(scopes[name], f"contract_scopes.{name}")
        for entry in entries:
            if "\\" in entry or entry.startswith("/") or ".." in Path(entry).parts:
                raise EvidenceContractError(f"non-portable contract scope: {entry}")

    isolation = value["isolation"]
    if not isinstance(isolation, dict):
        raise EvidenceContractError("isolation must be an object")
    _exact_keys(
        isolation,
        {
            "one_fresh_context_per_task",
            "one_task_per_context",
            "cross_task_context_reuse",
            "historical_workspace_visible",
            "tests_visible",
            "gold_visible",
            "memory_carryover",
            "public_artifact_retention",
            "private_artifact_retention",
        },
        "isolation",
    )
    expected_flags = {
        "one_fresh_context_per_task": True,
        "one_task_per_context": True,
        "cross_task_context_reuse": False,
        "historical_workspace_visible": False,
        "tests_visible": False,
        "gold_visible": False,
        "memory_carryover": False,
    }
    for field, expected in expected_flags.items():
        if isolation[field] is not expected:
            raise EvidenceContractError(f"isolation.{field} must be {expected}")
    if isolation["public_artifact_retention"] != "anonymous-input-prompt-review-and-receipt":
        raise EvidenceContractError("public retention contract is invalid")
    if isolation["private_artifact_retention"] != "local-only-anonymous-aggregate":
        raise EvidenceContractError("private retention contract is invalid")

    visible = value["agent_visible_inputs"]
    if not isinstance(visible, dict):
        raise EvidenceContractError("agent_visible_inputs must be an object")
    _exact_keys(visible, {"allowed", "forbidden"}, "agent_visible_inputs")
    _string_list(visible["allowed"], "agent_visible_inputs.allowed")
    forbidden = _string_list(visible["forbidden"], "agent_visible_inputs.forbidden")
    for required in ("tests-and-gold", "prior-task-transcripts", "expected-verdicts"):
        if required not in forbidden:
            raise EvidenceContractError(f"Agent forbidden inputs omit {required}")

    reveal = value["gold_reveal"]
    if not isinstance(reveal, dict):
        raise EvidenceContractError("gold_reveal must be an object")
    _exact_keys(
        reveal,
        {"freeze_before_agent_start", "reveal_after", "post_reveal_rerun", "role"},
        "gold_reveal",
    )
    if reveal["freeze_before_agent_start"] is not True:
        raise EvidenceContractError("gold must be frozen before Agent start")
    if reveal["reveal_after"] != ["terminal-receipt-frozen", "artifact-manifest-frozen"]:
        raise EvidenceContractError("gold reveal boundary is invalid")
    if reveal["post_reveal_rerun"] != "forbidden-for-inference-metrics":
        raise EvidenceContractError("post-reveal reruns must not enter inference metrics")
    if reveal["role"] != "evaluator-only":
        raise EvidenceContractError("gold must remain evaluator-only")

    attribution = value["failure_attribution"]
    if not isinstance(attribution, dict):
        raise EvidenceContractError("failure_attribution must be an object")
    _exact_keys(attribution, {"categories", "precedence", "frozen_before_trials"}, "failure_attribution")
    if attribution["categories"] != list(FAILURE_ATTRIBUTIONS):
        raise EvidenceContractError("failure attribution categories are not frozen")
    if attribution["precedence"] != list(FAILURE_ATTRIBUTIONS):
        raise EvidenceContractError("failure attribution precedence is not frozen")
    if attribution["frozen_before_trials"] is not True:
        raise EvidenceContractError("failure attribution must be frozen")

    stopping = value["stopping_conditions"]
    if not isinstance(stopping, dict):
        raise EvidenceContractError("stopping_conditions must be an object")
    _exact_keys(
        stopping,
        {
            "attempts_per_task_host",
            "infrastructure_retry",
            "task_terminal",
            "study_stop_only_for",
            "favorable_result_early_stop",
            "unrun_slots_after_stop",
        },
        "stopping_conditions",
    )
    if stopping["attempts_per_task_host"] != 1:
        raise EvidenceContractError("each task-host lane must have one scored attempt")
    if stopping["infrastructure_retry"] != "one-only-before-fixture-is-visible":
        raise EvidenceContractError("infrastructure retry rule is invalid")
    _string_list(stopping["task_terminal"], "stopping.task_terminal")
    _string_list(stopping["study_stop_only_for"], "stopping.study_stop_only_for")
    if stopping["favorable_result_early_stop"] is not False:
        raise EvidenceContractError("favorable-result early stopping is forbidden")
    if stopping["unrun_slots_after_stop"] != "published-as-unrun-with-reason":
        raise EvidenceContractError("unrun slots must remain visible")

    statistics = value["statistics"]
    if not isinstance(statistics, dict):
        raise EvidenceContractError("statistics must be an object")
    _exact_keys(
        statistics,
        {
            "confidence_level",
            "interval",
            "zero_denominator",
            "publish_exact_counts",
            "metrics",
            "pooling",
            "claim_limit",
        },
        "statistics",
    )
    if statistics["confidence_level"] != 0.95:
        raise EvidenceContractError("confidence level must be 0.95")
    if statistics["interval"] != "wilson-score-two-sided":
        raise EvidenceContractError("interval method must be Wilson score")
    if statistics["zero_denominator"] != "not-estimable-null-bounds":
        raise EvidenceContractError("zero-denominator rule is invalid")
    if statistics["publish_exact_counts"] is not True:
        raise EvidenceContractError("exact numerator and denominator are required")
    required_metrics = {
        "candidate-review-coverage",
        "delete-anchor-precision",
        "required-event-compliance",
        "supported-delete-anchor-recall",
        "task-success",
    }
    if set(_string_list(statistics["metrics"], "statistics.metrics")) != required_metrics:
        raise EvidenceContractError("statistics.metrics is incomplete")
    if statistics["pooling"] != "public-anonymous-only-with-host-breakdown":
        raise EvidenceContractError("public/private pooling contract is invalid")
    if statistics["claim_limit"] != "finite-corpus-point-estimate-no-cross-work-inference":
        raise EvidenceContractError("statistical claim limit is invalid")

    tasks = value["tasks"]
    if not isinstance(tasks, list) or len(tasks) < 12:
        raise EvidenceContractError("at least 12 preregistered tasks are required")
    task_ids: list[str] = []
    strata: set[str] = set()
    web_operations: set[str] = set()
    required_codex = 0
    conditional_opencode = 0
    has_large = False
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise EvidenceContractError(f"tasks[{index}] must be an object")
        _exact_keys(
            task,
            {
                "task_id",
                "stratum",
                "objective",
                "privacy",
                "host_lane",
                "fixture_kind",
                "scale_floor",
                "required_operations",
                "success_measure_keys",
            },
            f"tasks[{index}]",
        )
        task_id = _non_empty_string(task["task_id"], f"tasks[{index}].task_id")
        if TASK_ID_RE.fullmatch(task_id) is None:
            raise EvidenceContractError(f"invalid task id: {task_id}")
        task_ids.append(task_id)
        stratum = _non_empty_string(task["stratum"], f"{task_id}.stratum")
        strata.add(stratum)
        _non_empty_string(task["objective"], f"{task_id}.objective")
        if task["privacy"] not in PRIVACY_CLASSES:
            raise EvidenceContractError(f"{task_id}.privacy is invalid")
        host_lane = task["host_lane"]
        if host_lane not in HOST_LANES:
            raise EvidenceContractError(f"{task_id}.host_lane is invalid")
        required_codex += host_lane == "codex-required"
        conditional_opencode += host_lane == "opencode-conditional"
        _non_empty_string(task["fixture_kind"], f"{task_id}.fixture_kind")
        floor = task["scale_floor"]
        if not isinstance(floor, dict):
            raise EvidenceContractError(f"{task_id}.scale_floor must be an object")
        _exact_keys(floor, {"candidates", "anchors"}, f"{task_id}.scale_floor")
        candidates = _non_negative_int(floor["candidates"], f"{task_id}.candidates")
        anchors = _non_negative_int(floor["anchors"], f"{task_id}.anchors")
        if candidates >= 150 and anchors >= 700:
            has_large = True
        operations = _string_list(task["required_operations"], f"{task_id}.operations")
        if stratum == "web-ui":
            web_operations.update(operations)
        _string_list(task["success_measure_keys"], f"{task_id}.success_measure_keys")
    if len(task_ids) != len(set(task_ids)) or task_ids != sorted(task_ids):
        raise EvidenceContractError("task ids must be unique and sorted")
    if not REQUIRED_STRATA <= strata:
        raise EvidenceContractError(
            f"preregistered strata are incomplete: {sorted(REQUIRED_STRATA - strata)}"
        )
    if not REQUIRED_WEB_OPERATIONS <= web_operations:
        raise EvidenceContractError(
            f"web operations are incomplete: {sorted(REQUIRED_WEB_OPERATIONS - web_operations)}"
        )
    if required_codex < 12:
        raise EvidenceContractError("at least 12 tasks must remain executable on Codex")
    if conditional_opencode < 1:
        raise EvidenceContractError("at least one conditional OpenCode lane is required")
    if not has_large:
        raise EvidenceContractError("a large task needs 150+ candidates and 700+ anchors")
    return dict(value)


def capture_bindings(root: Path, preregistration: Mapping[str, Any]) -> dict[str, Any]:
    validated = validate_preregistration(preregistration)
    scopes = validated["contract_scopes"]
    return {
        name: framed_contract(root, scopes[name])
        for name in CONTRACT_NAMES
    }


def wilson_metric(
    numerator: int,
    denominator: int,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    numerator = _non_negative_int(numerator, "metric numerator")
    denominator = _non_negative_int(denominator, "metric denominator")
    if numerator > denominator:
        raise EvidenceContractError("metric numerator exceeds denominator")
    if confidence_level != 0.95:
        raise EvidenceContractError("only the preregistered 95% interval is supported")
    if denominator == 0:
        return {
            "numerator": numerator,
            "denominator": denominator,
            "point_estimate": None,
            "confidence_level": confidence_level,
            "interval_method": "wilson-score-two-sided",
            "lower": None,
            "upper": None,
        }
    point = numerator / denominator
    z = NormalDist().inv_cdf(0.5 + confidence_level / 2)
    z_squared = z * z
    scale = 1 + z_squared / denominator
    center = (point + z_squared / (2 * denominator)) / scale
    spread = (
        z
        * math.sqrt(
            point * (1 - point) / denominator
            + z_squared / (4 * denominator * denominator)
        )
        / scale
    )
    return {
        "numerator": numerator,
        "denominator": denominator,
        "point_estimate": round(point, 12),
        "confidence_level": confidence_level,
        "interval_method": "wilson-score-two-sided",
        "lower": round(max(0.0, center - spread), 12),
        "upper": round(min(1.0, center + spread), 12),
    }


COUNT_KEYS = {
    "anchor_total",
    "candidate_total",
    "candidate_reviewed",
    "delete_anchor_gold",
    "delete_anchor_selected",
    "delete_anchor_correct",
    "required_events",
    "honored_events",
}


def _validate_counts(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise EvidenceContractError(f"{label} must be an object")
    _exact_keys(value, COUNT_KEYS, label)
    result = {key: _non_negative_int(value[key], f"{label}.{key}") for key in COUNT_KEYS}
    if result["candidate_reviewed"] > result["candidate_total"]:
        raise EvidenceContractError(f"{label}: reviewed candidates exceed total")
    if result["delete_anchor_correct"] > result["delete_anchor_selected"]:
        raise EvidenceContractError(f"{label}: correct anchors exceed selected")
    if result["delete_anchor_correct"] > result["delete_anchor_gold"]:
        raise EvidenceContractError(f"{label}: correct anchors exceed gold")
    if result["honored_events"] > result["required_events"]:
        raise EvidenceContractError(f"{label}: honored events exceed required")
    return result


def _metrics_for_slots(slots: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    completed = [slot for slot in slots if slot["status"] == "completed"]
    counts = [slot["counts"] for slot in completed]
    successes = sum(slot["outcome"] in SUCCESS_OUTCOMES for slot in completed)
    return {
        "task-success": wilson_metric(successes, len(completed)),
        "candidate-review-coverage": wilson_metric(
            sum(item["candidate_reviewed"] for item in counts),
            sum(item["candidate_total"] for item in counts),
        ),
        "delete-anchor-precision": wilson_metric(
            sum(item["delete_anchor_correct"] for item in counts),
            sum(item["delete_anchor_selected"] for item in counts),
        ),
        "supported-delete-anchor-recall": wilson_metric(
            sum(item["delete_anchor_correct"] for item in counts),
            sum(item["delete_anchor_gold"] for item in counts),
        ),
        "required-event-compliance": wilson_metric(
            sum(item["honored_events"] for item in counts),
            sum(item["required_events"] for item in counts),
        ),
    }


def aggregate_results(
    preregistration: Mapping[str, Any],
    slots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    task_map = {task["task_id"]: task for task in preregistration["tasks"]}
    public = [
        slot
        for slot in slots
        if task_map[slot["task_id"]]["privacy"] == "public-anonymous"
    ]
    private = [
        slot
        for slot in slots
        if task_map[slot["task_id"]]["privacy"] == "private-local-aggregate-only"
    ]
    public_completed = [slot for slot in public if slot["status"] == "completed"]
    by_host = {
        host: {
            "task_count": len(host_slots),
            "task_success": _metrics_for_slots(host_slots)["task-success"],
        }
        for host in sorted(
            {slot["host"] for slot in public_completed if isinstance(slot["host"], str)}
        )
        if (
            host_slots := [slot for slot in public_completed if slot["host"] == host]
        )
    }
    by_stratum = {
        stratum: {
            "task_count": len(stratum_slots),
            "task_success": _metrics_for_slots(stratum_slots)["task-success"],
        }
        for stratum in sorted(
            {
                task_map[slot["task_id"]]["stratum"]
                for slot in public_completed
            }
        )
        if (
            stratum_slots := [
                slot
                for slot in public_completed
                if task_map[slot["task_id"]]["stratum"] == stratum
            ]
        )
    }
    private_completed = [slot for slot in private if slot["status"] == "completed"]
    return {
        "public_anonymous": {
            "included_in_release_metrics": True,
            "task_count": len(public_completed),
            "by_host": by_host,
            "by_stratum": by_stratum,
            "metrics": _metrics_for_slots(public_completed),
        },
        "private_self_attested": {
            "included_in_release_metrics": False,
            "evidence_class": "supplemental-private-self-attested",
            "task_count": len(private_completed),
            "success_count": sum(
                slot["outcome"] in SUCCESS_OUTCOMES for slot in private_completed
            ),
        },
        "protocol_invalid_count": sum(
            slot["status"] == "protocol-invalid" for slot in slots
        ),
        "host_unavailable_count": sum(
            slot["status"] == "host-unavailable" for slot in slots
        ),
        "pending_count": sum(slot["status"] == "pending" for slot in slots),
    }


SLOT_KEYS = {
    "artifact_manifest_sha256",
    "counts_sha256",
    "task_id",
    "status",
    "host",
    "agent_context_sha256",
    "fixture_sha256",
    "gold_sha256",
    "started_at",
    "completed_at",
    "terminal_receipt_sha256",
    "outcome",
    "failure_attribution",
    "counts",
}


def validate_results(
    value: Mapping[str, Any],
    preregistration: Mapping[str, Any],
) -> dict[str, Any]:
    prereg = validate_preregistration(preregistration)
    _exact_keys(
        value,
        {
            "schema",
            "study_id",
            "state",
            "evidence_class",
            "publication_claim",
            "bindings",
            "host_coverage",
            "task_slots",
            "aggregate",
        },
        "results",
    )
    if value["schema"] != RESULTS_SCHEMA:
        raise EvidenceContractError("unsupported inference-results schema")
    if value["study_id"] != prereg["study_id"]:
        raise EvidenceContractError("results study_id does not match preregistration")
    if value["state"] not in {"pending", "collecting", "completed", "stopped"}:
        raise EvidenceContractError("results state is invalid")
    if value["evidence_class"] != "fresh-agent-inference":
        raise EvidenceContractError("results cannot be labeled as deterministic replay")
    _non_empty_string(value["publication_claim"], "publication_claim")

    coverage = value["host_coverage"]
    if not isinstance(coverage, dict):
        raise EvidenceContractError("host_coverage must be an object")
    _exact_keys(
        coverage,
        {"required", "conditional", "executed", "conditional_unavailable_reason"},
        "host_coverage",
    )
    if coverage["required"] != ["codex"] or coverage["conditional"] != ["opencode"]:
        raise EvidenceContractError("host coverage plan is invalid")
    executed = _string_list(coverage["executed"], "host_coverage.executed", non_empty=False)
    if not set(executed) <= {"codex", "opencode"}:
        raise EvidenceContractError("host_coverage.executed contains an unknown host")
    unavailable_reason = coverage["conditional_unavailable_reason"]
    if unavailable_reason is not None:
        _non_empty_string(unavailable_reason, "conditional_unavailable_reason")

    task_slots = value["task_slots"]
    if not isinstance(task_slots, list):
        raise EvidenceContractError("task_slots must be a list")
    expected_ids = [task["task_id"] for task in prereg["tasks"]]
    if [slot.get("task_id") for slot in task_slots if isinstance(slot, dict)] != expected_ids:
        raise EvidenceContractError("task_slots must exactly follow preregistered task order")
    task_map = {task["task_id"]: task for task in prereg["tasks"]}
    for index, slot in enumerate(task_slots):
        if not isinstance(slot, dict):
            raise EvidenceContractError(f"task_slots[{index}] must be an object")
        _exact_keys(slot, SLOT_KEYS, f"task_slots[{index}]")
        status = slot["status"]
        if status not in SLOT_STATUSES:
            raise EvidenceContractError(f"{slot['task_id']}.status is invalid")
        host_lane = task_map[slot["task_id"]]["host_lane"]
        if status == "pending":
            for field in SLOT_KEYS - {"task_id", "status"}:
                if slot[field] is not None:
                    raise EvidenceContractError(
                        f"pending slot {slot['task_id']} must leave {field} null"
                    )
            continue
        if status == "host-unavailable":
            if host_lane != "opencode-conditional":
                raise EvidenceContractError("required Codex slots cannot be host-unavailable")
            if slot["host"] != "opencode" or slot["failure_attribution"] != "host-or-tooling":
                raise EvidenceContractError("host-unavailable slot attribution is invalid")
            for field in (
                "agent_context_sha256",
                "artifact_manifest_sha256",
                "counts_sha256",
                "fixture_sha256",
                "gold_sha256",
                "started_at",
                "completed_at",
                "terminal_receipt_sha256",
                "outcome",
                "counts",
            ):
                if slot[field] is not None:
                    raise EvidenceContractError(f"host-unavailable {field} must be null")
            continue
        expected_host = "codex" if host_lane == "codex-required" else "opencode"
        if slot["host"] != expected_host:
            raise EvidenceContractError(f"{slot['task_id']} ran on the wrong host lane")
        for field in (
            "agent_context_sha256",
            "artifact_manifest_sha256",
            "counts_sha256",
            "fixture_sha256",
            "gold_sha256",
            "terminal_receipt_sha256",
        ):
            _sha256(slot[field], f"{slot['task_id']}.{field}")
        started = _rfc3339(slot["started_at"], f"{slot['task_id']}.started_at")
        completed = _rfc3339(slot["completed_at"], f"{slot['task_id']}.completed_at")
        if completed < started:
            raise EvidenceContractError(
                f"{slot['task_id']}.completed_at precedes started_at"
            )
        if status == "protocol-invalid":
            if slot["outcome"] is not None or slot["counts"] is not None:
                raise EvidenceContractError("protocol-invalid slots cannot enter outcomes")
            if slot["failure_attribution"] not in FAILURE_ATTRIBUTIONS[:3]:
                raise EvidenceContractError("protocol-invalid attribution is invalid")
            continue
        if slot["outcome"] not in OUTCOMES:
            raise EvidenceContractError(f"{slot['task_id']}.outcome is invalid")
        attribution_value = slot["failure_attribution"]
        if attribution_value is not None and attribution_value not in FAILURE_ATTRIBUTIONS:
            raise EvidenceContractError(f"{slot['task_id']}.failure attribution is invalid")
        if slot["outcome"] in SUCCESS_OUTCOMES and attribution_value is not None:
            raise EvidenceContractError("successful outcomes cannot have failure attribution")
        if slot["outcome"] not in SUCCESS_OUTCOMES and attribution_value is None:
            raise EvidenceContractError("failed outcomes require failure attribution")
        counts = _validate_counts(slot["counts"], f"{slot['task_id']}.counts")
        floor = task_map[slot["task_id"]]["scale_floor"]
        if (
            counts["candidate_total"] < floor["candidates"]
            or counts["anchor_total"] < floor["anchors"]
        ):
            raise EvidenceContractError(
                f"{slot['task_id']} did not meet its preregistered scale floor"
            )
        if task_map[slot["task_id"]]["stratum"] == "zero-candidate" and (
            counts["candidate_total"] != 0 or counts["anchor_total"] != 0
        ):
            raise EvidenceContractError("zero-candidate task did not remain exactly zero")

    if value["state"] == "pending":
        if value["bindings"] is not None or value["aggregate"] is not None:
            raise EvidenceContractError("pending results cannot bind or aggregate evidence")
        if any(slot["status"] != "pending" for slot in task_slots):
            raise EvidenceContractError("pending results contain non-pending slots")
        if executed:
            raise EvidenceContractError("pending results cannot claim an executed host")
        if value["publication_claim"] != "none-pending-fresh-agent-execution":
            raise EvidenceContractError("pending results must publish no inference claim")
        return dict(value)

    expected_claim = {
        "collecting": "none-collecting-preregistered-slots",
        "stopped": "none-stopped-incomplete-study",
        "completed": "finite-preregistered-task-point-estimates-only",
    }[value["state"]]
    if value["publication_claim"] != expected_claim:
        raise EvidenceContractError(
            f"{value['state']} results publication claim is invalid"
        )

    bindings = value["bindings"]
    if not isinstance(bindings, dict):
        raise EvidenceContractError("started evidence must bind execution contracts")
    _exact_keys(bindings, set(CONTRACT_NAMES), "bindings")
    for name in CONTRACT_NAMES:
        _validate_contract(bindings[name], prereg["contract_scopes"][name], f"bindings.{name}")
    expected_aggregate = aggregate_results(prereg, task_slots)
    if value["aggregate"] != expected_aggregate:
        raise EvidenceContractError("published aggregate does not match exact task slots")
    if value["state"] == "completed":
        for task, slot in zip(prereg["tasks"], task_slots, strict=True):
            allowed = {"completed"}
            if task["host_lane"] == "opencode-conditional":
                allowed.add("host-unavailable")
            if slot["status"] not in allowed:
                raise EvidenceContractError("completed study has unfinished required slots")
        if "codex" not in executed:
            raise EvidenceContractError("completed evidence lacks required Codex execution")
        if "opencode" not in executed and unavailable_reason is None:
            raise EvidenceContractError("missing OpenCode execution needs a published reason")
    return dict(value)


def assess_evidence(
    root: Path,
    preregistration: Mapping[str, Any],
    results: Mapping[str, Any],
) -> dict[str, Any]:
    prereg = validate_preregistration(preregistration)
    validated_results = validate_results(results, prereg)
    if validated_results["state"] == "pending":
        return {
            "status": "pending",
            "inference_claim_allowed": False,
            "deterministic_replay_is_agent_inference": False,
            "stale_contracts": [],
            "reasons": ["fresh Agent task slots have not been executed"],
        }
    current = capture_bindings(root, prereg)
    stale = [
        name
        for name in CONTRACT_NAMES
        if validated_results["bindings"][name] != current[name]
    ]
    if stale:
        return {
            "status": "stale",
            "inference_claim_allowed": False,
            "deterministic_replay_is_agent_inference": False,
            "stale_contracts": stale,
            "reasons": [f"{name} contract changed" for name in stale],
        }
    if validated_results["state"] != "completed":
        return {
            "status": "incomplete",
            "inference_claim_allowed": False,
            "deterministic_replay_is_agent_inference": False,
            "stale_contracts": [],
            "reasons": [f"study state is {validated_results['state']}"],
        }
    return {
        "status": "current",
        "inference_claim_allowed": True,
        "deterministic_replay_is_agent_inference": False,
        "stale_contracts": [],
        "reasons": [],
    }


def assess_legacy_summary(root: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Classify pre-v1 evidence without rewriting its historical hashes."""
    stale_contracts: list[str] = []
    reasons = ["legacy evidence lacks the preregistered guidance/schema contracts"]
    for name in ("runtime", "fixture", "replay"):
        key = f"{name}_contract"
        contract = summary.get(key)
        if not isinstance(contract, dict) or not isinstance(contract.get("scope"), list):
            stale_contracts.append(name)
            reasons.append(f"legacy {name} contract is missing or malformed")
            continue
        try:
            current = framed_contract(root, contract["scope"])
        except EvidenceContractError as error:
            stale_contracts.append(name)
            reasons.append(f"legacy {name} contract cannot be replayed: {error}")
            continue
        if (
            contract.get("sha256") != current["sha256"]
            or contract.get("file_count") != current["file_count"]
        ):
            stale_contracts.append(name)
            reasons.append(f"legacy {name} contract changed")
    return {
        "status": "stale",
        "evidence_class": "legacy-forward-evidence",
        "inference_claim_allowed": False,
        "deterministic_replay_is_agent_inference": False,
        "stale_contracts": stale_contracts,
        "reasons": reasons,
    }


def _framed_named_payloads(items: Iterable[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, payload in sorted(items):
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _directory_payload_sha256(root: Path, *, exclude_manifest: bool = False) -> str:
    root = root.resolve()
    items = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if exclude_manifest and relative == "PACKAGE_MANIFEST.json":
            continue
        items.append((relative, path.read_bytes()))
    if not items:
        raise EvidenceContractError(f"package directory is empty: {root}")
    return _framed_named_payloads(items)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise EvidenceContractError(f"temporary result path already exists: {temporary}")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _agent_skill_files(root: Path) -> list[Path]:
    root = root.resolve()
    files = [
        _rooted_file(root, "SKILL.md"),
        _rooted_file(root, "agents/openai.yaml"),
    ]
    for directory_name in ("assets", "scripts", "references"):
        directory = _rooted_file(root, directory_name)
        files.extend(
            path.resolve()
            for path in directory.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix.casefold() not in {".pyc", ".pyo"}
            and path.name != "forward-evidence-protocol.md"
            and path.name != "forward_evidence.py"
        )
    return sorted(set(files), key=lambda path: path.relative_to(root).as_posix())


def validate_package_sources(
    value: Mapping[str, Any],
    preregistration: Mapping[str, Any],
    selected_task_ids: Sequence[str],
) -> dict[str, Any]:
    _exact_keys(value, {"schema", "study_id", "tasks"}, "package sources")
    if value["schema"] != PACKAGE_SOURCES_SCHEMA:
        raise EvidenceContractError("unsupported package-sources schema")
    if value["study_id"] != preregistration["study_id"]:
        raise EvidenceContractError("package sources study_id is stale")
    tasks = value["tasks"]
    if not isinstance(tasks, list):
        raise EvidenceContractError("package source tasks must be a list")
    source_ids = [task.get("task_id") for task in tasks if isinstance(task, dict)]
    if source_ids != list(selected_task_ids):
        raise EvidenceContractError(
            "package source tasks must exactly match selected preregistered order"
        )
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise EvidenceContractError(f"package source task {index} is invalid")
        _exact_keys(
            task,
            {"task_id", "prompt_file", "visible_files", "gold_files"},
            f"package source {task.get('task_id')}",
        )
        _non_empty_string(task["prompt_file"], f"{task['task_id']}.prompt_file")
        visible_files = task["visible_files"]
        if not isinstance(visible_files, list) or not visible_files:
            raise EvidenceContractError(f"{task['task_id']}.visible_files is empty")
        destinations: list[str] = []
        for entry in visible_files:
            if not isinstance(entry, dict):
                raise EvidenceContractError("visible file mapping must be an object")
            _exact_keys(entry, {"source", "destination"}, "visible file mapping")
            _non_empty_string(entry["source"], "visible source")
            destination = _non_empty_string(entry["destination"], "visible destination")
            destination_parts = [part.casefold() for part in Path(destination).parts]
            if (
                "\\" in destination
                or destination.startswith("/")
                or ".." in Path(destination).parts
                or any(
                    part in {"tests", "gold", "expected", "evaluator", "decisions"}
                    or "review" in part
                    for part in destination_parts
                )
            ):
                raise EvidenceContractError(
                    f"visible destination is forbidden: {destination}"
                )
            destinations.append(destination)
        if len(destinations) != len(set(destinations)):
            raise EvidenceContractError("visible destinations contain duplicates")
        _string_list(task["gold_files"], f"{task['task_id']}.gold_files")
    return dict(value)


def prepare_agent_packages(
    root: Path,
    preregistration: Mapping[str, Any],
    source_root: Path,
    package_sources: Mapping[str, Any],
    output_root: Path,
    selected_task_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    prereg = validate_preregistration(preregistration)
    all_ids = [task["task_id"] for task in prereg["tasks"]]
    selected = list(selected_task_ids) if selected_task_ids else all_ids
    if len(selected) != len(set(selected)) or any(task_id not in all_ids for task_id in selected):
        raise EvidenceContractError("selected task ids are invalid or duplicated")
    selected.sort(key=all_ids.index)
    sources = validate_package_sources(package_sources, prereg, selected)
    if output_root.exists():
        raise EvidenceContractError("package output already exists; refusing to overwrite")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    bindings = capture_bindings(root, prereg)
    prereg_tasks = {task["task_id"]: task for task in prereg["tasks"]}
    skill_files = _agent_skill_files(root)
    if not skill_files:
        raise EvidenceContractError("Agent skill payload is empty")

    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}.building-", dir=output_root.parent)
    ).resolve()
    package_entries: list[dict[str, Any]] = []
    try:
        for source_entry in sources["tasks"]:
            task_id = source_entry["task_id"]
            task = prereg_tasks[task_id]
            package = staging / task_id
            package.mkdir()
            for source_path in skill_files:
                relative = source_path.relative_to(root)
                destination = package / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, destination)

            prompt = _rooted_file(source_root, source_entry["prompt_file"])
            if not prompt.is_file():
                raise EvidenceContractError(f"prompt is missing for {task_id}")
            shutil.copyfile(prompt, package / "TASK.md")
            visible_payloads: list[tuple[str, bytes]] = []
            for visible in source_entry["visible_files"]:
                source = _rooted_file(source_root, visible["source"])
                if not source.is_file():
                    raise EvidenceContractError(f"visible input is missing for {task_id}")
                destination_name = f"assigned/{visible['destination']}"
                destination = package / destination_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                payload = source.read_bytes()
                destination.write_bytes(payload)
                visible_payloads.append((visible["destination"], payload))
            gold_payloads = []
            for index, relative in enumerate(source_entry["gold_files"], 1):
                gold_path = _rooted_file(source_root, relative)
                if not gold_path.is_file():
                    raise EvidenceContractError(f"gold is missing for {task_id}")
                gold_payloads.append((f"gold/{index:04d}", gold_path.read_bytes()))
            fixture_sha256 = _framed_named_payloads(visible_payloads)
            gold_sha256 = _framed_named_payloads(gold_payloads)
            prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
            payload_sha256 = _directory_payload_sha256(package)
            package_manifest = {
                "schema": AGENT_PACKAGE_SCHEMA,
                "study_id": prereg["study_id"],
                "task_id": task_id,
                "host_lane": task["host_lane"],
                "privacy": task["privacy"],
                "evidence_class": "fresh-agent-inference",
                "visible_payload_sha256": payload_sha256,
                "fixture_sha256": fixture_sha256,
                "prompt_sha256": prompt_sha256,
                "forbidden_payload_absent": [
                    "tests",
                    "gold",
                    "expected-verdicts",
                    "prior-task-transcripts",
                ],
            }
            (package / "PACKAGE_MANIFEST.json").write_text(
                json.dumps(package_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            package_entries.append(
                {
                    "task_id": task_id,
                    "package": task_id,
                    "package_sha256": _directory_payload_sha256(package),
                    "fixture_sha256": fixture_sha256,
                    "gold_sha256": gold_sha256,
                    "prompt_sha256": prompt_sha256,
                }
            )

        evaluator_manifest = {
            "schema": PACKAGE_MANIFEST_SCHEMA,
            "study_id": prereg["study_id"],
            "bindings": bindings,
            "packages": package_entries,
        }
        (staging / "EVALUATOR_MANIFEST.json").write_text(
            json.dumps(evaluator_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output_root)
        return evaluator_manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_evaluator_manifest(
    value: Mapping[str, Any],
    preregistration: Mapping[str, Any],
) -> dict[str, Any]:
    _exact_keys(value, {"schema", "study_id", "bindings", "packages"}, "evaluator manifest")
    if value["schema"] != PACKAGE_MANIFEST_SCHEMA:
        raise EvidenceContractError("unsupported evaluator manifest schema")
    if value["study_id"] != preregistration["study_id"]:
        raise EvidenceContractError("evaluator manifest study_id is stale")
    bindings = value["bindings"]
    if not isinstance(bindings, dict):
        raise EvidenceContractError("evaluator manifest bindings are missing")
    _exact_keys(bindings, set(CONTRACT_NAMES), "evaluator bindings")
    for name in CONTRACT_NAMES:
        _validate_contract(
            bindings[name],
            preregistration["contract_scopes"][name],
            f"evaluator bindings.{name}",
        )
    packages = value["packages"]
    if not isinstance(packages, list) or not packages:
        raise EvidenceContractError("evaluator packages are empty")
    seen: set[str] = set()
    preregistered_ids = {task["task_id"] for task in preregistration["tasks"]}
    for package in packages:
        if not isinstance(package, dict):
            raise EvidenceContractError("evaluator package entry is invalid")
        _exact_keys(
            package,
            {
                "task_id",
                "package",
                "package_sha256",
                "fixture_sha256",
                "gold_sha256",
                "prompt_sha256",
            },
            "evaluator package",
        )
        task_id = package["task_id"]
        if task_id not in preregistered_ids or task_id in seen:
            raise EvidenceContractError("evaluator package task is duplicated")
        seen.add(task_id)
        if package["package"] != task_id:
            raise EvidenceContractError("evaluator package path is not task-local")
        for field in ("package_sha256", "fixture_sha256", "gold_sha256", "prompt_sha256"):
            _sha256(package[field], f"{task_id}.{field}")
    if [package["task_id"] for package in packages] != sorted(seen):
        raise EvidenceContractError("evaluator packages are not in preregistered order")
    return dict(value)


TASK_ARTIFACT_SCHEMA = "cml.forward-task-artifacts.v1"
REQUIRED_ARTIFACT_ROLES = {
    "agent-review",
    "gold",
    "input",
    "prompt",
    "terminal-receipt",
}
OPTIONAL_ARTIFACT_ROLES = {"browser-state", "product-output"}


def validate_task_artifact_manifest(
    manifest_path: Path,
    task: Mapping[str, Any],
) -> dict[str, Any]:
    value = load_json(manifest_path)
    _exact_keys(value, {"schema", "task_id", "privacy", "artifacts"}, "task artifact manifest")
    if value["schema"] != TASK_ARTIFACT_SCHEMA:
        raise EvidenceContractError("unsupported task artifact manifest schema")
    if value["task_id"] != task["task_id"] or value["privacy"] != task["privacy"]:
        raise EvidenceContractError("task artifact manifest binding is stale")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise EvidenceContractError("task artifact manifest is empty")
    roles: list[str] = []
    paths: list[str] = []
    expected_retention = (
        "public-anonymous"
        if task["privacy"] == "public-anonymous"
        else "local-only"
    )
    root = manifest_path.resolve().parent
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise EvidenceContractError("task artifact entry must be an object")
        _exact_keys(artifact, {"role", "path", "sha256", "retention"}, "task artifact")
        role = _non_empty_string(artifact["role"], "artifact role")
        if role not in REQUIRED_ARTIFACT_ROLES | OPTIONAL_ARTIFACT_ROLES:
            raise EvidenceContractError(f"unsupported task artifact role: {role}")
        relative = _non_empty_string(artifact["path"], "artifact path")
        path = _rooted_file(root, relative)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
            raise EvidenceContractError(f"task artifact is missing or changed: {relative}")
        _sha256(artifact["sha256"], f"artifact {relative}.sha256")
        if artifact["retention"] != expected_retention:
            raise EvidenceContractError("task artifact retention contradicts privacy class")
        roles.append(role)
        paths.append(relative)
    if len(roles) != len(set(roles)) or len(paths) != len(set(paths)):
        raise EvidenceContractError("task artifact roles and paths must be unique")
    if not REQUIRED_ARTIFACT_ROLES <= set(roles):
        raise EvidenceContractError(
            f"task artifact manifest omits roles: {sorted(REQUIRED_ARTIFACT_ROLES - set(roles))}"
        )
    return value


def record_completed_slot(
    root: Path,
    preregistration: Mapping[str, Any],
    results_path: Path,
    evaluator_manifest_path: Path,
    artifact_manifest_path: Path,
    task_id: str,
    agent_context_sha256: str,
    terminal_receipt_path: Path,
    counts_path: Path,
    started_at: str,
    completed_at: str,
    outcome: str,
    failure_attribution: str | None,
) -> dict[str, Any]:
    prereg = validate_preregistration(preregistration)
    results = load_json(results_path)
    validate_results(results, prereg)
    evaluator_manifest = validate_evaluator_manifest(
        load_json(evaluator_manifest_path), prereg
    )
    current_bindings = capture_bindings(root, prereg)
    if evaluator_manifest["bindings"] != current_bindings:
        raise EvidenceContractError("package contracts are stale; do not record the slot")
    package_map = {entry["task_id"]: entry for entry in evaluator_manifest["packages"]}
    package = package_map.get(task_id)
    if package is None:
        raise EvidenceContractError(f"evaluator manifest has no package for {task_id}")
    package_root = evaluator_manifest_path.resolve().parent / package["package"]
    if (
        not package_root.is_dir()
        or _directory_payload_sha256(package_root) != package["package_sha256"]
    ):
        raise EvidenceContractError("Agent package changed after it was frozen")
    task_map = {task["task_id"]: task for task in prereg["tasks"]}
    task = task_map.get(task_id)
    if task is None:
        raise EvidenceContractError(f"unknown task id: {task_id}")
    slot_index = next(
        (index for index, slot in enumerate(results["task_slots"]) if slot["task_id"] == task_id),
        None,
    )
    if slot_index is None or results["task_slots"][slot_index]["status"] != "pending":
        raise EvidenceContractError("result slot is missing or already recorded")
    expected_host = "codex" if task["host_lane"] == "codex-required" else "opencode"
    artifact_manifest = validate_task_artifact_manifest(artifact_manifest_path, task)
    artifact_by_role = {
        artifact["role"]: artifact for artifact in artifact_manifest["artifacts"]
    }
    _sha256(agent_context_sha256, "agent_context_sha256")
    if _rfc3339(completed_at, "completed_at") < _rfc3339(started_at, "started_at"):
        raise EvidenceContractError("completed_at precedes started_at")
    if outcome not in OUTCOMES:
        raise EvidenceContractError("recorded outcome is invalid")
    if outcome in SUCCESS_OUTCOMES and failure_attribution is not None:
        raise EvidenceContractError("successful outcome cannot carry failure attribution")
    if outcome not in SUCCESS_OUTCOMES and failure_attribution not in FAILURE_ATTRIBUTIONS:
        raise EvidenceContractError("failed outcome needs a frozen failure attribution")
    receipt = terminal_receipt_path.resolve()
    if not receipt.is_file():
        raise EvidenceContractError("terminal receipt is missing")
    receipt_artifact = artifact_by_role["terminal-receipt"]
    declared_receipt = _rooted_file(artifact_manifest_path.resolve().parent, receipt_artifact["path"])
    if receipt != declared_receipt:
        raise EvidenceContractError("terminal receipt does not match task artifact manifest")
    counts = _validate_counts(load_json(counts_path), f"{task_id}.counts")
    slot = {
        "task_id": task_id,
        "status": "completed",
        "host": expected_host,
        "agent_context_sha256": agent_context_sha256,
        "artifact_manifest_sha256": hashlib.sha256(
            artifact_manifest_path.resolve().read_bytes()
        ).hexdigest(),
        "counts_sha256": hashlib.sha256(counts_path.resolve().read_bytes()).hexdigest(),
        "fixture_sha256": package["fixture_sha256"],
        "gold_sha256": package["gold_sha256"],
        "started_at": started_at,
        "completed_at": completed_at,
        "terminal_receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        "outcome": outcome,
        "failure_attribution": failure_attribution,
        "counts": counts,
    }
    updated = dict(results)
    updated_slots = list(results["task_slots"])
    updated_slots[slot_index] = slot
    updated["task_slots"] = updated_slots
    updated["state"] = "collecting"
    updated["publication_claim"] = "none-collecting-preregistered-slots"
    updated["bindings"] = evaluator_manifest["bindings"]
    coverage = dict(results["host_coverage"])
    coverage["executed"] = sorted(set(coverage["executed"]) | {expected_host})
    updated["host_coverage"] = coverage
    updated["aggregate"] = aggregate_results(prereg, updated_slots)
    validate_results(updated, prereg)
    _atomic_write_json(results_path, updated)
    return slot


def finalize_results(
    preregistration: Mapping[str, Any],
    results_path: Path,
    conditional_unavailable_reason: str | None,
) -> dict[str, Any]:
    prereg = validate_preregistration(preregistration)
    results = load_json(results_path)
    validate_results(results, prereg)
    if results["state"] == "pending":
        raise EvidenceContractError("no fresh Agent slots have been recorded")
    task_map = {task["task_id"]: task for task in prereg["tasks"]}
    slots = [dict(slot) for slot in results["task_slots"]]
    for slot in slots:
        if slot["status"] != "pending":
            continue
        if task_map[slot["task_id"]]["host_lane"] == "codex-required":
            raise EvidenceContractError(f"required task remains pending: {slot['task_id']}")
        if conditional_unavailable_reason is None:
            raise EvidenceContractError("pending OpenCode slots need an unavailable reason")
        slot.update(
            {
                "status": "host-unavailable",
                "host": "opencode",
                "failure_attribution": "host-or-tooling",
            }
        )
    updated = dict(results)
    updated["state"] = "completed"
    updated["publication_claim"] = "finite-preregistered-task-point-estimates-only"
    updated["task_slots"] = slots
    coverage = dict(results["host_coverage"])
    coverage["conditional_unavailable_reason"] = conditional_unavailable_reason
    updated["host_coverage"] = coverage
    updated["aggregate"] = aggregate_results(prereg, slots)
    validate_results(updated, prereg)
    _atomic_write_json(results_path, updated)
    return updated


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate preregistered forward evidence without rerunning Agent inference."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "status", "capture-contracts"):
        child = subparsers.add_parser(command)
        child.add_argument("--root", type=Path, default=Path.cwd())
        child.add_argument("--preregistration", type=Path, required=True)
        if command != "capture-contracts":
            child.add_argument("--results", type=Path, required=True)
    metric = subparsers.add_parser("interval")
    metric.add_argument("numerator", type=int)
    metric.add_argument("denominator", type=int)
    packages = subparsers.add_parser("prepare-packages")
    packages.add_argument("--root", type=Path, default=Path.cwd())
    packages.add_argument("--preregistration", type=Path, required=True)
    packages.add_argument("--source-root", type=Path, required=True)
    packages.add_argument("--source-manifest", type=Path, required=True)
    packages.add_argument("--output", type=Path, required=True)
    packages.add_argument("--task-id", action="append", default=[])
    record = subparsers.add_parser("record-slot")
    record.add_argument("--root", type=Path, default=Path.cwd())
    record.add_argument("--preregistration", type=Path, required=True)
    record.add_argument("--results", type=Path, required=True)
    record.add_argument("--evaluator-manifest", type=Path, required=True)
    record.add_argument("--artifact-manifest", type=Path, required=True)
    record.add_argument("--task-id", required=True)
    record.add_argument("--agent-context-sha256", required=True)
    record.add_argument("--terminal-receipt", type=Path, required=True)
    record.add_argument("--counts", type=Path, required=True)
    record.add_argument("--started-at", required=True)
    record.add_argument("--completed-at", required=True)
    record.add_argument("--outcome", choices=sorted(OUTCOMES), required=True)
    record.add_argument(
        "--failure-attribution",
        choices=FAILURE_ATTRIBUTIONS,
    )
    finalize = subparsers.add_parser("finalize-results")
    finalize.add_argument("--preregistration", type=Path, required=True)
    finalize.add_argument("--results", type=Path, required=True)
    finalize.add_argument("--conditional-unavailable-reason")
    legacy = subparsers.add_parser("legacy-status")
    legacy.add_argument("--root", type=Path, default=Path.cwd())
    legacy.add_argument("--summary", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "interval":
        payload = wilson_metric(args.numerator, args.denominator)
    elif args.command == "legacy-status":
        payload = assess_legacy_summary(args.root, load_json(args.summary))
    elif args.command == "prepare-packages":
        preregistration = load_json(args.preregistration)
        payload = prepare_agent_packages(
            args.root,
            preregistration,
            args.source_root,
            load_json(args.source_manifest),
            args.output,
            args.task_id or None,
        )
    elif args.command == "record-slot":
        preregistration = load_json(args.preregistration)
        payload = record_completed_slot(
            args.root,
            preregistration,
            args.results,
            args.evaluator_manifest,
            args.artifact_manifest,
            args.task_id,
            args.agent_context_sha256,
            args.terminal_receipt,
            args.counts,
            args.started_at,
            args.completed_at,
            args.outcome,
            args.failure_attribution,
        )
    elif args.command == "finalize-results":
        payload = finalize_results(
            load_json(args.preregistration),
            args.results,
            args.conditional_unavailable_reason,
        )
    else:
        preregistration = load_json(args.preregistration)
        validate_preregistration(preregistration)
        if args.command == "capture-contracts":
            payload = capture_bindings(args.root, preregistration)
        else:
            results = load_json(args.results)
            validate_results(results, preregistration)
            payload = (
                assess_evidence(args.root, preregistration, results)
                if args.command == "status"
                else {"status": "valid"}
            )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
