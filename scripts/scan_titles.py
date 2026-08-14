from __future__ import annotations

import argparse
import bisect
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
from parse_structure import (
    build_structure_artifact,
    match_chapter,
    parse as parse_chapters,
)
from scan_identity import (
    attach_anchor_ids,
    attach_candidate_fingerprints,
    build_scan_identity,
    load_bound_structure,
)


CHAPTER_MENTION_RE = re.compile(r"第[0-9零〇一二两三四五六七八九十百千万]+[章节卷集部篇回]")
OPENERS = "（([{【《「『"
CLOSERS = "）)]}】》」』"
PAIRED = dict(zip(OPENERS, CLOSERS))
MAX_LOW_CONFIDENCE_PSEUDO_TITLES = 50


def choose_input(workspace: Path, value: str) -> str:
    if value != "auto":
        return value
    return resolve_current_head(workspace).relative_to(Path(workspace).resolve()).as_posix()


def line_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = 0
    for line_no, line in enumerate(text.splitlines(keepends=True), 1):
        raw = line.rstrip("\n")
        leading = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        start = offset + leading
        end = start + len(stripped)
        records.append(
            {
                "line": line_no,
                "raw": raw,
                "text": stripped,
                "start": start,
                "end": end,
                "line_end": offset + len(line),
                "heading": match_chapter(stripped),
            }
        )
        offset += len(line)
    return records


def anchor(text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "offset": start,
        "end": end,
        "original": text[start:end],
        "prefix": text[max(0, start - 10) : start],
        "suffix": text[end : min(len(text), end + 10)],
    }


def locator_lookup(locators: list[dict[str, Any]], offset: int) -> dict[str, Any] | None:
    starts = [int(item.get("start_offset", 0)) for item in locators]
    index = bisect.bisect_right(starts, offset) - 1
    if index < 0 or index >= len(locators):
        return None
    locator = locators[index]
    end = locator.get("end_offset")
    if isinstance(end, int) and offset >= end:
        return None
    return {
        "kind": locator.get("kind", "chapter"),
        "index": locator.get("index"),
        "title": locator.get("title"),
        "line": locator.get("line"),
    }


def base_candidate(
    candidate_id: str,
    category: str,
    severity: str,
    message: str,
    record: dict[str, Any] | None,
    anchors: list[dict[str, Any]],
    proposed: str | None = None,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "candidate_id": candidate_id,
        "category": category,
        "severity": severity,
        "message": message,
        "line": record.get("line") if record else None,
        "current": record.get("text") if record else None,
        "report_only": True,
        "locator": locator,
        "anchors": anchors,
        "suggested_decision": {
            "candidate_id": candidate_id,
            "verdict": "uncertain",
            "confidence": None,
            "reason": "report-only in this release; no supported title compiler/apply/verify mutation path",
            "risk": severity,
        },
    }
    if proposed is not None:
        candidate["proposed"] = proposed
    return candidate


def bracket_issues(title: str) -> list[str]:
    issues: list[str] = []
    for opener, closer in PAIRED.items():
        if title.count(opener) != title.count(closer):
            issues.append(f"unbalanced {opener}{closer}")
    return issues


def next_non_empty(records: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    for record in records[index + 1 :]:
        if record["text"]:
            return record
    return None


def scan_text(
    text: str,
    parsed_structure: tuple[list[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = line_records(text)
    chapters, structure_report = (
        parse_chapters(text)
        if parsed_structure is None
        else parsed_structure
    )
    confidence = structure_report.get("structure_confidence", {})
    confidence_level = str(confidence.get("level", "medium"))
    low_confidence = confidence_level == "low"
    locators = structure_report.get("locators", [])
    if not isinstance(locators, list):
        locators = []
    candidates: list[dict[str, Any]] = []
    counter = 0
    suppressed_report_only = 0

    def new_id() -> str:
        nonlocal counter
        counter += 1
        return f"TT-{counter:04d}"

    for index, record in enumerate(records):
        heading = record["heading"]
        if not heading:
            continue

        title = record["text"]
        tail = str(heading.get("title_tail") or "")
        issues = bracket_issues(title)
        if issues:
            start = int(record["start"])
            candidates.append(
                base_candidate(
                    new_id(),
                    "unbalanced_brackets",
                    "medium",
                    "; ".join(issues),
                    record,
                    [anchor(text, start, int(record["end"]))],
                    locator=locator_lookup(locators, start),
                )
            )

        nxt = next_non_empty(records, index)
        if not tail and nxt and not nxt["heading"] and 1 <= len(nxt["text"]) <= 30:
            if not re.search(r"[。！？!?]$", nxt["text"]):
                start = int(record["start"])
                end = int(nxt["end"])
                proposed = f"{title} {nxt['text']}"
                candidates.append(
                    base_candidate(
                        new_id(),
                        "broken_title_line",
                        "medium",
                        "chapter number line is followed by a short possible title continuation",
                        record,
                        [anchor(text, start, end)],
                        proposed,
                        locator=locator_lookup(locators, start),
                    )
                )

        if tail and len(tail) > 35 and re.search(r"[。！？!?]", tail):
            start = int(record["start"])
            candidates.append(
                base_candidate(
                    new_id(),
                    "title_body_glued",
                    "high",
                    "chapter heading tail looks like body text glued to the title",
                    record,
                    [anchor(text, start, int(record["end"]))],
                    locator=locator_lookup(locators, start),
                )
            )

    duplicate_labels = structure_report.get("duplicate_labels", [])
    for label in duplicate_labels:
        candidates.append(
            base_candidate(
                new_id(),
                "duplicate_chapter_label",
                "high",
                f"duplicate chapter label detected: {label}",
                None,
                [],
            )
        )

    for item in structure_report.get("non_monotonic_numbers", []):
        candidates.append(
            base_candidate(
                new_id(),
                "non_monotonic_chapter_number",
                "high",
                f"chapter number is not increasing: {item}",
                None,
                [],
            )
        )

    heading_lines = {chapter.get("line") for chapter in chapters}
    for record in records:
        if not record["text"] or record["line"] in heading_lines:
            continue
        if CHAPTER_MENTION_RE.search(record["text"]):
            if low_confidence and suppressed_report_only >= MAX_LOW_CONFIDENCE_PSEUDO_TITLES:
                suppressed_report_only += 1
                continue
            start = int(record["start"])
            candidates.append(
                base_candidate(
                    new_id(),
                    "possible_pseudo_title",
                    "low",
                    "body line mentions a chapter marker; report-only in this release",
                    record,
                    [anchor(text, start, int(record["end"]))],
                    locator=locator_lookup(locators, start),
                )
            )
            if low_confidence:
                suppressed_report_only += 1

    summary = {
        "candidate_count": len(candidates),
        "chapter_count": len(chapters),
        "structure_confidence": confidence,
        "fallback_chunking": structure_report.get("fallback_chunking", {}),
        "execution_suggestions_enabled": False,
        "suppressed_report_only_count": max(0, suppressed_report_only - MAX_LOW_CONFIDENCE_PSEUDO_TITLES),
        "by_category": {},
    }
    for candidate in candidates:
        category = candidate["category"]
        summary["by_category"][category] = summary["by_category"].get(category, 0) + 1
    return candidates, summary


def run(workspace: Path, input_value: str, output_value: str) -> dict[str, Any]:
    with workspace_transaction_lock(workspace):
        return _run_locked(workspace, input_value, output_value)


def _run_locked(workspace: Path, input_value: str, output_value: str) -> dict[str, Any]:
    selected_input = choose_input(workspace, input_value)
    workspace, read_paths, write_paths = resolve_workspace_paths(
        workspace,
        reads={"input": selected_input},
        writes={
            "output": output_value,
            "report": "report/titles_scan_report.json",
            "structure": "meta/titles_structure.json",
        },
    )
    input_path = read_paths["input"]
    output_path = write_paths["output"]
    text = read_utf8(input_path)
    structure, structure_report = build_structure_artifact(text, input_path)
    chapters = structure["chapters"]
    if not isinstance(chapters, list):
        raise ValueError("generated title structure chapters are invalid")
    candidates, summary = scan_text(text, (chapters, structure_report))
    attach_candidate_fingerprints(candidates)
    attach_anchor_ids(candidates)
    scan_config: dict[str, Any] = {}
    structure_value = write_paths["structure"].relative_to(workspace).as_posix()
    with WorkspaceTransaction(workspace) as transaction:
        staged_structure = transaction.stage_path(write_paths["structure"])
        write_json(staged_structure, structure)
        load_bound_structure(input_path, staged_structure)
        scan_identity = build_scan_identity(
            "titles",
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
                "3_titles": (
                    "candidates_ready",
                    {
                        "input": str(input_path.relative_to(workspace)),
                        "structure": structure_value,
                        "candidates": str(output_path.relative_to(workspace)),
                        "report": "report/titles_scan_report.json",
                        "candidate_count": summary["candidate_count"],
                        **scan_identity,
                    },
                )
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan chapter title anomalies.")
    parser.add_argument("workspace", help="Path to the .cleanwork directory.")
    parser.add_argument("--input", default="auto")
    parser.add_argument("--output", default="candidates/titles.jsonl")
    args = parser.parse_args()
    report = run(Path(args.workspace).resolve(), args.input, args.output)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
