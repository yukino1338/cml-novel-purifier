from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from apply_decisions import (
    apply_operations,
    collect_operations,
    decision_action,
    decision_anchors,
    log_operations,
)
from common import (
    WorkspaceTransaction,
    load_jsonl,
    load_manifest,
    read_utf8,
    resolve_in_workspace,
    resolve_workspace_paths,
    sha256_file,
    stage_invalidation_targets,
    workspace_transaction_lock,
    write_json,
    write_jsonl,
    write_utf8,
)
import ad_decision_policy


MODULE_PATHS = {
    "ads": {
        "stage": "2_ads",
        "input": "versions/v1_preprocessed.txt",
        "output": "versions/v2_ads_removed.txt",
        "decisions": "decisions/ads_decisions.jsonl",
    },
}


def _module_paths(module: str) -> dict[str, str]:
    if module not in MODULE_PATHS:
        raise ValueError(f"unsupported module rollback: {module}")
    return MODULE_PATHS[module]


def _relative(workspace: Path, path: Path) -> str:
    return path.relative_to(workspace).as_posix()


def _validate_version_target(workspace: Path, target: Path) -> str:
    relative = target.relative_to(workspace)
    if len(relative.parts) < 2 or relative.parts[0] != "versions" or target.suffix != ".txt":
        raise ValueError("rollback output must be a .txt artifact inside versions/")
    return relative.as_posix()


def _copy_and_validate(source: Path, staged_target: Path) -> str:
    shutil.copyfile(source, staged_target)
    source_sha256 = sha256_file(source)
    if sha256_file(staged_target) != source_sha256 or staged_target.read_bytes() != source.read_bytes():
        raise ValueError("staged rollback copy does not match its source")
    return source_sha256


def _validate_module_baseline(
    workspace: Path,
    module: str,
    source: Path,
    decisions_path: Path,
    canonical_output: Path,
    anomalies_path: Path,
) -> tuple[str, list[dict[str, Any]], str, str, str, str]:
    paths = _module_paths(module)
    source_text = read_utf8(source)
    decisions = load_jsonl(decisions_path)
    full_operations = collect_operations(source_text, decisions, anomalies_path, module)
    replayed = apply_operations(source_text, full_operations)
    canonical_text = read_utf8(canonical_output)
    if replayed != canonical_text:
        raise ValueError("formal decisions no longer replay to the committed module output")

    input_sha256 = sha256_file(source)
    decision_sha256 = sha256_file(decisions_path)
    canonical_sha256 = sha256_file(canonical_output)
    manifest = load_manifest(workspace)
    stages = manifest.get("stages")
    stage = stages.get(paths["stage"]) if isinstance(stages, dict) else None
    if not isinstance(stage, dict) or stage.get("status") != "done":
        raise ValueError("module rollback requires a current completed apply stage")
    expected = {
        "input": _relative(workspace, source),
        "decisions": _relative(workspace, decisions_path),
        "output": _relative(workspace, canonical_output),
        "input_sha256": input_sha256,
        "decision_sha256": decision_sha256,
        "output_sha256": canonical_sha256,
    }
    for field, value in expected.items():
        if stage.get(field) != value:
            raise ValueError(f"module rollback apply binding is stale: {field}")
    active_run_id = stage.get("active_run_id")
    if not isinstance(active_run_id, str) or not active_run_id:
        raise ValueError("module rollback apply stage has no active_run_id")
    return (
        source_text,
        decisions,
        input_sha256,
        decision_sha256,
        canonical_sha256,
        active_run_id,
    )


def rollback_all(
    workspace: Path,
    output_value: str | None,
    overwrite: bool,
) -> dict[str, Any]:
    with workspace_transaction_lock(workspace):
        workspace, reads, writes = resolve_workspace_paths(
            workspace,
            reads={"source": "versions/v0_original.txt"},
            writes={
                "target": output_value or "versions/rollback_v0_original.txt",
                "report": "report/rollback_report.json",
            },
        )
        source = reads["source"]
        target = writes["target"]
        target_rel = _validate_version_target(workspace, target)
        if target.exists() and not overwrite:
            raise FileExistsError(f"target exists; pass --overwrite to replace it: {target}")
        manifest = load_manifest(workspace)
        report: dict[str, Any] = {
            "level": "all",
            "source": _relative(workspace, source),
            "output": target_rel,
            "input_sha256": sha256_file(source),
            "current_head_before": manifest["current_head"],
            "current_head_after": target_rel,
            "invalidated_stages": list(stage_invalidation_targets("rollback_all")),
        }
        with WorkspaceTransaction(workspace) as transaction:
            staged_target = transaction.stage_path(target)
            _copy_and_validate(source, staged_target)
            report["output_sha256"] = sha256_file(staged_target)
            report["run_id"] = transaction.run_id
            write_json(transaction.stage_path(writes["report"]), report)
            transaction.commit({"rollback_all": ("done", report)})
        return report


def rollback_module(
    workspace: Path,
    module: str,
    output_value: str | None,
    overwrite: bool,
) -> dict[str, Any]:
    paths = _module_paths(module)
    with workspace_transaction_lock(workspace):
        workspace, reads, writes = resolve_workspace_paths(
            workspace,
            reads={"source": paths["input"], "decisions": paths["decisions"]},
            writes={
                "target": output_value or paths["output"],
                "report": "report/rollback_report.json",
                "anomalies": "logs/anomalies.jsonl",
            },
        )
        source = reads["source"]
        decisions_path = reads["decisions"]
        canonical_output = resolve_in_workspace(workspace, paths["output"], role="read")
        target = writes["target"]
        target_rel = _validate_version_target(workspace, target)
        if target.exists() and not overwrite:
            raise FileExistsError(f"target exists; pass --overwrite to replace it: {target}")
        (
            _source_text,
            decisions,
            input_sha256,
            decision_sha256,
            baseline_sha256,
            apply_run_id,
        ) = _validate_module_baseline(
            workspace,
            module,
            source,
            decisions_path,
            canonical_output,
            writes["anomalies"],
        )
        stage = f"rollback_{module}"
        manifest = load_manifest(workspace)
        report: dict[str, Any] = {
            "level": "module",
            "module": module,
            "source": _relative(workspace, source),
            "original_decisions": _relative(workspace, decisions_path),
            "output": target_rel,
            "decision_count": len(decisions),
            "input_sha256": input_sha256,
            "original_decision_sha256": decision_sha256,
            "baseline_output_sha256": baseline_sha256,
            "apply_run_id": apply_run_id,
            "current_head_before": manifest["current_head"],
            "current_head_after": target_rel,
            "invalidated_stages": list(stage_invalidation_targets(stage)),
        }
        with WorkspaceTransaction(workspace) as transaction:
            staged_target = transaction.stage_path(target)
            _copy_and_validate(source, staged_target)
            report["output_sha256"] = sha256_file(staged_target)
            report["run_id"] = transaction.run_id
            write_json(transaction.stage_path(writes["report"]), report)
            transaction.commit({stage: ("done", {**report, "input": report["source"]})})
        return report


def _chapter_index(anchor: dict[str, Any]) -> int:
    chapter = anchor.get("chapter")
    if not isinstance(chapter, dict):
        raise ValueError("chapter rollback requires every mutating anchor to have a true chapter")
    index = chapter.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise ValueError("chapter rollback found an invalid true chapter index")
    return index


def decision_chapter_indexes(decision: dict[str, Any]) -> set[int]:
    if decision_action(decision) is None:
        return set()
    return {_chapter_index(anchor) for anchor in decision_anchors(decision)}


def _anchor_text_sha256(anchor: dict[str, Any]) -> str:
    original = anchor.get("original")
    if not isinstance(original, str):
        raise ValueError("chapter rollback requires every remaining anchor to carry original text")
    return hashlib.sha256(original.encode("utf-8")).hexdigest()


def _filter_chapter(
    decisions: list[dict[str, Any]],
    chapter: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
        raise ValueError("chapter index must be a positive integer")
    filtered: list[dict[str, Any]] = []
    restored_anchor_ids: list[str] = []
    for decision in decisions:
        if decision_action(decision) is None:
            filtered.append(copy.deepcopy(decision))
            continue
        kept_anchors: list[dict[str, Any]] = []
        for anchor in decision_anchors(decision):
            if _chapter_index(anchor) == chapter:
                restored_anchor_ids.append(str(anchor["anchor_id"]))
            else:
                kept_anchors.append(copy.deepcopy(anchor))
        if kept_anchors:
            item = copy.deepcopy(decision)
            item["anchors"] = kept_anchors
            item["anchor_ids"] = [str(anchor["anchor_id"]) for anchor in kept_anchors]
            item["anchor_text_sha256s"] = [
                _anchor_text_sha256(anchor)
                for anchor in kept_anchors
            ]
            item["occurrence_count"] = len(kept_anchors)
            if decision.get("edit_plan") is not None:
                subset = ad_decision_policy.subset_edit_plan_for_rollback(
                    decision["edit_plan"],
                    decision,
                    item["anchor_ids"],
                )
                item["edit_plan"] = subset
                item["edit_plan_id"] = subset["edit_plan_id"]
            filtered.append(item)
    if not restored_anchor_ids:
        raise ValueError(f"chapter rollback target has no matching anchor: {chapter}")
    return filtered, sorted(restored_anchor_ids)


def _filter_point(
    decisions: list[dict[str, Any]],
    candidate_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    matches = [
        decision
        for decision in decisions
        if str(decision.get("candidate_id", "")) == candidate_id
    ]
    if len(matches) != 1:
        raise ValueError(f"point rollback target must match exactly one decision: {candidate_id}")
    target = matches[0]
    if decision_action(target) is None:
        raise ValueError(f"point rollback target is not a mutating decision: {candidate_id}")
    restored_anchor_ids = sorted(
        str(anchor["anchor_id"]) for anchor in decision_anchors(target)
    )
    return (
        [
            copy.deepcopy(decision)
            for decision in decisions
            if decision is not target
        ],
        restored_anchor_ids,
    )


def _remaining_anchor_ids(decisions: list[dict[str, Any]]) -> list[str]:
    return sorted(
        str(anchor["anchor_id"])
        for decision in decisions
        if decision_action(decision) is not None
        for anchor in decision_anchors(decision)
    )


def _rollback_filtered(
    workspace: Path,
    module: str,
    level: str,
    target_value: int | str,
    output_value: str | None,
) -> dict[str, Any]:
    paths = _module_paths(module)
    filtered_rel = f"decisions/rollback_{module}_{level}.jsonl"
    workspace, reads, writes = resolve_workspace_paths(
        workspace,
        reads={"source": paths["input"], "decisions": paths["decisions"]},
        writes={
            "filtered_decisions": filtered_rel,
            "target": output_value or paths["output"],
            "anomalies": "logs/anomalies.jsonl",
            "operations": "logs/operations.jsonl",
            "report": "report/rollback_report.json",
        },
    )
    source = reads["source"]
    decisions_path = reads["decisions"]
    canonical_output = resolve_in_workspace(workspace, paths["output"], role="read")
    target = writes["target"]
    target_rel = _validate_version_target(workspace, target)
    (
        source_text,
        decisions,
        input_sha256,
        original_decision_sha256,
        baseline_sha256,
        apply_run_id,
    ) = _validate_module_baseline(
        workspace,
        module,
        source,
        decisions_path,
        canonical_output,
        writes["anomalies"],
    )
    if level == "chapter":
        filtered, restored_anchor_ids = _filter_chapter(decisions, int(target_value))
    elif level == "point":
        filtered, restored_anchor_ids = _filter_point(decisions, str(target_value))
    else:
        raise ValueError(f"unsupported targeted rollback level: {level}")

    stage = f"rollback_{module}_{level}"
    manifest = load_manifest(workspace)
    report: dict[str, Any] = {
        "level": level,
        "module": module,
        "target": target_value,
        "source": _relative(workspace, source),
        "original_decisions": _relative(workspace, decisions_path),
        "filtered_decisions": filtered_rel,
        "output": target_rel,
        "input_sha256": input_sha256,
        "original_decision_sha256": original_decision_sha256,
        "baseline_output_sha256": baseline_sha256,
        "apply_run_id": apply_run_id,
        "restored_anchor_ids": restored_anchor_ids,
        "remaining_anchor_ids": _remaining_anchor_ids(filtered),
        "current_head_before": manifest["current_head"],
        "current_head_after": target_rel,
        "invalidated_stages": list(stage_invalidation_targets(stage)),
    }
    with WorkspaceTransaction(workspace) as transaction:
        staged_filtered = transaction.stage_path(writes["filtered_decisions"])
        write_jsonl(staged_filtered, filtered)
        filtered_decision_sha256 = sha256_file(staged_filtered)
        operations = collect_operations(source_text, filtered, writes["anomalies"], module)
        staged_target = transaction.stage_path(target)
        write_utf8(staged_target, apply_operations(source_text, operations))
        output_sha256 = sha256_file(staged_target)
        operations_path = transaction.stage_path(writes["operations"], copy_existing=True)
        log_operations(
            workspace,
            operations_path,
            module,
            source,
            writes["filtered_decisions"],
            target,
            operations,
            run_id=transaction.run_id,
            input_sha256=input_sha256,
            decision_sha256=filtered_decision_sha256,
            output_sha256=output_sha256,
        )
        transaction.discard_unwritten_stage(writes["operations"])
        report.update(
            {
                "run_id": transaction.run_id,
                "filtered_decision_sha256": filtered_decision_sha256,
                "output_sha256": output_sha256,
                "operation_count": len(operations),
            }
        )
        write_json(transaction.stage_path(writes["report"]), report)
        transaction.commit(
            {
                stage: (
                    "done",
                    {
                        **report,
                        "input": report["source"],
                        "decisions": filtered_rel,
                        "decision_sha256": filtered_decision_sha256,
                    },
                )
            }
        )
    return report


def rollback_chapter(
    workspace: Path,
    module: str,
    chapter: int,
    output_value: str | None = None,
) -> dict[str, Any]:
    with workspace_transaction_lock(workspace):
        return _rollback_filtered(workspace, module, "chapter", chapter, output_value)


def rollback_point(
    workspace: Path,
    module: str,
    candidate_id: str,
    output_value: str | None = None,
) -> dict[str, Any]:
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate_id must be a non-empty string")
    with workspace_transaction_lock(workspace):
        return _rollback_filtered(workspace, module, "point", candidate_id, output_value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rollback version-chain outputs.")
    parser.add_argument("workspace", help="Path to the .cleanwork directory.")
    parser.add_argument("--level", choices=["all", "module", "chapter", "point"], required=True)
    parser.add_argument("--module", choices=["ads"], default="ads")
    parser.add_argument("--chapter", type=int, help="Chapter index for chapter rollback.")
    parser.add_argument("--candidate-id", help="Candidate id for point rollback.")
    parser.add_argument("--output", help="Optional output version path relative to workspace.")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    if args.level == "all":
        report = rollback_all(workspace, args.output, args.overwrite)
    elif args.level == "module":
        report = rollback_module(workspace, args.module, args.output, args.overwrite)
    elif args.level == "chapter":
        if args.chapter is None:
            raise ValueError("--chapter is required for chapter rollback")
        report = rollback_chapter(workspace, args.module, args.chapter, args.output)
    else:
        if not args.candidate_id:
            raise ValueError("--candidate-id is required for point rollback")
        report = rollback_point(workspace, args.module, args.candidate_id, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
