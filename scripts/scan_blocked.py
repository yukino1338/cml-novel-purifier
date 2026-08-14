from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from common import (
    WorkspaceTransaction,
    read_utf8,
    resolve_current_head,
    resolve_workspace_paths,
    workspace_transaction_lock,
    write_json,
    write_jsonl,
)
from parse_structure import build_structure_artifact
from scan_identity import (
    attach_anchor_ids,
    attach_candidate_fingerprints,
    build_scan_identity,
    load_bound_structure,
)


MASK_RE = re.compile(r"(?<=[\u4e00-\u9fff])[\*＊×Xx□■口](?=[\u4e00-\u9fff])|[\*＊×Xx□■口]{2,}")
SEPARATOR_RE = re.compile(r"[\u4e00-\u9fff][·•・.．]{1,2}[\u4e00-\u9fff]")
SENTENCE_BOUNDARIES = "。！？!?；;\n"


MAX_CANDIDATES = 10_000


def validate_max_candidates(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_CANDIDATES:
        raise ValueError(f"max_candidates must be an integer from 1 to {MAX_CANDIDATES}")


def choose_input(workspace: Path, value: str) -> str:
    if value != "auto":
        return value
    return resolve_current_head(workspace).relative_to(Path(workspace).resolve()).as_posix()


def sentence_context(text: str, start: int, end: int, sentence_count: int = 3) -> dict[str, str]:
    left = start
    seen = 0
    while left > 0 and seen < sentence_count:
        left -= 1
        if text[left] in SENTENCE_BOUNDARIES:
            seen += 1
    if left < start and left < len(text) and text[left] in SENTENCE_BOUNDARIES:
        left += 1

    right = end
    seen = 0
    while right < len(text) and seen < sentence_count:
        if text[right] in SENTENCE_BOUNDARIES:
            seen += 1
        right += 1
    return {
        "before": text[left:start],
        "original": text[start:end],
        "after": text[end:right],
    }


def anchor(text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "offset": start,
        "end": end,
        "original": text[start:end],
        "prefix": text[max(0, start - 10) : start],
        "suffix": text[end : min(len(text), end + 10)],
    }


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_text(text: str, max_candidates: int = 500) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validate_max_candidates(max_candidates)
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()

    def add_match(match: re.Match[str], mask_type: str) -> None:
        if len(candidates) >= max_candidates:
            return
        start, end = match.span()
        if (start, end) in seen:
            return
        seen.add((start, end))
        candidate_id = f"BW-{len(candidates) + 1:04d}"
        context = sentence_context(text, start, end)
        candidates.append(
            {
                "candidate_id": candidate_id,
                "mask_type": mask_type,
                "line": line_number(text, start),
                "offset": start,
                "original": text[start:end],
                "context": context,
                "report_only": True,
                "anchors": [anchor(text, start, end)],
                "suggested_decision": {
                    "candidate_id": candidate_id,
                    "verdict": "uncertain",
                    "confidence": None,
                    "reason": "report-only in this release; no supported blocked-word compiler/apply/verify mutation path",
                    "alternatives": [],
                    "risk": "high",
                },
            }
        )

    for match in MASK_RE.finditer(text):
        add_match(match, "mask_chars")
    for match in SEPARATOR_RE.finditer(text):
        add_match(match, "word_separator")

    summary = {
        "candidate_count": len(candidates),
        "max_candidates_reached": len(candidates) >= max_candidates,
        "by_type": {},
    }
    for candidate in candidates:
        mask_type = candidate["mask_type"]
        summary["by_type"][mask_type] = summary["by_type"].get(mask_type, 0) + 1
    return candidates, summary


def run(workspace: Path, input_value: str, output_value: str, max_candidates: int) -> dict[str, Any]:
    validate_max_candidates(max_candidates)
    with workspace_transaction_lock(workspace):
        return _run_locked(workspace, input_value, output_value, max_candidates)


def _run_locked(
    workspace: Path,
    input_value: str,
    output_value: str,
    max_candidates: int,
) -> dict[str, Any]:
    selected_input = choose_input(workspace, input_value)
    workspace, read_paths, write_paths = resolve_workspace_paths(
        workspace,
        reads={"input": selected_input},
        writes={
            "output": output_value,
            "report": "report/blocked_scan_report.json",
            "structure": "meta/blocked_structure.json",
        },
    )
    input_path = read_paths["input"]
    output_path = write_paths["output"]
    text = read_utf8(input_path)
    structure, _structure_report = build_structure_artifact(text, input_path)
    candidates, summary = scan_text(text, max_candidates)
    attach_candidate_fingerprints(candidates)
    attach_anchor_ids(candidates)
    scan_config = {"max_candidates": max_candidates}
    structure_value = write_paths["structure"].relative_to(workspace).as_posix()
    with WorkspaceTransaction(workspace) as transaction:
        staged_structure = transaction.stage_path(write_paths["structure"])
        write_json(staged_structure, structure)
        load_bound_structure(input_path, staged_structure)
        scan_identity = build_scan_identity(
            "blocked",
            input_path,
            staged_structure,
            scan_config,
            candidates,
        )
        report = {
            **scan_identity,
            "input": str(input_path.relative_to(workspace)),
            "structure": structure_value,
            "output": str(output_path.relative_to(workspace)),
            "scan_config": scan_config,
            "summary": summary,
        }
        write_jsonl(transaction.stage_path(output_path), candidates)
        write_json(transaction.stage_path(write_paths["report"]), report)
        transaction.commit(
            {
                "4_blocked_words": (
                    "candidates_ready",
                    {
                        "input": str(input_path.relative_to(workspace)),
                        "structure": structure_value,
                        "candidates": str(output_path.relative_to(workspace)),
                        "report": "report/blocked_scan_report.json",
                        "candidate_count": summary["candidate_count"],
                        **scan_identity,
                    },
                )
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan blocked-word masking candidates.")
    parser.add_argument("workspace", help="Path to the .cleanwork directory.")
    parser.add_argument("--input", default="auto")
    parser.add_argument("--output", default="candidates/blocked.jsonl")
    parser.add_argument("--max-candidates", type=int, default=500)
    args = parser.parse_args()
    report = run(Path(args.workspace).resolve(), args.input, args.output, args.max_candidates)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
