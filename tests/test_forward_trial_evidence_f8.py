from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import apply_decisions  # noqa: E402
import common  # noqa: E402
import export_outputs  # noqa: E402
import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import normalize_layout  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_ads  # noqa: E402
import scan_blocked  # noqa: E402
import scan_identity  # noqa: E402
import scan_titles  # noqa: E402
import verify  # noqa: E402


EVIDENCE = ROOT / "tests" / "forward_trials_summary.json"
FIXTURES = ROOT / "tests" / "fixtures" / "forward_trials"
RUNTIME_SCOPE = (
    "SKILL.md",
    "agents/openai.yaml",
    "assets/**",
    "references/**",
    "scripts/**",
)
FIXTURE_SCOPE = ("tests/fixtures/forward_trials/**",)
REPLAY_SCOPE = (
    "tests/test_forward_trial_evidence_f8.py",
    ".github/workflows/ci.yml",
    "pyproject.toml",
    "requirements-dev.txt",
)
CONTRACT_KEYS = {"schema", "sha256", "file_count", "scope", "framing"}
FRAMING = (
    "sorted POSIX relative path; 8-byte big-endian path length; UTF-8 path; "
    "8-byte big-endian payload length; original payload bytes"
)
OUTCOME_VALUES = {
    "passed-exported",
    "passed-exported-no-deletion",
    "uncertain-stopped",
    "truncation-rescanned-passed-exported",
    "deletion-threshold-stopped",
}
STOP_EVENT_VALUES = {
    "truncated-anchors-before-rescan",
    "uncertain-formal-decision",
    "deletion-ratio-above-eight-percent",
}
OUTCOME_KEYS = {
    "trial",
    "outcome",
    "total_candidates",
    "reviewed_candidates",
    "keep",
    "delete",
    "uncertain",
    "candidate_anchors",
    "eligible_delete_anchor_ids",
    "deleted_anchor_ids",
    "correct_delete_anchor_ids",
    "false_delete_anchor_ids",
    "missed_delete_anchor_ids",
    "required_stop_events",
    "observed_stop_events",
    "missing_stop_events",
    "extra_stop_events",
    "verify",
    "export",
    "source_unchanged",
    "v0_unchanged",
    "unauthorized_title_or_blocked_mutation",
    "identities",
}
PUBLIC_KEYS = {
    "included_in_release_metrics",
    "script_pipeline_replayable",
    "total_candidate_count",
    "reviewed_candidate_count",
    "kept_candidate_count",
    "deleted_candidate_count",
    "uncertain_candidate_count",
    "candidate_anchor_count",
    "eligible_delete_anchor_count",
    "deleted_anchor_count",
    "correct_delete_anchor_count",
    "false_delete_anchor_count",
    "missed_delete_anchor_count",
    "required_stop_count",
    "observed_stop_count",
    "honored_stop_count",
    "missing_stop_count",
    "extra_stop_count",
    "truncation_rescan_count",
    "verification_pass_count",
    "export_pass_count",
    "source_unchanged_count",
    "v0_unchanged_count",
    "unauthorized_title_or_blocked_mutation_count",
    "outcomes",
}
PRIVATE_KEYS = {
    "included_in_release_metrics",
    "evidence_class",
    "retention",
    "trial_count",
    "input_bytes",
    "reviewed_candidate_count",
    "kept_candidate_count",
    "deleted_candidate_count",
    "uncertain_candidate_count",
    "candidate_anchor_count",
    "delete_anchor_count",
    "zero_candidate_trial_count",
    "verification_pass_count",
    "export_pass_count",
    "source_unchanged_count",
    "v0_unchanged_count",
    "false_delete_anchor_count",
    "public_file_change_count",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
TRIAL_RE = re.compile(r"anonymous-[0-9]{2}")


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        value = json.loads(line, object_pairs_hook=reject_duplicate_keys)
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object at {path}:{line_number}")
        rows.append(value)
    return rows


def runtime_contract_files() -> list[Path]:
    files = [ROOT / "SKILL.md", ROOT / "agents" / "openai.yaml"]
    for directory in ("assets", "references", "scripts"):
        files.extend(
            path
            for path in (ROOT / directory).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def fixture_contract_files() -> list[Path]:
    return sorted(
        (path for path in FIXTURES.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(ROOT).as_posix(),
    )


def replay_contract_files() -> list[Path]:
    return sorted(
        (ROOT / relative for relative in REPLAY_SCOPE),
        key=lambda path: path.relative_to(ROOT).as_posix(),
    )


def framed_contract_sha256(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def tree_snapshot(root: Path) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    directories = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        )
    )
    files = tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in root.rglob("*")
            if path.is_file()
        )
    )
    return directories, files


def optional_file_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def derive_outcome(
    verdict_counts: dict[str, int],
    observed_stop_events: list[str],
    verify_status: str,
    export_status: str,
) -> str:
    observed = set(observed_stop_events)
    if verdict_counts["uncertain"]:
        return "uncertain-stopped"
    if verify_status == "blocked":
        return "deletion-threshold-stopped"
    if export_status == "passed":
        if "truncated-anchors-before-rescan" in observed:
            return "truncation-rescanned-passed-exported"
        if verdict_counts["delete"] == 0:
            return "passed-exported-no-deletion"
        return "passed-exported"
    raise AssertionError(
        "the replay did not reach a recognized terminal outcome: "
        f"verify={verify_status}, export={export_status}, stops={observed_stop_events}"
    )


class ForwardTrialEvidenceF8Tests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.evidence = read_json(EVIDENCE)

    def assert_exact_keys(
        self,
        value: dict[str, Any],
        expected: set[str],
        label: str,
    ) -> None:
        self.assertEqual(set(value), expected, label)

    def assert_non_negative_int(self, value: Any, label: str) -> None:
        self.assertIs(type(value), int, label)
        self.assertGreaterEqual(value, 0, label)

    def assert_string_list(
        self,
        value: Any,
        label: str,
        *,
        unique: bool = True,
    ) -> list[str]:
        self.assertIsInstance(value, list, label)
        self.assertTrue(all(isinstance(item, str) for item in value), label)
        if unique:
            self.assertEqual(len(value), len(set(value)), label)
        return value

    def assert_contract(
        self,
        contract: dict[str, Any],
        *,
        scope: tuple[str, ...],
        files: list[Path],
        label: str,
    ) -> None:
        self.assert_exact_keys(contract, CONTRACT_KEYS, label)
        self.assertEqual(contract["schema"], "framed-path-sha256-v1")
        self.assertEqual(contract["framing"], FRAMING)
        self.assertEqual(tuple(contract["scope"]), scope)
        self.assertEqual(contract["file_count"], len(files))
        self.assertIsInstance(contract["sha256"], str)
        self.assertIsNotNone(SHA256_RE.fullmatch(contract["sha256"]))
        self.assertEqual(contract["sha256"], framed_contract_sha256(files))

    def test_schema3_is_strict_and_semantics_are_explicit(self) -> None:
        data = self.evidence
        self.assert_exact_keys(
            data,
            {
                "schema_version",
                "kind",
                "completed_on",
                "evidence_semantics",
                "runtime_contract",
                "fixture_contract",
                "replay_contract",
                "isolation",
                "trial_counts",
                "public_trials",
                "private_excerpt_aggregate",
                "release_metrics",
            },
            "top-level schema",
        )
        self.assertIs(data["schema_version"], 3)
        self.assertEqual(data["kind"], "replayable-public-forward-trials")
        completed = data["completed_on"]
        self.assertIsInstance(completed, str)
        parsed_date = date.fromisoformat(completed)
        self.assertEqual(parsed_date.isoformat(), completed)

        semantics = data["evidence_semantics"]
        self.assert_exact_keys(
            semantics,
            {
                "agent_review_source",
                "ci_replay_scope",
                "ci_reruns_agent_inference",
            },
            "evidence semantics",
        )
        self.assertEqual(
            semantics["agent_review_source"],
            "retained-fresh-agent-reviews",
        )
        self.assertEqual(
            semantics["ci_replay_scope"],
            "retained-reviews-and-script-pipeline",
        )
        self.assertIs(semantics["ci_reruns_agent_inference"], False)

        counts = data["trial_counts"]
        self.assert_exact_keys(
            counts,
            {"total", "public_replayable", "authorized_private_excerpt"},
            "trial counts",
        )
        self.assertEqual(
            counts,
            {
                "total": 9,
                "public_replayable": 6,
                "authorized_private_excerpt": 3,
            },
        )

        isolation = data["isolation"]
        self.assert_exact_keys(
            isolation,
            {
                "evidence_class",
                "fresh_agent_context_count",
                "cross_trial_context_reuse",
                "historical_workspaces_excluded",
                "tests_and_gold_excluded_during_agent_review",
                "private_inputs_local_only",
                "private_public_retention",
                "public_agent_reviews_retained",
            },
            "isolation",
        )
        self.assertEqual(
            isolation["evidence_class"],
            "generation-protocol-self-attested",
        )
        self.assertEqual(isolation["fresh_agent_context_count"], counts["total"])
        self.assertIs(isolation["cross_trial_context_reuse"], False)
        for field in (
            "historical_workspaces_excluded",
            "tests_and_gold_excluded_during_agent_review",
            "private_inputs_local_only",
            "public_agent_reviews_retained",
        ):
            self.assertIs(isolation[field], True)
        self.assertEqual(isolation["private_public_retention"], "aggregate-metrics-only")

        public = data["public_trials"]
        self.assert_exact_keys(public, PUBLIC_KEYS, "public trials")
        self.assertIs(public["included_in_release_metrics"], True)
        self.assertIs(public["script_pipeline_replayable"], True)
        for field in PUBLIC_KEYS - {
            "included_in_release_metrics",
            "script_pipeline_replayable",
            "outcomes",
        }:
            self.assert_non_negative_int(public[field], f"public.{field}")
        outcomes = public["outcomes"]
        self.assertIsInstance(outcomes, list)
        self.assertEqual(len(outcomes), counts["public_replayable"])
        trial_names: list[str] = []
        for outcome in outcomes:
            self.assertIsInstance(outcome, dict)
            self.assert_exact_keys(outcome, OUTCOME_KEYS, "public outcome")
            self.assertIsNotNone(TRIAL_RE.fullmatch(outcome["trial"]))
            trial_names.append(outcome["trial"])
            self.assertIn(outcome["outcome"], OUTCOME_VALUES)
            self.assertIn(outcome["verify"], {"not-run", "blocked", "passed"})
            self.assertIn(outcome["export"], {"not-run", "passed"})
            for field in (
                "total_candidates",
                "keep",
                "delete",
                "uncertain",
                "candidate_anchors",
                "unauthorized_title_or_blocked_mutation",
            ):
                self.assert_non_negative_int(outcome[field], f"outcome.{field}")
            reviewed = self.assert_string_list(
                outcome["reviewed_candidates"],
                "outcome.reviewed_candidates",
            )
            self.assertEqual(reviewed, sorted(reviewed))
            self.assertTrue(
                all(re.fullmatch(r"AD-[0-9]{4}", candidate_id) for candidate_id in reviewed)
            )
            for field in (
                "eligible_delete_anchor_ids",
                "deleted_anchor_ids",
                "correct_delete_anchor_ids",
                "false_delete_anchor_ids",
                "missed_delete_anchor_ids",
            ):
                values = self.assert_string_list(outcome[field], f"outcome.{field}")
                self.assertEqual(values, sorted(values))
                self.assertTrue(
                    all(
                        re.fullmatch(r"AN-[0-9a-f]{64}", anchor_id)
                        for anchor_id in values
                    )
                )
            for field in (
                "required_stop_events",
                "observed_stop_events",
                "missing_stop_events",
                "extra_stop_events",
            ):
                events = self.assert_string_list(outcome[field], f"outcome.{field}")
                self.assertTrue(set(events) <= STOP_EVENT_VALUES)
            self.assertIs(type(outcome["source_unchanged"]), bool)
            self.assertIs(type(outcome["v0_unchanged"]), bool)

            identities = outcome["identities"]
            required_identity_keys = {
                "input_sha256",
                "scan_id",
                "candidate_set_sha256",
                "reviews_sha256",
                "formal_decisions_sha256",
            }
            if outcome["outcome"] == "uncertain-stopped":
                expected_identity_keys = required_identity_keys
            else:
                expected_identity_keys = required_identity_keys | {
                    "apply_output_sha256",
                    "final_output_sha256",
                }
            self.assert_exact_keys(
                identities,
                expected_identity_keys,
                "outcome identities",
            )
            for identity in identities.values():
                self.assertIsInstance(identity, str)
                self.assertIsNotNone(SHA256_RE.fullmatch(identity))
        self.assertEqual(len(trial_names), len(set(trial_names)))
        self.assertEqual(trial_names, sorted(trial_names))

        private = data["private_excerpt_aggregate"]
        self.assert_exact_keys(private, PRIVATE_KEYS, "private aggregate")
        self.assertIs(private["included_in_release_metrics"], False)
        self.assertEqual(
            private["evidence_class"],
            "supplemental-private-self-attested",
        )
        self.assertEqual(private["retention"], "aggregate-metrics-only")
        for field in PRIVATE_KEYS - {
            "included_in_release_metrics",
            "evidence_class",
            "retention",
        }:
            self.assert_non_negative_int(private[field], f"private.{field}")
        self.assertGreater(private["input_bytes"], 0)
        self.assertEqual(
            private["trial_count"],
            counts["authorized_private_excerpt"],
        )
        self.assertEqual(
            private["reviewed_candidate_count"],
            private["kept_candidate_count"]
            + private["deleted_candidate_count"]
            + private["uncertain_candidate_count"],
        )
        self.assertEqual(private["source_unchanged_count"], private["trial_count"])
        self.assertEqual(private["v0_unchanged_count"], private["trial_count"])
        self.assertEqual(private["public_file_change_count"], 0)
        private_blob = json.dumps(private, ensure_ascii=False).casefold()
        for forbidden in ("path", "filename", "content", "title", "author"):
            self.assertNotIn(forbidden, private_blob)

        metrics = data["release_metrics"]
        self.assert_exact_keys(
            metrics,
            {
                "basis",
                "candidate_review_coverage",
                "supported_delete_anchor_recall",
                "delete_anchor_precision",
                "required_stop_compliance",
            },
            "release metrics",
        )
        self.assertEqual(metrics["basis"], "six public replayable trials only")
        for metric, numerator_key, denominator_key in (
            ("candidate_review_coverage", "reviewed", "total"),
            ("supported_delete_anchor_recall", "correct", "eligible"),
            ("delete_anchor_precision", "correct", "deleted"),
            ("required_stop_compliance", "honored", "required"),
        ):
            value = metrics[metric]
            self.assert_exact_keys(
                value,
                {numerator_key, denominator_key, "rate"},
                f"release metric {metric}",
            )
            self.assert_non_negative_int(value[numerator_key], metric)
            self.assert_non_negative_int(value[denominator_key], metric)
            self.assertIn(type(value["rate"]), (int, float))
            self.assertTrue(math.isfinite(value["rate"]))
            self.assertGreaterEqual(value["rate"], 0)
            self.assertLessEqual(value["rate"], 1)

    def test_runtime_and_replay_contracts_bind_execution_surface(self) -> None:
        data = self.evidence
        # The retained Agent judgments are historical evidence.  Runtime or
        # replay/schema drift makes that inference evidence stale; a green
        # deterministic replay must never silently refresh its old contracts.
        current_runtime_files = runtime_contract_files()
        self.assertNotEqual(
            (
                data["runtime_contract"]["sha256"],
                data["runtime_contract"]["file_count"],
            ),
            (
                framed_contract_sha256(current_runtime_files),
                len(current_runtime_files),
            ),
        )
        replay_files = replay_contract_files()
        self.assertTrue(all(path.is_file() for path in replay_files))
        self.assertNotEqual(
            data["replay_contract"]["sha256"],
            framed_contract_sha256(replay_files),
        )
        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "ci.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertIsInstance(workflow, dict)
        quality = workflow["jobs"]["quality"]
        commands = {
            " ".join(str(step["run"]).split())
            for step in quality["steps"]
            if isinstance(step, dict) and "run" in step
        }
        self.assertIn(
            'python -m coverage run --branch -m unittest discover -s tests -p "test*.py"',
            commands,
        )

    def load_trial_config(self, fixture: Path) -> dict[str, Any]:
        config = read_json(fixture / "trial_config.json")
        self.assert_exact_keys(
            config,
            {
                "schema_version",
                "min_chars",
                "max_candidates",
                "max_anchors",
                "near_scan_scope",
                "near_boundary_chars",
            },
            f"{fixture.name} trial config",
        )
        self.assertIs(config["schema_version"], 1)
        for field in (
            "min_chars",
            "max_candidates",
            "max_anchors",
            "near_boundary_chars",
        ):
            self.assertIs(type(config[field]), int)
            self.assertGreater(config[field], 0)
        self.assertIn(config["near_scan_scope"], {"boundary", "all"})
        return config

    def scan_ads_from_config(
        self,
        workspace: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        return scan_ads.run(
            workspace,
            "versions/v1_preprocessed.txt",
            "candidates/ads.jsonl",
            config["min_chars"],
            config["max_candidates"],
            config["max_anchors"],
            config["near_scan_scope"],
            config["near_boundary_chars"],
        )

    def prepare_workspace(
        self,
        fixture: Path,
        trial_root: Path,
    ) -> tuple[Path, Path, bytes]:
        trial_root.mkdir()
        source = trial_root / "input.txt"
        shutil.copyfile(fixture / "input.txt", source)
        source_before = source.read_bytes()
        workspace = preprocess.run(
            source,
            str(trial_root / "input.txt.cleanwork"),
            "utf-8",
        )
        parse_structure.run(workspace)
        shutil.copyfile(
            fixture / "profile.json",
            workspace / "meta" / "book_profile.json",
        )
        return source, workspace, source_before

    def prove_truncation_gate(
        self,
        fixture: Path,
        root: Path,
        trial_config: dict[str, Any],
    ) -> str | None:
        probe_path = fixture / "truncation_probe.json"
        if not probe_path.exists():
            return None

        probe = read_json(probe_path)
        self.assert_exact_keys(
            probe,
            {
                "schema_version",
                "scan_config",
                "scan_id",
                "candidate_set_sha256",
                "page_sha256",
                "candidate_id",
                "candidate_fingerprint",
                "occurrence_count",
                "anchors_truncated",
                "saved_anchor_ids",
                "required_action",
            },
            "truncation probe",
        )
        self.assertIs(probe["schema_version"], 1)
        self.assertEqual(probe["required_action"], "stop-before-review-and-rescan")
        self.assertEqual(set(probe["scan_config"]), {"max_anchors"})
        probe_config = dict(trial_config)
        probe_config["max_anchors"] = probe["scan_config"]["max_anchors"]

        _, workspace, _ = self.prepare_workspace(
            fixture,
            root / f"{fixture.name}-truncation-probe",
        )
        probe_report = self.scan_ads_from_config(workspace, probe_config)
        probe_candidates = scan_identity.load_validated_pages(
            workspace,
            probe_report,
        )
        self.assertEqual(len(probe_candidates), 1)
        candidate = probe_candidates[0]
        self.assertEqual(candidate["candidate_id"], probe["candidate_id"])
        self.assertEqual(candidate["occurrence_count"], probe["occurrence_count"])
        self.assertIs(candidate["anchors_truncated"], True)
        self.assertEqual(len(candidate["anchors"]), probe_config["max_anchors"])

        make_ad_decisions.run(
            workspace,
            "candidates/ads_pages",
            "decisions/ads_decisions.draft.jsonl",
            "meta/book_profile.json",
            True,
        )
        probe_review = {
            "scan_id": probe_report["scan_id"],
            "candidate_id": candidate["candidate_id"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "verdict": "delete",
            "confidence": 0.99,
            "reason": "探针仅证明截断候选不能形成删除决策。",
            "action": "delete",
            "risk": "low",
            "splice_strategy": "remove_paragraph",
        }
        write_jsonl(
            workspace / "decisions" / "ads_agent_reviews.jsonl",
            [probe_review],
        )
        formal_path = workspace / "decisions" / "ads_decisions.jsonl"
        self.assertFalse(formal_path.exists())
        manifest_before = (workspace / "manifest.json").read_bytes()
        tree_before = tree_snapshot(workspace)
        with self.assertRaisesRegex(ValueError, "truncated"):
            finalize_ad_decisions.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_agent_reviews.jsonl",
                "decisions/ads_decisions.draft.jsonl",
                "decisions/ads_decisions.jsonl",
            )
        self.assertFalse(formal_path.exists())
        self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
        self.assertEqual(tree_snapshot(workspace), tree_before)
        return "truncated-anchors-before-rescan"

    def run_report_only_scans(self, workspace: Path) -> int:
        manifest_before = read_json(workspace / "manifest.json")
        current_head = manifest_before["current_head"]
        current_path = workspace / current_head
        current_bytes = current_path.read_bytes()
        current_sha256 = common.sha256_file(current_path)
        versions_before = tree_snapshot(workspace / "versions")
        operations_path = workspace / "logs" / "operations.jsonl"
        operations_before = optional_file_bytes(operations_path)

        scan_titles.run(workspace, "auto", "candidates/titles.jsonl")
        scan_blocked.run(
            workspace,
            "auto",
            "candidates/blocked.jsonl",
            300,
        )

        manifest_after = read_json(workspace / "manifest.json")
        mutation = any(
            (
                manifest_after["current_head"] != current_head,
                current_path.read_bytes() != current_bytes,
                common.sha256_file(current_path) != current_sha256,
                tree_snapshot(workspace / "versions") != versions_before,
                optional_file_bytes(operations_path) != operations_before,
                (workspace / "decisions" / "titles_decisions.jsonl").exists(),
                (workspace / "decisions" / "blocked_decisions.jsonl").exists(),
                (workspace / "versions" / "v3_titles_fixed.txt").exists(),
                (workspace / "versions" / "v4_blocked_fixed.txt").exists(),
            )
        )
        self.assertFalse(mutation)
        self.assertEqual(
            manifest_after["artifacts"][current_head]["sha256"],
            current_sha256,
        )
        return int(mutation)

    def replay_trial(self, fixture: Path, root: Path) -> dict[str, Any]:
        # Configuration and historical semantic verdicts are deterministic replay
        # inputs, not fresh Agent inference.  Gold is withheld until the product
        # pipeline reaches an observed terminal state, so it cannot select control
        # flow.  Current identifiers are rebound mechanically; verdicts are not
        # regenerated and this replay cannot refresh historical inference evidence.
        config = self.load_trial_config(fixture)
        observed_stop_events: list[str] = []
        truncation_stop = self.prove_truncation_gate(fixture, root, config)
        if truncation_stop is not None:
            observed_stop_events.append(truncation_stop)

        trial_root = root / fixture.name
        source, workspace, source_before = self.prepare_workspace(
            fixture,
            trial_root,
        )
        scan_report = self.scan_ads_from_config(workspace, config)
        candidates = scan_identity.load_validated_pages(workspace, scan_report)
        self.assertEqual(
            scan_report["summary"]["total_candidate_count"],
            len(candidates),
        )
        self.assertFalse(
            any(candidate["anchors_truncated"] for candidate in candidates)
        )
        make_ad_decisions.run(
            workspace,
            "candidates/ads_pages",
            "decisions/ads_decisions.draft.jsonl",
            "meta/book_profile.json",
            True,
        )
        reviews_path = workspace / "decisions" / "ads_agent_reviews.jsonl"
        historical_reviews = read_jsonl(fixture / "reviews.jsonl")
        candidate_by_id = {
            candidate["candidate_id"]: candidate for candidate in candidates
        }
        self.assertEqual(
            {review["candidate_id"] for review in historical_reviews},
            set(candidate_by_id),
        )
        replay_reviews = []
        for historical in historical_reviews:
            current_candidate = candidate_by_id[historical["candidate_id"]]
            rebound = dict(historical)
            rebound["scan_id"] = scan_report["scan_id"]
            rebound["candidate_fingerprint"] = current_candidate[
                "candidate_fingerprint"
            ]
            if rebound.get("verdict") == "delete" and current_candidate.get(
                "edit_plan"
            ):
                rebound["edit_plan_id"] = current_candidate["edit_plan"][
                    "edit_plan_id"
                ]
                rebound["splice_strategy"] = "exact_segment"
            replay_reviews.append(rebound)
        write_jsonl(reviews_path, replay_reviews)
        retained_reviews = read_jsonl(reviews_path)
        formal_report = finalize_ad_decisions.run(
            workspace,
            "candidates/ads_pages",
            "decisions/ads_agent_reviews.jsonl",
            "decisions/ads_decisions.draft.jsonl",
            "decisions/ads_decisions.jsonl",
        )
        decisions_path = workspace / "decisions" / "ads_decisions.jsonl"
        decisions = read_jsonl(decisions_path)
        total_candidates = scan_report["summary"]["total_candidate_count"]
        formal_ids = {decision["candidate_id"] for decision in decisions}
        reviewed_candidates = sorted(
            {
                review["candidate_id"]
                for review in retained_reviews
                if review.get("candidate_id") in formal_ids
            }
        )
        self.assertEqual(formal_report["review_count"], len(retained_reviews))
        self.assertEqual(formal_report["decision_count"], len(decisions))

        verdict_counts = {
            verdict: sum(
                decision["verdict"] == verdict for decision in decisions
            )
            for verdict in ("keep", "delete", "uncertain")
        }
        verify_status = "not-run"
        export_status = "not-run"
        apply_output_sha256: str | None = None
        final_output_sha256: str | None = None
        unauthorized_mutation = self.run_report_only_scans(workspace)

        if verdict_counts["uncertain"]:
            manifest_before = (workspace / "manifest.json").read_bytes()
            tree_before = tree_snapshot(workspace)
            with self.assertRaisesRegex(ValueError, "uncertain"):
                apply_decisions.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "decisions/ads_decisions.jsonl",
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                )
            self.assertEqual(
                (workspace / "manifest.json").read_bytes(),
                manifest_before,
            )
            self.assertEqual(tree_snapshot(workspace), tree_before)
            self.assertFalse(
                (workspace / "versions" / "v2_ads_removed.txt").exists()
            )
            observed_stop_events.append("uncertain-formal-decision")
        else:
            apply_report = apply_decisions.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )
            apply_output_sha256 = apply_report["output_sha256"]
            layout_report = normalize_layout.run(
                workspace,
                "auto",
                "versions/v5_layout_final.txt",
                None,
            )
            final_output_sha256 = layout_report["output_sha256"]
            verify_report = verify.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "auto",
                "decisions/ads_decisions.jsonl",
                False,
            )
            verify_status = verify_report["status"]
            if verify_status == "blocked":
                self.assertGreater(
                    verify_report["char_counts"]["deletion_ratio"],
                    0.08,
                )
                self.assertIn(
                    "deletion ratio is above 8%",
                    verify_report["warnings"],
                )
                export_root = trial_root / "exports"
                manifest_before = (workspace / "manifest.json").read_bytes()
                tree_before = tree_snapshot(workspace)
                with self.assertRaisesRegex(ValueError, "verification"):
                    export_outputs.run(
                        workspace,
                        "auto",
                        None,
                        export_root,
                    )
                self.assertEqual(
                    (workspace / "manifest.json").read_bytes(),
                    manifest_before,
                )
                self.assertEqual(tree_snapshot(workspace), tree_before)
                self.assertFalse(export_root.exists())
                observed_stop_events.append(
                    "deletion-ratio-above-eight-percent"
                )
            elif verify_status == "passed":
                export_report = export_outputs.run(
                    workspace,
                    "auto",
                    None,
                    trial_root / "exports",
                )
                export_status = export_report["status"]
                for artifact in export_report["output_artifacts"].values():
                    self.assertIs(artifact["self_check"]["passed"], True)
            else:
                self.fail(f"unexpected verify status: {verify_status}")

        actual_outcome = derive_outcome(
            verdict_counts,
            observed_stop_events,
            verify_status,
            export_status,
        )
        actual_verdicts = {
            decision["candidate_id"]: decision["verdict"]
            for decision in decisions
        }

        # Gold is an evaluator only.  It is first opened after reviews have been
        # retained, formal decisions compiled, and the actual terminal state and
        # stop events derived from product behavior.
        gold = read_json(fixture / "gold.json")
        self.assert_exact_keys(
            gold,
            {
                "schema_version",
                "expected_outcome",
                "expected_verdicts",
                "eligible_delete_anchor_ids",
                "required_stop_events",
                "expected_verify",
                "expected_export",
            },
            f"{fixture.name} gold",
        )
        self.assertIs(gold["schema_version"], 1)
        self.assertIn(gold["expected_outcome"], OUTCOME_VALUES)
        self.assertIsInstance(gold["expected_verdicts"], dict)
        self.assertTrue(
            all(
                isinstance(candidate_id, str)
                and re.fullmatch(r"AD-[0-9]{4}", candidate_id)
                and verdict in {"keep", "delete", "uncertain"}
                for candidate_id, verdict in gold["expected_verdicts"].items()
            )
        )
        self.assertEqual(actual_outcome, gold["expected_outcome"])
        self.assertEqual(actual_verdicts, gold["expected_verdicts"])
        self.assertEqual(verify_status, gold["expected_verify"])
        self.assertEqual(export_status, gold["expected_export"])

        historical_eligible_anchor_ids = set(
            self.assert_string_list(
                gold["eligible_delete_anchor_ids"],
                "gold.eligible_delete_anchor_ids",
            )
        )
        # Anchor identifiers are runtime identities and legitimately drift when
        # their schema changes.  The frozen gold still fixes which candidates
        # and how many occurrences were eligible; deterministic replay maps that
        # semantic gold onto the current candidate ledger after reveal.
        eligible_anchor_ids = {
            anchor["anchor_id"]
            for candidate in candidates
            if gold["expected_verdicts"].get(candidate["candidate_id"])
            == "delete"
            for anchor in candidate["anchors"]
        }
        self.assertEqual(
            len(eligible_anchor_ids),
            len(historical_eligible_anchor_ids),
        )
        deleted_anchor_ids = {
            anchor_id
            for decision in decisions
            if decision["verdict"] == "delete"
            for anchor_id in decision["anchor_ids"]
        }
        correct_anchor_ids = eligible_anchor_ids & deleted_anchor_ids
        false_anchor_ids = deleted_anchor_ids - eligible_anchor_ids
        missed_anchor_ids = eligible_anchor_ids - deleted_anchor_ids

        required_stop_events = set(
            self.assert_string_list(
                gold["required_stop_events"],
                "gold.required_stop_events",
            )
        )
        self.assertTrue(required_stop_events <= STOP_EVENT_VALUES)
        observed_stop_set = set(observed_stop_events)
        missing_stop_events = required_stop_events - observed_stop_set
        extra_stop_events = observed_stop_set - required_stop_events

        source_unchanged = source.read_bytes() == source_before
        v0_unchanged = (
            workspace / "versions" / "v0_original.txt"
        ).read_bytes() == source_before
        self.assertTrue(source_unchanged)
        self.assertTrue(v0_unchanged)

        identities: dict[str, Any] = {
            "input_sha256": common.sha256_file(source),
            "scan_id": scan_report["scan_id"],
            "candidate_set_sha256": scan_report["candidate_set_sha256"],
            "reviews_sha256": common.sha256_file(reviews_path),
            "formal_decisions_sha256": common.sha256_file(decisions_path),
        }
        if apply_output_sha256 is not None:
            identities["apply_output_sha256"] = apply_output_sha256
        if final_output_sha256 is not None:
            identities["final_output_sha256"] = final_output_sha256

        return {
            "trial": fixture.name,
            "outcome": actual_outcome,
            "total_candidates": total_candidates,
            "reviewed_candidates": reviewed_candidates,
            "keep": verdict_counts["keep"],
            "delete": verdict_counts["delete"],
            "uncertain": verdict_counts["uncertain"],
            "candidate_anchors": sum(
                len(candidate["anchors"]) for candidate in candidates
            ),
            "eligible_delete_anchor_ids": sorted(eligible_anchor_ids),
            "deleted_anchor_ids": sorted(deleted_anchor_ids),
            "correct_delete_anchor_ids": sorted(correct_anchor_ids),
            "false_delete_anchor_ids": sorted(false_anchor_ids),
            "missed_delete_anchor_ids": sorted(missed_anchor_ids),
            "required_stop_events": sorted(required_stop_events),
            "observed_stop_events": sorted(observed_stop_set),
            "missing_stop_events": sorted(missing_stop_events),
            "extra_stop_events": sorted(extra_stop_events),
            "verify": verify_status,
            "export": export_status,
            "source_unchanged": source_unchanged,
            "v0_unchanged": v0_unchanged,
            "unauthorized_title_or_blocked_mutation": unauthorized_mutation,
            "identities": identities,
        }

    def test_six_historical_trials_deterministically_replay_pipeline_semantics(
        self,
    ) -> None:
        fixtures = sorted(path for path in FIXTURES.iterdir() if path.is_dir())
        fixture_names = [fixture.name for fixture in fixtures]
        self.assertEqual(len(fixture_names), len(set(fixture_names)))
        self.assertTrue(all(TRIAL_RE.fullmatch(name) for name in fixture_names))
        self.assertEqual(
            len(fixtures),
            self.evidence["trial_counts"]["public_replayable"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outcomes = []
            for fixture in fixtures:
                with self.subTest(trial=fixture.name):
                    outcomes.append(self.replay_trial(fixture, root))

        public = self.evidence["public_trials"]
        non_semantic_fields = {
            "identities",
            "eligible_delete_anchor_ids",
            "deleted_anchor_ids",
            "correct_delete_anchor_ids",
            "false_delete_anchor_ids",
            "missed_delete_anchor_ids",
        }
        self.assertEqual(
            [
                {
                    key: value
                    for key, value in outcome.items()
                    if key not in non_semantic_fields
                }
                for outcome in outcomes
            ],
            [
                {
                    key: value
                    for key, value in outcome.items()
                    if key not in non_semantic_fields
                }
                for outcome in public["outcomes"]
            ],
        )
        self.assert_contract(
            self.evidence["fixture_contract"],
            scope=FIXTURE_SCOPE,
            files=fixture_contract_files(),
            label="fixture contract",
        )

        aggregate = {
            "total_candidate_count": sum(
                item["total_candidates"] for item in outcomes
            ),
            "reviewed_candidate_count": sum(
                len(item["reviewed_candidates"]) for item in outcomes
            ),
            "kept_candidate_count": sum(item["keep"] for item in outcomes),
            "deleted_candidate_count": sum(item["delete"] for item in outcomes),
            "uncertain_candidate_count": sum(
                item["uncertain"] for item in outcomes
            ),
            "candidate_anchor_count": sum(
                item["candidate_anchors"] for item in outcomes
            ),
            "eligible_delete_anchor_count": sum(
                len(item["eligible_delete_anchor_ids"]) for item in outcomes
            ),
            "deleted_anchor_count": sum(
                len(item["deleted_anchor_ids"]) for item in outcomes
            ),
            "correct_delete_anchor_count": sum(
                len(item["correct_delete_anchor_ids"]) for item in outcomes
            ),
            "false_delete_anchor_count": sum(
                len(item["false_delete_anchor_ids"]) for item in outcomes
            ),
            "missed_delete_anchor_count": sum(
                len(item["missed_delete_anchor_ids"]) for item in outcomes
            ),
            "required_stop_count": sum(
                len(item["required_stop_events"]) for item in outcomes
            ),
            "observed_stop_count": sum(
                len(item["observed_stop_events"]) for item in outcomes
            ),
            "honored_stop_count": sum(
                len(
                    set(item["required_stop_events"])
                    & set(item["observed_stop_events"])
                )
                for item in outcomes
            ),
            "missing_stop_count": sum(
                len(item["missing_stop_events"]) for item in outcomes
            ),
            "extra_stop_count": sum(
                len(item["extra_stop_events"]) for item in outcomes
            ),
            "truncation_rescan_count": sum(
                "truncated-anchors-before-rescan"
                in item["observed_stop_events"]
                for item in outcomes
            ),
            "verification_pass_count": sum(
                item["verify"] == "passed" for item in outcomes
            ),
            "export_pass_count": sum(
                item["export"] == "passed" for item in outcomes
            ),
            "source_unchanged_count": sum(
                item["source_unchanged"] for item in outcomes
            ),
            "v0_unchanged_count": sum(
                item["v0_unchanged"] for item in outcomes
            ),
            "unauthorized_title_or_blocked_mutation_count": sum(
                item["unauthorized_title_or_blocked_mutation"]
                for item in outcomes
            ),
        }
        for field, expected in aggregate.items():
            self.assertEqual(public[field], expected)

        # Each metric joins independently produced sets/counts: scan totals versus
        # compiled review IDs, gold-eligible anchors versus formal deletions, and
        # required stops versus observed product gates.
        metrics = self.evidence["release_metrics"]
        expected_metrics = {
            "candidate_review_coverage": (
                aggregate["reviewed_candidate_count"],
                aggregate["total_candidate_count"],
                "reviewed",
                "total",
            ),
            "supported_delete_anchor_recall": (
                aggregate["correct_delete_anchor_count"],
                aggregate["eligible_delete_anchor_count"],
                "correct",
                "eligible",
            ),
            "delete_anchor_precision": (
                aggregate["correct_delete_anchor_count"],
                aggregate["deleted_anchor_count"],
                "correct",
                "deleted",
            ),
            "required_stop_compliance": (
                aggregate["honored_stop_count"],
                aggregate["required_stop_count"],
                "honored",
                "required",
            ),
        }
        for name, (
            numerator,
            denominator,
            numerator_key,
            denominator_key,
        ) in expected_metrics.items():
            with self.subTest(metric=name):
                self.assertGreater(denominator, 0)
                self.assertEqual(metrics[name][numerator_key], numerator)
                self.assertEqual(metrics[name][denominator_key], denominator)
                self.assertEqual(metrics[name]["rate"], numerator / denominator)


if __name__ == "__main__":
    unittest.main()
