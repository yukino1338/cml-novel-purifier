from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from common import workspace_for_source, write_json, write_utf8


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
SANDBOX_MARKER = ".cml-novel-purifier-experiment.json"
SANDBOX_TOOL = "cml-novel-purifier-experiment"
DEFAULT_SANDBOX = Path(tempfile.gettempdir()) / "cml-novel-purifier-experiment-work"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_link_or_junction(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _reject_linked_path(path: Path) -> None:
    current = path.absolute()
    while True:
        if current.exists() and _is_link_or_junction(current):
            raise ValueError(f"experiment sandbox path contains a link or junction: {current}")
        if current.parent == current:
            break
        current = current.parent


def _reject_linked_tree(root: Path) -> None:
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in [*names, *files]:
            path = directory_path / name
            if _is_link_or_junction(path):
                raise ValueError(f"experiment sandbox contains a link or junction: {path}")


def _valid_run_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_sandbox_location(
    sample_dir: Path,
    sandbox: Path,
    *,
    project_root: Path,
    user_home: Path,
) -> tuple[Path, Path]:
    _reject_linked_path(sandbox)
    sample_dir = sample_dir.resolve()
    sandbox = sandbox.resolve()
    project_root = project_root.resolve()
    user_home = user_home.resolve()
    allowed_root = sandbox.parent

    if sandbox.parent == sandbox or sandbox == Path(sandbox.anchor):
        raise ValueError("experiment sandbox cannot be a filesystem root")
    if _is_relative_to(sample_dir, sandbox) or _is_relative_to(sandbox, sample_dir):
        raise ValueError("experiment sandbox cannot overlap the sample directory")
    if sandbox == project_root or _is_relative_to(project_root, sandbox):
        raise ValueError("experiment sandbox cannot be the project root or its ancestor")
    if sandbox == user_home or _is_relative_to(user_home, sandbox):
        raise ValueError("experiment sandbox cannot be the user home or its ancestor")
    if any(part.casefold().endswith(".cleanwork") for part in sandbox.parts):
        raise ValueError("experiment sandbox cannot be a workspace or live inside one")
    if sandbox == allowed_root or not _is_relative_to(sandbox, allowed_root):
        raise ValueError("experiment sandbox must be below its allowed experiment root")
    return sandbox, allowed_root


def _load_sandbox_marker(sandbox: Path, allowed_root: Path) -> dict[str, Any]:
    marker_path = sandbox / SANDBOX_MARKER
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("existing experiment sandbox has no valid ownership marker") from exc
    if (
        not isinstance(marker, dict)
        or marker.get("schema_version") != 1
        or marker.get("tool") != SANDBOX_TOOL
        or not _valid_run_id(marker.get("run_id"))
        or marker.get("sandbox") != str(sandbox)
        or marker.get("allowed_root") != str(allowed_root)
    ):
        raise ValueError("existing experiment sandbox ownership marker is invalid")
    return marker


def prepare_sandbox(
    sample_dir: Path,
    sandbox: Path,
    *,
    keep_existing: bool = False,
    project_root: Path = ROOT,
    user_home: Path | None = None,
) -> tuple[Path, str]:
    sample_dir = Path(sample_dir).resolve()
    if not sample_dir.is_dir():
        raise ValueError(f"sample directory does not exist: {sample_dir}")
    sandbox, allowed_root = _validate_sandbox_location(
        sample_dir,
        Path(sandbox),
        project_root=Path(project_root),
        user_home=Path(user_home) if user_home is not None else Path.home(),
    )

    if sandbox.exists():
        if not sandbox.is_dir():
            raise ValueError("experiment sandbox target is not a directory")
        marker = _load_sandbox_marker(sandbox, allowed_root)
        if keep_existing:
            return sandbox, str(marker["run_id"])
        _reject_linked_tree(sandbox)
        shutil.rmtree(sandbox)

    sandbox.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    write_json(
        sandbox / SANDBOX_MARKER,
        {
            "schema_version": 1,
            "tool": SANDBOX_TOOL,
            "run_id": run_id,
            "sandbox": str(sandbox),
            "allowed_root": str(allowed_root),
        },
    )
    return sandbox, run_id


def run_cmd(args: list[str], cwd: Path, timeout: int) -> tuple[int, float, str, str]:
    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - start
    return proc.returncode, elapsed, proc.stdout, proc.stderr


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def discover_samples(sample_dir: Path, max_files: int, min_size: int, max_size: int) -> list[Path]:
    files = [
        path
        for path in sample_dir.rglob("*.txt")
        if path.is_file() and min_size <= path.stat().st_size <= max_size
    ]
    files.sort(key=lambda path: (path.stat().st_size, str(path)))
    if max_files <= 0:
        return files
    if len(files) <= max_files:
        return files
    if max_files == 1:
        return [files[0]]
    indexes = [round(i * (len(files) - 1) / (max_files - 1)) for i in range(max_files)]
    return [files[index] for index in indexes]


def unique_copy_name(path: Path, index: int) -> str:
    suffix = path.suffix or ".txt"
    return f"sample-{index:02d}-{path.stem[:40]}{suffix}"


def run_sample(
    source: Path,
    index: int,
    sandbox: Path,
    timeout: int,
    max_candidates: int,
) -> dict[str, Any]:
    copied = sandbox / unique_copy_name(source, index)
    shutil.copyfile(source, copied)
    workspace = workspace_for_source(copied)
    commands = [
        ("preprocess", ["scripts/preprocess.py", str(copied)]),
        ("parse_structure", ["scripts/parse_structure.py", str(workspace)]),
        ("scan_ads", ["scripts/scan_ads.py", str(workspace), "--max-candidates", str(max_candidates)]),
        ("make_ad_decisions", ["scripts/make_ad_decisions.py", str(workspace)]),
        ("scan_titles", ["scripts/scan_titles.py", str(workspace)]),
        ("scan_blocked", ["scripts/scan_blocked.py", str(workspace), "--max-candidates", str(max_candidates)]),
    ]

    result: dict[str, Any] = {
        "source": str(source),
        "copied": str(copied),
        "workspace": str(workspace),
        "size_bytes": source.stat().st_size,
        "commands": [],
        "ok": True,
    }
    for name, args in commands:
        try:
            code, elapsed, stdout, stderr = run_cmd(args, ROOT, timeout)
        except subprocess.TimeoutExpired as exc:
            result["commands"].append(
                {
                    "name": name,
                    "code": "timeout",
                    "elapsed_seconds": timeout,
                    "stdout_tail": (exc.stdout or "")[-1000:] if isinstance(exc.stdout, str) else "",
                    "stderr_tail": (exc.stderr or "")[-1000:] if isinstance(exc.stderr, str) else "",
                }
            )
            result["ok"] = False
            break
        result["commands"].append(
            {
                "name": name,
                "code": code,
                "elapsed_seconds": round(elapsed, 3),
                "stdout_tail": stdout[-1000:],
                "stderr_tail": stderr[-1000:],
            }
        )
        if code != 0:
            result["ok"] = False
            break

    reports = workspace / "report"
    result["reports"] = {
        "preprocess": read_json(reports / "preprocess_report.json"),
        "structure": read_json(reports / "structure_report.json"),
        "ads": read_json(reports / "ads_scan_report.json"),
        "ad_decisions": read_json(reports / "ad_decision_draft_report.json"),
        "titles": read_json(reports / "titles_scan_report.json"),
        "blocked": read_json(reports / "blocked_scan_report.json"),
    }
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok_count = sum(1 for item in results if item.get("ok"))
    total_size = sum(int(item.get("size_bytes", 0)) for item in results)
    ads_counts = []
    ads_total_counts = []
    ads_page_counts = []
    ads_strong_deferred_counts = []
    ad_draft_delete_counts = []
    ad_draft_uncertain_counts = []
    title_counts = []
    blocked_counts = []
    chapter_counts = []
    low_confidence_count = 0
    fallback_chunking_count = 0
    max_hit = 0
    for item in results:
        reports = item.get("reports", {})
        ads_summary = reports.get("ads", {}).get("summary", {})
        ad_decisions = reports.get("ad_decisions", {})
        title_summary = reports.get("titles", {}).get("summary", {})
        blocked_summary = reports.get("blocked", {}).get("summary", {})
        structure = reports.get("structure", {})
        ads_counts.append(int(ads_summary.get("candidate_count", 0)))
        ads_total_counts.append(int(ads_summary.get("total_candidate_count", ads_summary.get("candidate_count", 0))))
        ads_page_counts.append(int(ads_summary.get("page_count", 0)))
        ads_strong_deferred_counts.append(int(ads_summary.get("strong_signal_deferred_count", 0)))
        ad_draft_delete_counts.append(int(ad_decisions.get("delete_count", 0)))
        ad_draft_uncertain_counts.append(int(ad_decisions.get("uncertain_count", 0)))
        title_counts.append(int(title_summary.get("candidate_count", 0)))
        blocked_counts.append(int(blocked_summary.get("candidate_count", 0)))
        chapter_counts.append(int(structure.get("chapter_count", 0)))
        confidence = structure.get("structure_confidence", {})
        if isinstance(confidence, dict) and confidence.get("level") == "low":
            low_confidence_count += 1
        fallback = structure.get("fallback_chunking", {})
        if isinstance(fallback, dict) and fallback.get("enabled"):
            fallback_chunking_count += 1
        if ads_summary.get("max_candidates_reached"):
            max_hit += 1
    return {
        "sample_count": len(results),
        "ok_count": ok_count,
        "failed_count": len(results) - ok_count,
        "total_size_bytes": total_size,
        "ads_candidate_total": sum(ads_counts),
        "ads_total_candidate_total": sum(ads_total_counts),
        "ads_page_total": sum(ads_page_counts),
        "ads_strong_deferred_total": sum(ads_strong_deferred_counts),
        "ad_draft_delete_total": sum(ad_draft_delete_counts),
        "ad_draft_uncertain_total": sum(ad_draft_uncertain_counts),
        "title_candidate_total": sum(title_counts),
        "blocked_candidate_total": sum(blocked_counts),
        "ads_candidate_max": max(ads_counts or [0]),
        "ads_total_candidate_max": max(ads_total_counts or [0]),
        "ads_page_max": max(ads_page_counts or [0]),
        "title_candidate_max": max(title_counts or [0]),
        "blocked_candidate_max": max(blocked_counts or [0]),
        "chapter_count_min": min(chapter_counts or [0]),
        "chapter_count_max": max(chapter_counts or [0]),
        "structure_low_confidence_count": low_confidence_count,
        "fallback_chunking_count": fallback_chunking_count,
        "ads_max_candidates_reached_count": max_hit,
    }


def recommendations(summary: dict[str, Any], results: list[dict[str, Any]]) -> list[str]:
    recs: list[str] = []
    if summary["failed_count"]:
        recs.append("Add failure-focused fixtures for files that failed in the batch before expanding features.")
    if summary["ads_max_candidates_reached_count"]:
        recs.append("Ad candidates exceeded the first-page size on some files; review candidates/ads_pages for the complete paginated set.")
    if summary.get("ads_strong_deferred_total", 0):
        recs.append("Some strong ad signals are beyond page 1; increase --max-candidates or strong-signal quotas when first-pass review must include every strong hit.")
    if summary["blocked_candidate_total"] > summary["sample_count"] * 200:
        recs.append("Blocked-word scanning is noisy on some files; refine deterministic report-only evaluation fixtures and allowlists before drawing precision conclusions.")
    if summary.get("structure_low_confidence_count", 0):
        recs.append("Some samples have low structure confidence; review fallback locator coverage and keep title fixes report-only.")
    if not recs:
        recs.append("The deterministic report-only evaluation stages completed on the sampled files; next improvements should focus on measured precision and candidate-governance evidence.")
    large_ads = [
        Path(item["source"]).name
        for item in results
        if int(item.get("reports", {}).get("ads", {}).get("summary", {}).get("candidate_count", 0)) > 50
    ]
    if large_ads:
        recs.append("Prioritize manual review and decision batching for high-ad samples: " + ", ".join(large_ads[:5]))
    return recs


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report["summary"]
    lines = [
        "# Experiment Report",
        "",
        "## Summary",
        "",
        f"- Samples: {summary['sample_count']}",
        f"- OK: {summary['ok_count']}",
        f"- Failed: {summary['failed_count']}",
        f"- Total size: {summary['total_size_bytes']} bytes",
        f"- Ads candidates: {summary['ads_candidate_total']} total, max {summary['ads_candidate_max']}",
        f"- Ads full candidate pool: {summary.get('ads_total_candidate_total', summary['ads_candidate_total'])} total, max {summary.get('ads_total_candidate_max', summary['ads_candidate_max'])}",
        f"- Ads pages: {summary.get('ads_page_total', 0)} total, max {summary.get('ads_page_max', 0)}",
        f"- Ad decision drafts: delete {summary.get('ad_draft_delete_total', 0)}, uncertain {summary.get('ad_draft_uncertain_total', 0)}",
        f"- Title candidates: {summary['title_candidate_total']} total, max {summary['title_candidate_max']}",
        f"- Blocked candidates: {summary['blocked_candidate_total']} total, max {summary['blocked_candidate_max']}",
        f"- Chapter count range: {summary['chapter_count_min']} - {summary['chapter_count_max']}",
        f"- Low-confidence structures: {summary.get('structure_low_confidence_count', 0)}",
        f"- Fallback chunking: {summary.get('fallback_chunking_count', 0)}",
        "",
        "## Recommendations",
        "",
    ]
    lines.extend(f"- {item}" for item in report["recommendations"])
    lines.extend(["", "## Samples", ""])
    for item in report["results"]:
        ads_summary = item.get("reports", {}).get("ads", {}).get("summary", {})
        ad_decisions = item.get("reports", {}).get("ad_decisions", {})
        ads = ads_summary.get("candidate_count", 0)
        ads_total = ads_summary.get("total_candidate_count", ads)
        ads_pages = ads_summary.get("page_count", 0)
        ad_delete = ad_decisions.get("delete_count", 0)
        ad_uncertain = ad_decisions.get("uncertain_count", 0)
        titles = item.get("reports", {}).get("titles", {}).get("summary", {}).get("candidate_count", 0)
        blocked = item.get("reports", {}).get("blocked", {}).get("summary", {}).get("candidate_count", 0)
        structure = item.get("reports", {}).get("structure", {})
        confidence = structure.get("structure_confidence", {})
        confidence_level = confidence.get("level") if isinstance(confidence, dict) else "unknown"
        lines.append(f"- `{Path(item['source']).name}`: ok={item.get('ok')}, structure={confidence_level}, ads={ads}/{ads_total}, ads_pages={ads_pages}, ad_drafts=delete:{ad_delete}/uncertain:{ad_uncertain}, titles={titles}, blocked={blocked}")
    write_utf8(path, "\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run non-destructive batch experiments against novel samples.")
    parser.add_argument("sample_dir", help="Directory containing .txt samples.")
    parser.add_argument("--sandbox", default=str(DEFAULT_SANDBOX))
    parser.add_argument("--max-files", type=int, default=8)
    parser.add_argument("--min-size", type=int, default=1)
    parser.add_argument("--max-size", type=int, default=40_000_000)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--max-candidates", type=int, default=120)
    parser.add_argument("--keep-sandbox", action="store_true")
    args = parser.parse_args()

    sample_dir = Path(args.sample_dir).resolve()
    sandbox, _run_id = prepare_sandbox(
        sample_dir,
        Path(args.sandbox),
        keep_existing=args.keep_sandbox,
    )

    samples = discover_samples(sample_dir, args.max_files, args.min_size, args.max_size)
    results = [
        run_sample(sample, index + 1, sandbox, args.timeout, args.max_candidates)
        for index, sample in enumerate(samples)
    ]
    summary = summarize(results)
    report = {
        "sample_dir": str(sample_dir),
        "sandbox": str(sandbox),
        "summary": summary,
        "recommendations": recommendations(summary, results),
        "results": results,
    }
    output_json = sandbox / "experiment_report.json"
    output_md = sandbox / "experiment_report.md"
    write_json(output_json, report)
    write_markdown(output_md, report)
    print(json.dumps({"report": str(output_json), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
