from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from common import (
    WorkspaceTransaction,
    load_jsonl,
    load_manifest,
    resolve_in_workspace,
    resolve_workspace_paths,
    sha256_file,
    write_json,
    write_utf8,
)
import scan_identity
import ad_decision_policy


MODULES = {
    "ads": {
        "candidates": "candidates/ads.jsonl",
        "decisions": "decisions/ads_decisions.jsonl",
        "mutating_verdicts": {"delete"},
        "mutating_actions": {"delete"},
    },
    "titles": {
        "candidates": "candidates/titles.jsonl",
    },
    "blocked": {
        "candidates": "candidates/blocked.jsonl",
    },
}
MODULE_STAGES = {
    "ads": "2_ads",
    "titles": "3_titles",
    "blocked": "4_blocked_words",
}
SCAN_REPORTS = {
    "ads": "report/ads_scan_report.json",
    "titles": "report/titles_scan_report.json",
    "blocked": "report/blocked_scan_report.json",
}
ACTIVE_SCAN_STATUSES = frozenset(
    {"candidates_ready", "draft_decisions_ready", "formal_decisions_ready", "done"}
)
MAX_SEGMENT_PREVIEWS = 50


def is_mutating(decision: dict[str, Any], config: dict[str, Any]) -> bool:
    verdict = str(decision.get("verdict", ""))
    action = str(decision.get("action", ""))
    return verdict in config.get("mutating_verdicts", set()) or action in config.get(
        "mutating_actions", set()
    )


def module_summary(
    candidates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    module: str,
    config: dict[str, Any],
    scan_current: bool,
    decisions_current: bool,
    scan_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    executable = isinstance(config.get("decisions"), str)
    report_summary = (
        scan_report.get("summary")
        if isinstance(scan_report, dict) and isinstance(scan_report.get("summary"), dict)
        else {}
    )
    report_only_scan_complete = True
    if module == "titles":
        suppressed = report_summary.get("suppressed_report_only_count", 0)
        report_only_scan_complete = (
            isinstance(suppressed, int)
            and not isinstance(suppressed, bool)
            and suppressed == 0
        )
    elif module == "blocked":
        report_only_scan_complete = (
            report_summary.get("max_candidates_reached", False) is False
        )
    mutating = [decision for decision in decisions if is_mutating(decision, config)]
    uncertain = [
        decision
        for decision in decisions
        if str(decision.get("verdict", "")) == "uncertain"
        or str(decision.get("action", "")) in {"mark_candidates", "keep_original"}
    ]
    summary = {
        "candidate_file": config["candidates"],
        "candidate_count": len(candidates),
        "anchor_count": sum(
            len(candidate.get("anchors", []))
            for candidate in candidates
            if isinstance(candidate.get("anchors", []), list)
        ),
        "truncated_candidate_count": sum(
            candidate.get("anchors_truncated") is True for candidate in candidates
        ),
        "candidate_file_exists": scan_current,
        "scan_current": scan_current,
        "status": (
            "complete"
            if scan_current
            and (not executable or decisions_current)
            and report_only_scan_complete
            else "pending"
        ),
    }
    if not executable:
        summary["report_only"] = True
        return summary
    summary.update(
        {
            "decision_file": config["decisions"],
            "decision_count": len(decisions),
            "estimated_mutating_decision_count": len(mutating),
            "manual_review_count": len(uncertain),
            "decision_file_exists": decisions_current,
            "decision_file_current": decisions_current,
        }
    )
    if module == "ads":
        decision_by_id = {
            str(decision.get("candidate_id") or ""): decision
            for decision in decisions
        }
        segment_previews: list[dict[str, Any]] = []
        total_segment_occurrences = 0
        for candidate in candidates:
            if candidate.get("edit_plan") is None:
                continue
            previews = ad_decision_policy.edit_plan_preview(candidate)
            total_segment_occurrences += len(previews)
            decision = decision_by_id.get(str(candidate.get("candidate_id") or ""), {})
            for preview in previews:
                if len(segment_previews) >= MAX_SEGMENT_PREVIEWS:
                    break
                segment_previews.append(
                    {
                        "candidate_id": candidate.get("candidate_id"),
                        "edit_plan_id": candidate["edit_plan"]["edit_plan_id"],
                        "requested_verdict": decision.get("verdict"),
                        **preview,
                    }
                )
        summary["segment_edit_preview_count"] = total_segment_occurrences
        summary["segment_edit_previews_truncated"] = (
            total_segment_occurrences > len(segment_previews)
        )
        summary["segment_edit_previews"] = segment_previews
    return summary


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = ["# Dry Run Report", "", f"- Status: {report['status']}", ""]
    for module, summary in report["modules"].items():
        lines.extend(
            [
                f"## {module}",
                "",
                f"- Candidates: {summary['candidate_count']}",
                f"- Anchors: {summary['anchor_count']}",
                f"- Truncated candidates: {summary['truncated_candidate_count']}",
            ]
        )
        if summary.get("report_only") is True:
            lines.extend(["- Mode: report-only", ""])
        else:
            lines.extend(
                [
                    f"- Decisions: {summary['decision_count']}",
                    f"- Estimated mutating decisions: {summary['estimated_mutating_decision_count']}",
                    f"- Manual review decisions: {summary['manual_review_count']}",
                    "",
                ]
            )
            previews = summary.get("segment_edit_previews", [])
            if isinstance(previews, list) and previews:
                lines.extend(["### Mixed segment previews", ""])
                for preview in previews[:10]:
                    if not isinstance(preview, dict):
                        continue
                    lines.extend(
                        [
                            f"- Candidate: {preview.get('candidate_id')}",
                            f"  - Keep: {preview.get('keep_text', '')}",
                            f"  - Delete: {preview.get('delete_text', '')}",
                            f"  - After: {preview.get('after_text', '')}",
                        ]
                    )
                lines.append("")
    lines.extend(
        [
            "## Notes",
            "",
            "- Dry run does not edit novel text.",
            "- A candidate count is not a deletion count; only confirmed mutating decisions can change files.",
            "",
        ]
    )
    write_utf8(path, "\n".join(lines))


def _stage(manifest: dict[str, Any], module: str) -> dict[str, Any]:
    stages = manifest.get("stages")
    value = stages.get(MODULE_STAGES[module]) if isinstance(stages, dict) else None
    return value if isinstance(value, dict) else {}


def _committed_path(
    workspace: Path,
    manifest: dict[str, Any],
    relative: str,
) -> Path:
    relative = relative.replace("\\", "/")
    path = resolve_in_workspace(workspace, relative, role="read")
    artifacts = manifest.get("artifacts")
    record = artifacts.get(relative) if isinstance(artifacts, dict) else None
    if not isinstance(record, dict) or record.get("sha256") != sha256_file(path):
        raise ValueError(f"dry-run input is not a current committed artifact: {relative}")
    return path


def load_current_scan(
    workspace: Path,
    manifest: dict[str, Any],
    module: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    stage = _stage(manifest, module)
    if stage.get("status") not in ACTIVE_SCAN_STATUSES:
        return [], {}, False
    if not isinstance(stage.get("scan_id"), str):
        raise ValueError(f"active {module} stage has no scan identity")
    report_path = _committed_path(workspace, manifest, SCAN_REPORTS[module])
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{module} scan report is not valid UTF-8 JSON") from error
    if not isinstance(report, dict):
        raise ValueError(f"{module} scan report must be an object")
    if module == "ads":
        candidates = scan_identity.load_validated_pages(workspace, report)
    else:
        output = report.get("output")
        if not isinstance(output, str):
            raise ValueError(f"{module} scan report has no candidate output")
        candidates = load_jsonl(_committed_path(workspace, manifest, output))
        scan_identity.validate_scan_identity(workspace, report, candidates)
    return candidates, report, True


def load_current_decisions(
    workspace: Path,
    manifest: dict[str, Any],
    module: str,
    candidates: list[dict[str, Any]],
    report: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    if module != "ads":
        raise ValueError("formal decisions are only executable for ads")
    stage = _stage(manifest, module)
    status = stage.get("status")
    if status not in {"formal_decisions_ready", "done"}:
        return [], False
    relative = stage.get("decisions") if status == "done" else stage.get("formal_decisions")
    if not isinstance(relative, str):
        return [], False
    relative = relative.replace("\\", "/")
    if relative != MODULES[module]["decisions"]:
        raise ValueError(f"current {module} decisions are not at the canonical path")
    path = _committed_path(workspace, manifest, relative)
    if status == "done" and stage.get("decision_sha256") != sha256_file(path):
        raise ValueError(f"current {module} decision hash is stale")
    decisions = load_jsonl(path)
    candidate_map = {str(item.get("candidate_id") or ""): item for item in candidates}
    decision_ids = [str(item.get("candidate_id") or "") for item in decisions]
    if not all(decision_ids) or len(decision_ids) != len(set(decision_ids)):
        raise ValueError(f"current {module} decisions contain invalid or duplicate IDs")
    if module == "ads" and set(decision_ids) != set(candidate_map):
        raise ValueError("current ad decisions do not cover the complete candidate set")
    for decision in decisions:
        candidate = candidate_map.get(str(decision["candidate_id"]))
        if (
            candidate is None
            or decision.get("scan_id") != report.get("scan_id")
            or decision.get("candidate_fingerprint") != candidate.get("candidate_fingerprint")
        ):
            raise ValueError(f"current {module} decision identity is stale")
    return decisions, True


def run(workspace: Path) -> dict[str, Any]:
    workspace, _, _ = resolve_workspace_paths(workspace)
    manifest = load_manifest(workspace)
    scans = {
        module: load_current_scan(workspace, manifest, module)
        for module in MODULES
    }
    decisions = {
        module: (
            load_current_decisions(
                workspace,
                manifest,
                module,
                scans[module][0],
                scans[module][1],
            )
            if module == "ads" and scans[module][2]
            else ([], False)
        )
        for module in MODULES
    }
    workspace, _, write_paths = resolve_workspace_paths(
        workspace,
        writes={
            "report": "report/dry_run_report.json",
            "markdown": "report/dry_run_report.md",
        },
    )
    modules = {}
    for module, config in MODULES.items():
        module_config = dict(config)
        if module == "ads":
            module_config["candidates"] = "candidates/ads_pages"
        modules[module] = module_summary(
            scans[module][0],
            decisions[module][0],
            module,
            module_config,
            scans[module][2],
            decisions[module][1],
            scan_report=scans[module][1],
        )
    report = {
        "status": (
            "complete"
            if all(summary["status"] == "complete" for summary in modules.values())
            else "pending"
        ),
        "modules": modules,
    }
    with WorkspaceTransaction(workspace) as transaction:
        write_json(transaction.stage_path(write_paths["report"]), report)
        write_markdown(transaction.stage_path(write_paths["markdown"]), report)
        transaction.commit(
            {
                "dry_run": (
                    "done",
                    {
                        "report": "report/dry_run_report.json",
                        "markdown": "report/dry_run_report.md",
                        "summary_status": report["status"],
                    },
                )
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize candidates and decisions without editing text.")
    parser.add_argument("workspace", help="Path to the .cleanwork directory.")
    args = parser.parse_args()
    report = run(Path(args.workspace).resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "complete":
        sys.exit(1)


if __name__ == "__main__":
    main()
