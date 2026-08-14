from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import scan_ads
from common import write_json
from scan_identity import (
    attach_anchor_ids,
    attach_candidate_fingerprints,
    candidate_set_sha256,
)


SCHEMA_VERSION = 1
GENERATOR_VERSION = 1
DEFAULT_SEED = 20260715
WORKLOADS = ("narrative", "high-density")
SIZE_PROFILES: dict[str, tuple[tuple[str, int], ...]] = {
    "ci": (("100kb", 100 * 1024), ("1mb", 1024**2)),
    "full": (
        ("100kb", 100 * 1024),
        ("5mb", 5 * 1024**2),
        ("20mb", 20 * 1024**2),
        ("40mb", 40 * 1024**2),
    ),
}
MAX_REGRESSION = 0.15
MAX_40MB_SECONDS = 60.0
MAX_40MB_MEMORY_BYTES = 1024**3
MAX_SCALING_RATIO = 2.6


def _alpha_token(value: int, seed: int, width: int = 12) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz"
    digest = hashlib.blake2s(f"{seed}:{value}".encode("ascii")).digest()
    return "".join(alphabet[byte % len(alphabet)] for byte in digest[:width])


def _paragraph(index: int, workload: str, seed: int) -> str:
    token = _alpha_token(index, seed)
    narrative = (
        f"叙事片段{token}。人物沿着河岸观察云影，整理旧纸页后继续交谈。"
        "风穿过庭院，灯光落在桌面，脚步声与远处钟声交替出现。"
    )
    if workload == "high-density" and index % 4 == 0:
        base = (
            f"站外提示{token}：请访问 https://{token}.example.com/read "
            "获取最新章节和电子书下载地址。"
        )
    elif workload == "narrative" and index % 128 == 0:
        base = (
            f"来源提示{token}：本文转自 https://{token}.example.com/archive ，"
            "仅供学习交流。"
        )
    else:
        base = narrative
    padding = "人物仍在当前场景中观察、记录并核对纸页，没有离开故事语境。"
    # Keep normalized paragraphs inside scan_ads' L2 eligibility window so the
    # deep ``all`` profile exercises near-repeat feature extraction as intended.
    while len(base) + len(padding) <= 220:
        base += padding
    return base + "\n"


def _chapter_locators(text: str, starts: list[tuple[int, str]]) -> list[dict[str, Any]]:
    locators: list[dict[str, Any]] = []
    for index, (start, title) in enumerate(starts, 1):
        end = starts[index][0] if index < len(starts) else len(text)
        locators.append(
            {
                "kind": "chapter",
                "index": index,
                "title": title,
                "start_offset": start,
                "end_offset": end,
            }
        )
    return locators


def generate_case(target_bytes: int, workload: str, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    if not isinstance(target_bytes, int) or isinstance(target_bytes, bool) or target_bytes < 1024:
        raise ValueError("target_bytes must be an integer of at least 1024")
    if workload not in WORKLOADS:
        raise ValueError(f"workload must be one of: {', '.join(WORKLOADS)}")

    chunks: list[str] = []
    chapter_starts: list[tuple[int, str]] = []
    byte_count = 0
    char_count = 0
    paragraph_index = 0
    chapter_index = 0
    while True:
        if paragraph_index % 64 == 0:
            chapter_index += 1
            title = f"第{chapter_index}章 匿名性能样本{_alpha_token(chapter_index, seed, 6)}"
            chunk = title + "\n"
            is_heading = True
        else:
            chunk = _paragraph(paragraph_index, workload, seed)
            is_heading = False
        encoded_size = len(chunk.encode("utf-8"))
        if byte_count + encoded_size > target_bytes:
            break
        if is_heading:
            chapter_starts.append((char_count, chunk.rstrip("\n")))
        chunks.append(chunk)
        byte_count += encoded_size
        char_count += len(chunk)
        paragraph_index += 1

    # ASCII punctuation gives an exact byte target without creating a candidate block.
    chunks.append("~" * (target_bytes - byte_count))
    text = "".join(chunks)
    if not chapter_starts:
        raise ValueError("target_bytes is too small for a chapter heading")
    return {
        "text": text,
        "chapters": _chapter_locators(text, chapter_starts),
    }


def candidate_hash(candidates: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"[")
    for index, candidate in enumerate(candidates):
        if index:
            digest.update(b",")
        digest.update(
            json.dumps(
                candidate,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    digest.update(b"]")
    return digest.hexdigest()


def machine_metadata() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
    }


def environment_id(metadata: dict[str, Any]) -> str:
    payload = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def peak_rss_bytes() -> int:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        )
        get_process_memory_info.restype = wintypes.BOOL
        handle = get_current_process()
        ok = get_process_memory_info(
            handle,
            ctypes.byref(counters),
            counters.cb,
        )
        if not ok:
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.PeakWorkingSetSize)

    import resource

    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024


def measure_once(size_bytes: int, workload: str, scope: str, seed: int) -> dict[str, Any]:
    generated = generate_case(size_bytes, workload, seed)
    text = str(generated["text"])
    chapters = list(generated["chapters"])
    input_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    scan_ads.feature_bits.cache_clear()
    started = perf_counter()
    candidates, summary = scan_ads.scan_candidates(
        text,
        chapters=chapters,
        near_scan_scope=scope,
    )
    identity_started = perf_counter()
    attach_candidate_fingerprints(candidates)
    attach_anchor_ids(candidates)
    identity_seconds = perf_counter() - identity_started
    elapsed = perf_counter() - started
    ordered_hash = candidate_hash(candidates)
    return {
        "input_sha256": input_sha256,
        "candidate_sha256": ordered_hash,
        "candidate_set_sha256": candidate_set_sha256(candidates),
        "candidate_count": len(candidates),
        "elapsed_seconds": round(elapsed, 6),
        "peak_memory_bytes": peak_rss_bytes(),
        "identity_seconds": round(identity_seconds, 6),
        "scanner_timings_seconds": summary["performance"]["timings_seconds"],
    }


def _worker_command(size_bytes: int, workload: str, scope: str, seed: int) -> dict[str, Any]:
    process = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_worker",
            str(size_bytes),
            workload,
            scope,
            str(seed),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=3600,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "unknown worker failure"
        raise RuntimeError(f"benchmark worker failed: {detail}")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("benchmark worker returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("benchmark worker returned a non-object result")
    return result


def measure_case(
    label: str,
    size_bytes: int,
    workload: str,
    scope: str,
    *,
    repeat: int,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    if scope not in {"boundary", "all"}:
        raise ValueError("scope must be 'boundary' or 'all'")
    if not isinstance(repeat, int) or isinstance(repeat, bool) or not 1 <= repeat <= 9:
        raise ValueError("repeat must be an integer from 1 to 9")

    runs: list[dict[str, Any]] = []
    expected_input_hash: str | None = None
    expected_candidate_hash: str | None = None
    expected_candidate_set_hash: str | None = None
    expected_candidate_count: int | None = None

    for run_index in range(1, repeat + 1):
        measured = _worker_command(size_bytes, workload, scope, seed)
        input_digest = str(measured["input_sha256"])
        digest = str(measured["candidate_sha256"])
        set_digest = str(measured["candidate_set_sha256"])
        candidate_count = int(measured["candidate_count"])
        if expected_candidate_hash is None:
            expected_input_hash = input_digest
            expected_candidate_hash = digest
            expected_candidate_set_hash = set_digest
            expected_candidate_count = candidate_count
        elif (
            input_digest != expected_input_hash
            or digest != expected_candidate_hash
            or set_digest != expected_candidate_set_hash
            or candidate_count != expected_candidate_count
        ):
            raise RuntimeError("candidate semantics changed between benchmark repetitions")
        runs.append(
            {
                "run": run_index,
                "elapsed_seconds": float(measured["elapsed_seconds"]),
                "peak_memory_bytes": int(measured["peak_memory_bytes"]),
                "identity_seconds": float(measured["identity_seconds"]),
                "scanner_timings_seconds": measured["scanner_timings_seconds"],
            }
        )

    elapsed_values = [float(item["elapsed_seconds"]) for item in runs]
    memory_values = [int(item["peak_memory_bytes"]) for item in runs]
    return {
        "case_id": f"{label}:{workload}:{scope}",
        "size_label": label,
        "size_bytes": size_bytes,
        "workload": workload,
        "scope": scope,
        "seed": seed,
        "generator_version": GENERATOR_VERSION,
        "input_sha256": expected_input_hash,
        "candidate_sha256": expected_candidate_hash,
        "candidate_set_sha256": expected_candidate_set_hash,
        "candidate_count": expected_candidate_count,
        "candidate_count_per_mib": round(
            int(expected_candidate_count or 0) / (size_bytes / 1024**2),
            3,
        ),
        "median_elapsed_seconds": round(statistics.median(elapsed_values), 6),
        "median_peak_memory_bytes": int(statistics.median(memory_values)),
        "runs": runs,
    }


def evaluate_objectives(records: list[dict[str, Any]]) -> dict[str, Any]:
    boundary = {
        (str(item.get("workload")), int(item.get("size_bytes", 0))): item
        for item in records
        if item.get("scope") == "boundary"
    }
    forty_mb = 40 * 1024**2
    twenty_mb = 20 * 1024**2
    present_workloads = [workload for workload in WORKLOADS if (workload, forty_mb) in boundary]
    if not present_workloads:
        return {"status": "not-applicable", "violations": []}

    violations: list[str] = []
    for workload in present_workloads:
        record = boundary[(workload, forty_mb)]
        elapsed = float(record.get("median_elapsed_seconds", 0))
        memory = int(record.get("median_peak_memory_bytes", 0))
        if elapsed > MAX_40MB_SECONDS:
            violations.append(f"{workload} 40MB boundary scan exceeded 60 seconds: {elapsed:.3f}s")
        if memory > MAX_40MB_MEMORY_BYTES:
            violations.append(f"{workload} 40MB boundary scan exceeded 1 GiB peak memory: {memory} bytes")
        smaller = boundary.get((workload, twenty_mb))
        if smaller is None:
            violations.append(f"{workload} is missing the 20MB boundary scaling sample")
            continue
        smaller_elapsed = float(smaller.get("median_elapsed_seconds", 0))
        ratio = float("inf") if smaller_elapsed <= 0 else elapsed / smaller_elapsed
        if ratio > MAX_SCALING_RATIO:
            violations.append(f"{workload} T(40MB)/T(20MB) exceeded 2.6: {ratio:.3f}")
    return {"status": "failed" if violations else "passed", "violations": violations}


def compare_baseline(
    current: dict[str, Any],
    baseline: dict[str, Any],
    max_regression: float = MAX_REGRESSION,
) -> dict[str, Any]:
    violations: list[str] = []
    if baseline.get("schema_version") != SCHEMA_VERSION:
        violations.append("frozen baseline schema version is unsupported")
    if baseline.get("kind") != "frozen-scan-benchmark-baseline":
        violations.append("benchmark comparison requires a frozen baseline")
    if current.get("measurement") != baseline.get("measurement"):
        violations.append("benchmark measurement scope differs from the frozen baseline")

    raw_baseline_records = baseline.get("records", [])
    if not isinstance(raw_baseline_records, list):
        violations.append("frozen baseline records must be a list")
        raw_baseline_records = []
    baseline_records = {
        str(item.get("case_id")): item
        for item in raw_baseline_records
        if isinstance(item, dict)
    }
    if len(baseline_records) != len(raw_baseline_records):
        violations.append("frozen baseline has invalid or duplicate case IDs")
    same_environment = current.get("environment_id") == baseline.get("environment_id")
    timing_comparable = (
        same_environment
        and current.get("profile") == "full"
        and baseline.get("profile") == "full"
    )
    for item in current.get("records", []):
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("case_id"))
        frozen = baseline_records.get(case_id)
        if frozen is None:
            violations.append(f"{case_id} has no matching frozen baseline")
            continue
        if item.get("input_sha256") != frozen.get("input_sha256"):
            violations.append(f"{case_id} input hash differs from the frozen baseline")
        if item.get("candidate_sha256") != frozen.get("candidate_sha256"):
            violations.append(f"{case_id} candidate hash differs from the frozen baseline")
        if item.get("candidate_set_sha256") != frozen.get("candidate_set_sha256"):
            violations.append(f"{case_id} candidate-set hash differs from the frozen baseline")
        if item.get("generator_version") != frozen.get("generator_version"):
            violations.append(f"{case_id} generator version differs from the frozen baseline")
        frozen_elapsed = float(frozen.get("median_elapsed_seconds", 0))
        current_elapsed = float(item.get("median_elapsed_seconds", 0))
        if not timing_comparable:
            continue
        if frozen_elapsed <= 0:
            violations.append(f"{case_id} frozen elapsed time is invalid")
        elif current_elapsed > frozen_elapsed * (1 + max_regression):
            violations.append(
                f"{case_id} median elapsed time regressed by more than "
                f"{max_regression:.0%} "
                f"({frozen_elapsed:.6f}s -> {current_elapsed:.6f}s)"
            )
    if violations:
        return {"status": "failed", "violations": violations}
    if not same_environment:
        return {
            "status": "not-comparable",
            "reason": "semantic hashes passed; timing skipped because machine/Python differs",
            "violations": [],
        }
    if not timing_comparable:
        return {
            "status": "semantic-only",
            "reason": "CI profile hashes passed; timing is enforced only by the full fixed-environment baseline",
            "violations": [],
        }
    return {"status": "passed", "violations": []}


def validate_frozen_report(report: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    profile = str(report.get("profile"))
    if profile not in SIZE_PROFILES:
        return ["frozen baseline has an unknown size profile"]
    if int(report.get("repeat", 0)) < 3:
        violations.append("frozen baseline requires at least three repetitions")
    records = report.get("records", [])
    if not isinstance(records, list):
        return [*violations, "frozen baseline records must be a list"]
    expected = {
        f"{label}:{workload}:{scope}"
        for label, _size in SIZE_PROFILES[profile]
        for workload in WORKLOADS
        for scope in ("boundary", "all")
    }
    found = {
        str(item.get("case_id"))
        for item in records
        if isinstance(item, dict)
    }
    if len(found) != len(records) or found != expected:
        violations.append("frozen baseline must contain the complete two-workload/two-scope matrix")
    for item in records:
        if not isinstance(item, dict):
            continue
        if item.get("generator_version") != GENERATOR_VERSION:
            violations.append(f"{item.get('case_id')} has an unsupported generator version")
        for field in ("input_sha256", "candidate_sha256", "candidate_set_sha256"):
            value = item.get(field)
            if not isinstance(value, str) or len(value) != 64:
                violations.append(f"{item.get('case_id')} has an invalid {field}")
    if profile == "full" and report.get("objectives", {}).get("status") != "passed":
        violations.append("full frozen baseline does not satisfy the boundary objectives")
    return violations


def run_benchmark(
    profile: str,
    scopes: tuple[str, ...],
    workloads: tuple[str, ...],
    repeat: int,
    seed: int,
) -> dict[str, Any]:
    if profile not in SIZE_PROFILES:
        raise ValueError(f"unknown benchmark profile: {profile}")
    metadata = machine_metadata()
    records = [
        measure_case(label, size, workload, scope, repeat=repeat, seed=seed)
        for label, size in SIZE_PROFILES[profile]
        for workload in workloads
        for scope in scopes
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "scan-benchmark",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "repeat": repeat,
        "measurement": {
            "elapsed": "perf_counter around scan_candidates plus production fingerprints and anchor IDs",
            "peak_memory": "isolated worker process peak RSS including generation, scan, identity, and hashing",
            "candidate_hash": "ordered canonical JSON of identified candidates; excluded from timing",
        },
        "workload_definitions": {
            "narrative": "unique narrative paragraphs with one explicit external marker per 128 paragraphs",
            "high-density": "unique narrative paragraphs with one explicit external marker per 4 paragraphs",
        },
        "machine": metadata,
        "environment_id": environment_id(metadata),
        "records": records,
        "objectives": evaluate_objectives(records),
    }


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("benchmark JSON root must be an object")
    return value


def baseline_gate_failed(
    comparison: object, *, require_comparable: bool
) -> bool:
    status = comparison.get("status") if isinstance(comparison, dict) else None
    return status == "failed" or (require_comparable and status != "passed")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run deterministic ad-scan performance benchmarks without changing scanner semantics."
    )
    parser.add_argument("--profile", choices=tuple(SIZE_PROFILES), default="ci")
    parser.add_argument("--scope", choices=("boundary", "all", "both"), default="boundary")
    parser.add_argument("--workload", choices=(*WORKLOADS, "both"), default="both")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--require-comparable-baseline",
        action="store_true",
        help="fail unless the frozen baseline timing comparison is fully comparable and passed",
    )
    parser.add_argument("--freeze-baseline", type=Path)
    args = parser.parse_args()

    if args.baseline and args.freeze_baseline:
        parser.error("--baseline and --freeze-baseline cannot be used together")
    if args.require_comparable_baseline and not args.baseline:
        parser.error("--require-comparable-baseline requires --baseline")
    if args.freeze_baseline and (
        args.scope != "both" or args.workload != "both" or args.repeat < 3
    ):
        parser.error("--freeze-baseline requires --scope both --workload both --repeat >= 3")
    scopes = ("boundary", "all") if args.scope == "both" else (args.scope,)
    workloads = WORKLOADS if args.workload == "both" else (args.workload,)
    try:
        report = run_benchmark(args.profile, scopes, workloads, args.repeat, args.seed)
    except ValueError as exc:
        parser.error(str(exc))

    if args.baseline:
        report["baseline_comparison"] = compare_baseline(report, _load_json(args.baseline))
    if args.freeze_baseline:
        violations = validate_frozen_report(report)
        if violations:
            parser.error("; ".join(violations))
        report["kind"] = "frozen-scan-benchmark-baseline"
        write_json(args.freeze_baseline.resolve(), report)
    if args.output:
        write_json(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    failed = report["objectives"]["status"] == "failed"
    comparison = report.get("baseline_comparison", {})
    if baseline_gate_failed(
        comparison, require_comparable=args.require_comparable_baseline
    ):
        failed = True
    if failed:
        sys.exit(1)


def worker_main(values: list[str]) -> None:
    if len(values) != 4:
        raise SystemExit("benchmark worker requires size, workload, scope, and seed")
    size_bytes, workload, scope, seed = values
    result = measure_once(int(size_bytes), workload, scope, int(seed))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--_worker"]:
        worker_main(sys.argv[2:])
    else:
        main()
