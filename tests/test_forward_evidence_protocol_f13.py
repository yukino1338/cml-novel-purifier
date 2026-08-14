from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forward_evidence  # noqa: E402


FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "forward_evidence_v1"
PREREGISTRATION = FIXTURE_ROOT / "preregistration.json"
RESULTS = FIXTURE_ROOT / "inference_results.json"


def completed_results(preregistration: dict[str, Any], root: Path) -> dict[str, Any]:
    slots = []
    for index, task in enumerate(preregistration["tasks"], 1):
        floor = task["scale_floor"]
        if task["stratum"] in {"encoding-block", "encoding-repair", "zero-candidate"}:
            candidate_total = 0
            anchor_total = 0
        else:
            candidate_total = max(1, floor["candidates"])
            anchor_total = max(candidate_total, floor["anchors"])
        slots.append(
            {
                "task_id": task["task_id"],
                "status": "completed",
                "host": (
                    "codex"
                    if task["host_lane"] == "codex-required"
                    else "opencode"
                ),
                "agent_context_sha256": f"{index % 16:x}" * 64,
                "artifact_manifest_sha256": f"{(index + 4) % 16:x}" * 64,
                "counts_sha256": f"{(index + 5) % 16:x}" * 64,
                "fixture_sha256": f"{(index + 1) % 16:x}" * 64,
                "gold_sha256": f"{(index + 2) % 16:x}" * 64,
                "started_at": f"2026-08-09T00:{index:02d}:00Z",
                "completed_at": f"2026-08-09T00:{index:02d}:59Z",
                "terminal_receipt_sha256": f"{(index + 3) % 16:x}" * 64,
                "outcome": "success",
                "failure_attribution": None,
                "counts": {
                    "anchor_total": anchor_total,
                    "candidate_total": candidate_total,
                    "candidate_reviewed": candidate_total,
                    "delete_anchor_gold": 0,
                    "delete_anchor_selected": 0,
                    "delete_anchor_correct": 0,
                    "required_events": len(task["required_operations"]),
                    "honored_events": len(task["required_operations"]),
                },
            }
        )
    result = {
        "schema": forward_evidence.RESULTS_SCHEMA,
        "study_id": preregistration["study_id"],
        "state": "completed",
        "evidence_class": "fresh-agent-inference",
        "publication_claim": "finite-preregistered-task-point-estimates-only",
        "bindings": forward_evidence.capture_bindings(root, preregistration),
        "host_coverage": {
            "required": ["codex"],
            "conditional": ["opencode"],
            "executed": ["codex", "opencode"],
            "conditional_unavailable_reason": None,
        },
        "task_slots": slots,
        "aggregate": None,
    }
    result["aggregate"] = forward_evidence.aggregate_results(preregistration, slots)
    return result


class ForwardEvidenceProtocolF13Tests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.preregistration = forward_evidence.load_json(PREREGISTRATION)
        cls.pending_results = forward_evidence.load_json(RESULTS)

    def test_preregistration_has_required_strata_hosts_scale_and_web_operations(self) -> None:
        preregistration = forward_evidence.validate_preregistration(
            self.preregistration
        )
        tasks = preregistration["tasks"]
        self.assertGreaterEqual(len(tasks), 12)
        self.assertGreaterEqual(
            sum(task["host_lane"] == "codex-required" for task in tasks),
            12,
        )
        self.assertTrue(
            any(task["host_lane"] == "opencode-conditional" for task in tasks)
        )
        self.assertTrue(
            any(
                task["scale_floor"]["candidates"] >= 150
                and task["scale_floor"]["anchors"] >= 700
                for task in tasks
            )
        )
        operations = {
            operation
            for task in tasks
            if task["stratum"] == "web-ui"
            for operation in task["required_operations"]
        }
        self.assertTrue(forward_evidence.REQUIRED_WEB_OPERATIONS <= operations)

    def test_pending_slots_are_explicit_and_publish_no_inference_claim(self) -> None:
        forward_evidence.validate_results(
            self.pending_results,
            self.preregistration,
        )
        assessment = forward_evidence.assess_evidence(
            ROOT,
            self.preregistration,
            self.pending_results,
        )
        self.assertEqual(assessment["status"], "pending")
        self.assertIs(assessment["inference_claim_allowed"], False)
        self.assertIs(assessment["deterministic_replay_is_agent_inference"], False)
        self.assertIsNone(self.pending_results["bindings"])
        self.assertEqual(self.pending_results["host_coverage"]["executed"], [])

    def test_legacy_summary_is_machine_readably_stale_not_silently_rehashed(self) -> None:
        legacy = forward_evidence.load_json(ROOT / "tests" / "forward_trials_summary.json")
        before = (ROOT / "tests" / "forward_trials_summary.json").read_bytes()
        assessment = forward_evidence.assess_legacy_summary(ROOT, legacy)
        self.assertEqual(assessment["status"], "stale")
        self.assertIs(assessment["inference_claim_allowed"], False)
        self.assertIs(assessment["deterministic_replay_is_agent_inference"], False)
        self.assertIn("runtime", assessment["stale_contracts"])
        self.assertIn("replay", assessment["stale_contracts"])
        self.assertEqual(
            (ROOT / "tests" / "forward_trials_summary.json").read_bytes(),
            before,
        )

    def test_status_cli_does_not_capture_runtime_hashes_for_pending_evidence(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "forward_evidence.py"),
                "status",
                "--root",
                str(ROOT),
                "--preregistration",
                str(PREREGISTRATION),
                "--results",
                str(RESULTS),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "pending")
        self.assertNotIn("sha256", completed.stdout)

    def test_wilson_interval_keeps_exact_counts_and_limits_twelve_of_twelve(self) -> None:
        metric = forward_evidence.wilson_metric(12, 12)
        self.assertEqual(metric["numerator"], 12)
        self.assertEqual(metric["denominator"], 12)
        self.assertEqual(metric["point_estimate"], 1.0)
        self.assertEqual(metric["interval_method"], "wilson-score-two-sided")
        self.assertAlmostEqual(metric["lower"], 0.757505993345, places=12)
        self.assertEqual(metric["upper"], 1.0)
        self.assertLess(metric["lower"], 0.995)

        empty = forward_evidence.wilson_metric(0, 0)
        self.assertIsNone(empty["point_estimate"])
        self.assertIsNone(empty["lower"])
        self.assertIsNone(empty["upper"])

    def test_runtime_guidance_and_schema_drift_each_mark_completed_evidence_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            files = {
                "runtime.py": "runtime-v1\n",
                "GUIDE.md": "guidance-v1\n",
                "schema.json": "{}\n",
            }
            for name, content in files.items():
                (root / name).write_text(content, encoding="utf-8")
            preregistration = copy.deepcopy(self.preregistration)
            preregistration["contract_scopes"] = {
                "runtime": ["runtime.py"],
                "guidance": ["GUIDE.md"],
                "schema": ["schema.json"],
            }
            results = completed_results(preregistration, root)
            forward_evidence.validate_results(results, preregistration)
            self.assertEqual(
                forward_evidence.assess_evidence(root, preregistration, results)[
                    "status"
                ],
                "current",
            )

            for contract_name, filename in (
                ("runtime", "runtime.py"),
                ("guidance", "GUIDE.md"),
                ("schema", "schema.json"),
            ):
                with self.subTest(contract=contract_name):
                    path = root / filename
                    before = path.read_text(encoding="utf-8")
                    path.write_text(before + "drift\n", encoding="utf-8")
                    assessment = forward_evidence.assess_evidence(
                        root,
                        preregistration,
                        results,
                    )
                    self.assertEqual(assessment["status"], "stale")
                    self.assertIn(contract_name, assessment["stale_contracts"])
                    self.assertIs(assessment["inference_claim_allowed"], False)
                    path.write_text(before, encoding="utf-8")

    def test_completed_aggregate_separates_public_and_private_and_has_host_breakdown(self) -> None:
        results = completed_results(self.preregistration, ROOT)
        forward_evidence.validate_results(results, self.preregistration)
        aggregate = results["aggregate"]
        public = aggregate["public_anonymous"]
        private = aggregate["private_self_attested"]
        self.assertEqual(public["task_count"], 14)
        self.assertEqual(public["metrics"]["task-success"]["numerator"], 14)
        self.assertEqual(public["metrics"]["task-success"]["denominator"], 14)
        self.assertEqual(set(public["by_host"]), {"codex", "opencode"})
        self.assertTrue(
            forward_evidence.REQUIRED_STRATA <= set(public["by_stratum"])
        )
        self.assertIs(private["included_in_release_metrics"], False)
        self.assertEqual(private["task_count"], 1)
        self.assertEqual(private["success_count"], 1)
        private_blob = json.dumps(private, ensure_ascii=False).casefold()
        for forbidden in ("path", "filename", "title", "author", "content"):
            self.assertNotIn(forbidden, private_blob)

    def test_tampered_aggregate_and_scale_shortfall_fail_closed(self) -> None:
        results = completed_results(self.preregistration, ROOT)
        tampered = copy.deepcopy(results)
        tampered["aggregate"]["public_anonymous"]["metrics"]["task-success"][
            "numerator"
        ] -= 1
        with self.assertRaisesRegex(
            forward_evidence.EvidenceContractError,
            "aggregate",
        ):
            forward_evidence.validate_results(tampered, self.preregistration)

        inflated_claim = copy.deepcopy(results)
        inflated_claim["publication_claim"] = "cross-work-precision-is-99.5-percent"
        with self.assertRaisesRegex(
            forward_evidence.EvidenceContractError,
            "publication claim",
        ):
            forward_evidence.validate_results(
                inflated_claim,
                self.preregistration,
            )

        short = copy.deepcopy(results)
        large = next(
            slot for slot in short["task_slots"] if slot["task_id"] == "FT-008"
        )
        large["counts"]["candidate_total"] = 149
        large["counts"]["candidate_reviewed"] = 149
        with self.assertRaisesRegex(
            forward_evidence.EvidenceContractError,
            "scale floor",
        ):
            forward_evidence.validate_results(short, self.preregistration)

    def test_unknown_fields_duplicate_json_and_escaping_scopes_fail_closed(self) -> None:
        unknown = copy.deepcopy(self.preregistration)
        unknown["tasks"][0]["post_hoc_note"] = "not preregistered"
        with self.assertRaisesRegex(
            forward_evidence.EvidenceContractError,
            "unknown",
        ):
            forward_evidence.validate_preregistration(unknown)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"schema":1,"schema":2}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                forward_evidence.EvidenceContractError,
                "duplicate JSON key",
            ):
                forward_evidence.load_json(duplicate)
            with self.assertRaisesRegex(
                forward_evidence.EvidenceContractError,
                "escapes root",
            ):
                forward_evidence.collect_scope_files(root, ["../outside.txt"])

    def test_prepare_package_excludes_gold_tests_and_records_one_bound_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sources = root / "evaluator-sources"
            sources.mkdir()
            (sources / "prompt.md").write_text(
                "请按 Skill 清洗 assigned/novel/input.txt，并给出终态回执。\n",
                encoding="utf-8",
            )
            (sources / "input.txt").write_text(
                "第一章 风声\n正文。\n请访问 https://reader.example.com/update 获取后续。\n",
                encoding="utf-8",
            )
            gold_secret = "EVALUATOR_ONLY_EXPECTED_DELETE"
            (sources / "gold.json").write_text(
                json.dumps({"secret": gold_secret}) + "\n",
                encoding="utf-8",
            )
            source_manifest = {
                "schema": forward_evidence.PACKAGE_SOURCES_SCHEMA,
                "study_id": self.preregistration["study_id"],
                "tasks": [
                    {
                        "task_id": "FT-001",
                        "prompt_file": "prompt.md",
                        "visible_files": [
                            {
                                "source": "input.txt",
                                "destination": "novel/input.txt",
                            }
                        ],
                        "gold_files": ["gold.json"],
                    }
                ],
            }
            output = root / "packages"
            evaluator = forward_evidence.prepare_agent_packages(
                ROOT,
                self.preregistration,
                sources,
                source_manifest,
                output,
                ["FT-001"],
            )
            package = output / "FT-001"
            self.assertTrue((package / "SKILL.md").is_file())
            self.assertTrue((package / "TASK.md").is_file())
            self.assertTrue((package / "assigned" / "novel" / "input.txt").is_file())
            self.assertFalse((package / "tests").exists())
            self.assertFalse((package / "gold").exists())
            package_bytes = b"".join(
                path.read_bytes() for path in package.rglob("*") if path.is_file()
            )
            self.assertNotIn(gold_secret.encode("utf-8"), package_bytes)
            package_manifest = forward_evidence.load_json(
                package / "PACKAGE_MANIFEST.json"
            )
            self.assertNotIn("gold_sha256", package_manifest)
            self.assertEqual(evaluator["packages"][0]["task_id"], "FT-001")
            with self.assertRaisesRegex(
                forward_evidence.EvidenceContractError,
                "already exists",
            ):
                forward_evidence.prepare_agent_packages(
                    ROOT,
                    self.preregistration,
                    sources,
                    source_manifest,
                    output,
                    ["FT-001"],
                )

            results_path = root / "results.json"
            results_path.write_text(
                json.dumps(self.pending_results, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            receipt = root / "receipt.json"
            receipt.write_text('{"status":"passed"}\n', encoding="utf-8")
            review = root / "agent-review.jsonl"
            review.write_text('{"verdict":"keep"}\n', encoding="utf-8")
            artifact_manifest_path = root / "task-artifacts.json"
            artifact_entries = []
            for role, path in (
                ("agent-review", review),
                ("gold", sources / "gold.json"),
                ("input", sources / "input.txt"),
                ("prompt", sources / "prompt.md"),
                ("terminal-receipt", receipt),
            ):
                artifact_entries.append(
                    {
                        "role": role,
                        "path": path.relative_to(root).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "retention": "public-anonymous",
                    }
                )
            artifact_manifest_path.write_text(
                json.dumps(
                    {
                        "schema": forward_evidence.TASK_ARTIFACT_SCHEMA,
                        "task_id": "FT-001",
                        "privacy": "public-anonymous",
                        "artifacts": artifact_entries,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            counts = root / "counts.json"
            counts.write_text(
                json.dumps(
                    {
                        "anchor_total": 8,
                        "candidate_total": 6,
                        "candidate_reviewed": 6,
                        "delete_anchor_gold": 3,
                        "delete_anchor_selected": 3,
                        "delete_anchor_correct": 3,
                        "required_events": 4,
                        "honored_events": 4,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            slot = forward_evidence.record_completed_slot(
                ROOT,
                self.preregistration,
                results_path,
                output / "EVALUATOR_MANIFEST.json",
                artifact_manifest_path,
                "FT-001",
                "a" * 64,
                receipt,
                counts,
                "2026-08-09T01:00:00Z",
                "2026-08-09T01:05:00Z",
                "success",
                None,
            )
            self.assertEqual(slot["status"], "completed")
            recorded = forward_evidence.load_json(results_path)
            self.assertEqual(recorded["state"], "collecting")
            self.assertEqual(recorded["host_coverage"]["executed"], ["codex"])
            self.assertEqual(
                recorded["aggregate"]["public_anonymous"]["task_count"],
                1,
            )
            self.assertEqual(
                forward_evidence.assess_evidence(
                    ROOT,
                    self.preregistration,
                    recorded,
                )["status"],
                "incomplete",
            )

    def test_finalize_requires_all_codex_slots_and_marks_only_conditional_hosts_unavailable(self) -> None:
        completed = completed_results(self.preregistration, ROOT)
        task_map = {
            task["task_id"]: task for task in self.preregistration["tasks"]
        }
        slots = []
        for slot in completed["task_slots"]:
            if task_map[slot["task_id"]]["host_lane"] == "opencode-conditional":
                slots.append(
                    {
                        "task_id": slot["task_id"],
                        "status": "pending",
                        "host": None,
                        "agent_context_sha256": None,
                        "artifact_manifest_sha256": None,
                        "counts_sha256": None,
                        "fixture_sha256": None,
                        "gold_sha256": None,
                        "started_at": None,
                        "completed_at": None,
                        "terminal_receipt_sha256": None,
                        "outcome": None,
                        "failure_attribution": None,
                        "counts": None,
                    }
                )
            else:
                slots.append(slot)
        collecting = copy.deepcopy(completed)
        collecting["state"] = "collecting"
        collecting["publication_claim"] = "none-collecting-preregistered-slots"
        collecting["host_coverage"]["executed"] = ["codex"]
        collecting["task_slots"] = slots
        collecting["aggregate"] = forward_evidence.aggregate_results(
            self.preregistration,
            slots,
        )
        forward_evidence.validate_results(collecting, self.preregistration)

        with tempfile.TemporaryDirectory() as temp_dir:
            results_path = Path(temp_dir) / "results.json"
            results_path.write_text(
                json.dumps(collecting, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            finalized = forward_evidence.finalize_results(
                self.preregistration,
                results_path,
                "OpenCode host unavailable during the preregistered window",
            )
            self.assertEqual(finalized["state"], "completed")
            self.assertEqual(finalized["aggregate"]["host_unavailable_count"], 3)
            self.assertEqual(
                finalized["aggregate"]["public_anonymous"]["task_count"],
                12,
            )
            self.assertEqual(
                forward_evidence.assess_evidence(
                    ROOT,
                    self.preregistration,
                    finalized,
                )["status"],
                "current",
            )

            broken = copy.deepcopy(collecting)
            broken["task_slots"][0] = copy.deepcopy(slots[-1])
            broken["task_slots"][0]["task_id"] = "FT-001"
            broken["aggregate"] = forward_evidence.aggregate_results(
                self.preregistration,
                broken["task_slots"],
            )
            results_path.write_text(
                json.dumps(broken, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                forward_evidence.EvidenceContractError,
                "required task remains pending",
            ):
                forward_evidence.finalize_results(
                    self.preregistration,
                    results_path,
                    "OpenCode host unavailable during the preregistered window",
                )


if __name__ == "__main__":
    unittest.main()
