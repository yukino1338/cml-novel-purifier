from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OVERALL_LINE_MIN = 90.0
CRITICAL_MODULE_THRESHOLDS = {
    "scripts/common.py": (95.0, 90.0),
    "scripts/apply_decisions.py": (95.0, 90.0),
    "scripts/scan_identity.py": (95.0, 90.0),
    "scripts/finalize_ad_decisions.py": (95.0, 90.0),
    "scripts/verify.py": (95.0, 90.0),
    "scripts/export_outputs.py": (95.0, 90.0),
    "scripts/rollback.py": (95.0, 90.0),
    "scripts/scan_ads.py": (90.0, 85.0),
    "scripts/make_ad_decisions.py": (75.0, 65.0),
    "scripts/build_review_html.py": (85.0, 75.0),
}
CRITICAL_MODULES = tuple(CRITICAL_MODULE_THRESHOLDS)


@dataclass(frozen=True)
class CoverageResult:
    line_percent: float
    errors: tuple[str, ...]


def load_report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("coverage JSON root must be an object")
    return data


def normalize_source_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("\\", "/")
    folded = normalized.casefold()
    if folded.startswith("scripts/"):
        return "scripts/" + normalized[len("scripts/") :]
    marker = "/scripts/"
    index = folded.rfind(marker)
    if index < 0:
        return None
    return "scripts/" + normalized[index + len(marker) :]


def percentage(covered: int, total: int) -> float:
    return 100.0 if total == 0 else covered * 100.0 / total


def summary_counts(summary: object, path: str) -> tuple[int, int, int, int]:
    if not isinstance(summary, dict):
        raise ValueError(f"{path}: summary must be an object")
    values = []
    for field in ("covered_lines", "num_statements", "covered_branches", "num_branches"):
        value = summary.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{path}: {field} must be a non-negative integer")
        values.append(value)
    covered_lines, statements, covered_branches, branches = values
    if covered_lines > statements:
        raise ValueError(f"{path}: covered_lines exceeds num_statements")
    if covered_branches > branches:
        raise ValueError(f"{path}: covered_branches exceeds num_branches")
    return covered_lines, statements, covered_branches, branches


def expected_sources(root: Path) -> set[str]:
    scripts = root / "scripts"
    if not scripts.is_dir():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in scripts.rglob("*.py")
        if path.is_file()
    }


def evaluate_coverage(report: dict[str, Any], root: Path) -> CoverageResult:
    errors: list[str] = []
    meta = report.get("meta")
    if not isinstance(meta, dict) or meta.get("branch_coverage") is not True:
        errors.append("coverage JSON must be generated with branch coverage enabled")
    files = report.get("files")
    if not isinstance(files, dict):
        errors.append("coverage JSON files must be an object")
        return CoverageResult(0.0, tuple(errors))

    expected = expected_sources(root)
    if not expected:
        errors.append("scripts directory contains no Python sources")
        return CoverageResult(0.0, tuple(errors))

    counts: dict[str, tuple[int, int, int, int]] = {}
    for raw_path, entry in files.items():
        path = normalize_source_path(raw_path)
        if path is None:
            continue
        if path in counts:
            errors.append(f"duplicate coverage data for {path}")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{path}: file record must be an object")
            continue
        try:
            counts[path] = summary_counts(entry.get("summary"), path)
        except ValueError as exc:
            errors.append(str(exc))

    missing = sorted(expected - counts.keys())
    if missing:
        errors.append("missing coverage data: " + ", ".join(missing))
    unexpected = sorted(counts.keys() - expected)
    if unexpected:
        errors.append("coverage data references unknown sources: " + ", ".join(unexpected))

    covered_lines = sum(value[0] for path, value in counts.items() if path in expected)
    statements = sum(value[1] for path, value in counts.items() if path in expected)
    line_percent = percentage(covered_lines, statements)
    if line_percent < OVERALL_LINE_MIN:
        errors.append(
            f"all scripts line coverage {line_percent:.2f}% is below {OVERALL_LINE_MIN:.2f}%"
        )

    for path, (line_min, branch_min) in CRITICAL_MODULE_THRESHOLDS.items():
        if path not in expected:
            errors.append(f"critical source is missing: {path}")
            continue
        values = counts.get(path)
        if values is None:
            continue
        module_line = percentage(values[0], values[1])
        module_branch = percentage(values[2], values[3])
        if module_line < line_min:
            errors.append(
                f"{path} line coverage {module_line:.2f}% is below {line_min:.2f}%"
            )
        if values[3] == 0:
            errors.append(f"{path} reports no branches")
        elif module_branch < branch_min:
            errors.append(
                f"{path} branch coverage {module_branch:.2f}% is below "
                f"{branch_min:.2f}%"
            )

    return CoverageResult(line_percent, tuple(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description="Enforce repository coverage thresholds.")
    parser.add_argument("coverage_json", nargs="?", type=Path, default=Path("coverage.json"))
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to this script's parent repository)",
    )
    args = parser.parse_args()
    try:
        report = load_report(args.coverage_json)
        result = evaluate_coverage(report, args.root.resolve())
    except (OSError, ValueError) as exc:
        print(f"coverage gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if result.errors:
        print("coverage gate failed:", file=sys.stderr)
        for error in result.errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"coverage gate passed: all scripts line coverage {result.line_percent:.2f}%")


if __name__ == "__main__":
    main()
