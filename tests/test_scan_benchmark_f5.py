from __future__ import annotations

import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_scan  # noqa: E402


class ScanBenchmarkF5Tests(unittest.TestCase):
    def test_committed_ci_baseline_is_complete_and_semantically_frozen(self) -> None:
        baseline = json.loads(
            (ROOT / "tests" / "performance" / "scan_baseline_ci.json").read_text(encoding="utf-8")
        )

        self.assertEqual(baseline["kind"], "frozen-scan-benchmark-baseline")
        self.assertEqual(baseline["profile"], "ci")
        self.assertEqual(baseline["repeat"], 3)
        self.assertEqual(benchmark_scan.validate_frozen_report(baseline), [])

    def test_profiles_expose_the_documented_full_sizes_and_small_ci_sizes(self) -> None:
        self.assertEqual(
            benchmark_scan.SIZE_PROFILES["full"],
            (
                ("100kb", 100 * 1024),
                ("5mb", 5 * 1024**2),
                ("20mb", 20 * 1024**2),
                ("40mb", 40 * 1024**2),
            ),
        )
        self.assertTrue(
            all(
                size <= 1024**2
                for _label, size in benchmark_scan.SIZE_PROFILES["ci"]
            )
        )

    def test_generated_inputs_are_exact_deterministic_and_structurally_bound(self) -> None:
        for workload in benchmark_scan.WORKLOADS:
            with self.subTest(workload=workload):
                first = benchmark_scan.generate_case(100 * 1024, workload, seed=20260715)
                second = benchmark_scan.generate_case(100 * 1024, workload, seed=20260715)
                self.assertEqual(len(first["text"].encode("utf-8")), 100 * 1024)
                self.assertEqual(first, second)
                self.assertEqual(first["chapters"][0]["start_offset"], 0)
                self.assertEqual(first["chapters"][-1]["end_offset"], len(first["text"]))

    def test_high_density_fixture_surfaces_more_candidates_than_narrative(self) -> None:
        narrative = benchmark_scan.measure_case(
            "test",
            100 * 1024,
            "narrative",
            "boundary",
            repeat=1,
            seed=benchmark_scan.DEFAULT_SEED,
        )
        dense = benchmark_scan.measure_case(
            "test",
            100 * 1024,
            "high-density",
            "boundary",
            repeat=1,
            seed=benchmark_scan.DEFAULT_SEED,
        )
        self.assertGreater(dense["candidate_count"], narrative["candidate_count"])
        self.assertEqual(
            narrative["candidate_sha256"],
            "c97abae851fac67e63b3d468df5c36b621f69680a8b7a4bdadd28ae7c2e70ac1",
        )
        self.assertEqual(
            dense["candidate_sha256"],
            "8cfb57fc85d3d9f66dfa2c6ca598de7ae325de8d2d0eb5e455422b8b83406688",
        )
        for record in (narrative, dense):
            self.assertEqual(len(record["input_sha256"]), 64)
            self.assertEqual(len(record["candidate_sha256"]), 64)
            self.assertEqual(len(record["candidate_set_sha256"]), 64)
            self.assertGreaterEqual(record["median_elapsed_seconds"], 0)
            self.assertGreater(record["median_peak_memory_bytes"], 0)
            self.assertGreater(record["candidate_count_per_mib"], 0)
            self.assertEqual(len(record["runs"]), 1)

    def test_baseline_comparison_is_environment_and_semantics_bound(self) -> None:
        current = {
            "schema_version": benchmark_scan.SCHEMA_VERSION,
            "kind": "scan-benchmark",
            "profile": "full",
            "measurement": {"scope": "test"},
            "environment_id": "fixed-environment",
            "records": [
                {
                    "case_id": "100kb:narrative:boundary",
                    "generator_version": benchmark_scan.GENERATOR_VERSION,
                    "input_sha256": "a" * 64,
                    "candidate_sha256": "b" * 64,
                    "candidate_set_sha256": "d" * 64,
                    "median_elapsed_seconds": 1.16,
                }
            ],
        }
        baseline = copy.deepcopy(current)
        baseline["kind"] = "frozen-scan-benchmark-baseline"
        baseline["records"][0]["median_elapsed_seconds"] = 1.0

        comparison = benchmark_scan.compare_baseline(current, baseline)
        self.assertEqual(comparison["status"], "failed")
        self.assertIn("15%", comparison["violations"][0])

        other_machine = copy.deepcopy(current)
        other_machine["environment_id"] = "another-environment"
        comparison = benchmark_scan.compare_baseline(other_machine, baseline)
        self.assertEqual(comparison["status"], "not-comparable")

        changed_semantics = copy.deepcopy(current)
        changed_semantics["records"][0]["candidate_sha256"] = "c" * 64
        changed_semantics["records"][0]["median_elapsed_seconds"] = 1.0
        comparison = benchmark_scan.compare_baseline(changed_semantics, baseline)
        self.assertEqual(comparison["status"], "failed")
        self.assertTrue(any("candidate hash" in item for item in comparison["violations"]))

        cross_machine_change = copy.deepcopy(changed_semantics)
        cross_machine_change["environment_id"] = "another-environment"
        comparison = benchmark_scan.compare_baseline(cross_machine_change, baseline)
        self.assertEqual(comparison["status"], "failed")

    def test_strict_fixed_environment_gate_rejects_noncomparable_results(self) -> None:
        self.assertFalse(
            benchmark_scan.baseline_gate_failed({"status": "passed"}, require_comparable=True)
        )
        self.assertTrue(
            benchmark_scan.baseline_gate_failed(
                {"status": "not-comparable"}, require_comparable=True
            )
        )
        self.assertFalse(
            benchmark_scan.baseline_gate_failed(
                {"status": "not-comparable"}, require_comparable=False
            )
        )
        self.assertTrue(
            benchmark_scan.baseline_gate_failed({"status": "failed"}, require_comparable=False)
        )

    def test_strict_baseline_cli_exits_nonzero_when_timing_is_not_comparable(self) -> None:
        current = {
            "schema_version": benchmark_scan.SCHEMA_VERSION,
            "kind": "scan-benchmark",
            "profile": "full",
            "measurement": {"scope": "test"},
            "environment_id": "current",
            "records": [],
            "objectives": {"status": "passed", "violations": []},
        }
        baseline = {
            **copy.deepcopy(current),
            "kind": "frozen-scan-benchmark-baseline",
            "environment_id": "frozen",
        }
        argv = [
            "benchmark_scan.py",
            "--profile",
            "full",
            "--baseline",
            "baseline.json",
            "--require-comparable-baseline",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            benchmark_scan, "run_benchmark", return_value=current
        ), mock.patch.object(
            benchmark_scan, "_load_json", return_value=baseline
        ), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            benchmark_scan.main()
        self.assertEqual(raised.exception.code, 1)

        with mock.patch.object(
            sys, "argv", ["benchmark_scan.py", "--require-comparable-baseline"]
        ), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            benchmark_scan.main()
        self.assertEqual(raised.exception.code, 2)

    def test_freeze_requires_three_repetitions_and_the_complete_matrix(self) -> None:
        report = {
            "profile": "ci",
            "repeat": 2,
            "records": [],
            "objectives": {"status": "not-applicable"},
        }
        violations = benchmark_scan.validate_frozen_report(report)
        self.assertTrue(any("three repetitions" in item for item in violations))
        self.assertTrue(any("complete" in item for item in violations))

    def test_full_acceptance_checks_40mb_limits_and_scaling(self) -> None:
        records = []
        for workload in benchmark_scan.WORKLOADS:
            records.extend(
                [
                    {
                        "case_id": f"20mb:{workload}:boundary",
                        "size_bytes": 20 * 1024**2,
                        "workload": workload,
                        "scope": "boundary",
                        "median_elapsed_seconds": 20.0,
                        "median_peak_memory_bytes": 500_000_000,
                    },
                    {
                        "case_id": f"40mb:{workload}:boundary",
                        "size_bytes": 40 * 1024**2,
                        "workload": workload,
                        "scope": "boundary",
                        "median_elapsed_seconds": 52.0,
                        "median_peak_memory_bytes": 1_000_000_000,
                    },
                ]
            )
        self.assertEqual(benchmark_scan.evaluate_objectives(records)["status"], "passed")
        records[-1]["median_elapsed_seconds"] = 61.0
        result = benchmark_scan.evaluate_objectives(records)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("60 seconds" in item for item in result["violations"]))


if __name__ == "__main__":
    unittest.main()
