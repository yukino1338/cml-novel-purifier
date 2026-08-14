from __future__ import annotations

import json
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_coverage  # noqa: E402
import check_release  # noqa: E402


EXPECTED_CRITICAL_MODULE_THRESHOLDS = {
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
CRITICAL_MODULES = tuple(
    Path(path).stem for path in EXPECTED_CRITICAL_MODULE_THRESHOLDS
)
CI_MATRIX = {
    ("ubuntu-latest", "3.11"),
    ("ubuntu-latest", "3.12"),
    ("ubuntu-latest", "3.13"),
    ("ubuntu-latest", "3.14"),
    ("windows-latest", "3.11"),
    ("windows-latest", "3.14"),
    ("macos-latest", "3.11"),
    ("macos-latest", "3.14"),
}
CHECKOUT_ACTION = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
SETUP_PYTHON_ACTION = "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405"


def coverage_summary(
    *, statements: int = 100, covered: int = 100, branches: int = 10, covered_branches: int = 10
) -> dict[str, int]:
    return {
        "num_statements": statements,
        "covered_lines": covered,
        "num_branches": branches,
        "covered_branches": covered_branches,
    }


class CoverageGateF8Tests(unittest.TestCase):
    def make_root_and_report(self, root: Path) -> dict[str, object]:
        scripts = root / "scripts"
        scripts.mkdir()
        names = (*CRITICAL_MODULES, "other")
        for name in names:
            (scripts / f"{name}.py").write_text("VALUE = 1\n", encoding="utf-8")

        files: dict[str, object] = {}
        for index, name in enumerate(names):
            if index % 3 == 0:
                key = rf"C:\agent\repo\scripts\{name}.py"
            elif index % 3 == 1:
                key = "/" + f"home/runner/repo/scripts/{name}.py"
            else:
                key = f"scripts/{name}.py"
            files[key] = {"summary": coverage_summary()}
        return {"meta": {"branch_coverage": True}, "files": files, "totals": {}}

    def set_exact_critical_thresholds(self, report: dict[str, object]) -> None:
        for raw_path, entry in report["files"].items():  # type: ignore[union-attr]
            path = check_coverage.normalize_source_path(raw_path)
            thresholds = EXPECTED_CRITICAL_MODULE_THRESHOLDS.get(path)
            if thresholds is None:
                continue
            line_min, branch_min = thresholds
            entry["summary"] = coverage_summary(  # type: ignore[index]
                statements=100,
                covered=int(line_min),
                branches=100,
                covered_branches=int(branch_min),
            )

    def test_critical_module_threshold_contract_is_exact(self) -> None:
        self.assertEqual(
            check_coverage.CRITICAL_MODULE_THRESHOLDS,
            EXPECTED_CRITICAL_MODULE_THRESHOLDS,
        )
        self.assertEqual(
            check_coverage.CRITICAL_MODULES,
            tuple(EXPECTED_CRITICAL_MODULE_THRESHOLDS),
        )

    def test_accepts_cross_platform_paths_at_exact_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.make_root_and_report(root)
            self.set_exact_critical_thresholds(report)

            result = check_coverage.evaluate_coverage(report, root)

        self.assertEqual(result.errors, ())
        expected_covered = 100 + sum(
            line_min
            for line_min, _branch_min in EXPECTED_CRITICAL_MODULE_THRESHOLDS.values()
        )
        self.assertAlmostEqual(
            result.line_percent,
            expected_covered * 100 / (100 * (len(CRITICAL_MODULES) + 1)),
        )

    def test_each_critical_threshold_rejects_slightly_lower_coverage(self) -> None:
        for path, (line_min, branch_min) in EXPECTED_CRITICAL_MODULE_THRESHOLDS.items():
            for field, minimum, label in (
                ("covered_lines", line_min, "line"),
                ("covered_branches", branch_min, "branch"),
            ):
                with self.subTest(path=path, metric=label), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    report = self.make_root_and_report(root)
                    self.set_exact_critical_thresholds(report)
                    for raw_path, entry in report["files"].items():  # type: ignore[union-attr]
                        if check_coverage.normalize_source_path(raw_path) == path:
                            entry["summary"][field] = int(minimum) - 1  # type: ignore[index]
                            break

                    result = check_coverage.evaluate_coverage(report, root)

                self.assertTrue(
                    any(error.startswith(f"{path} {label} coverage") for error in result.errors),
                    result.errors,
                )

    def test_rejects_overall_and_critical_threshold_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.make_root_and_report(root)
            entries = report["files"]  # type: ignore[assignment]
            for path, entry in entries.items():  # type: ignore[union-attr]
                normalized = path.replace("\\", "/")
                if normalized.endswith("/other.py"):
                    entry["summary"]["covered_lines"] = 0
                    entry["summary"]["num_statements"] = 300
                elif normalized.endswith("/common.py"):
                    entry["summary"]["covered_lines"] = 94
                elif normalized.endswith("/verify.py"):
                    entry["summary"]["covered_branches"] = 8

            result = check_coverage.evaluate_coverage(report, root)

        joined = "\n".join(result.errors)
        self.assertIn("all scripts line coverage", joined)
        self.assertIn("scripts/common.py line coverage", joined)
        self.assertIn("scripts/verify.py branch coverage", joined)

    def test_rejects_missing_source_file_and_invalid_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.make_root_and_report(root)
            report["files"].pop("scripts/scan_identity.py")  # type: ignore[union-attr]
            result = check_coverage.evaluate_coverage(report, root)

            invalid = check_coverage.evaluate_coverage({"files": []}, root)

        self.assertTrue(any("missing coverage data" in item for item in result.errors))
        self.assertTrue(any("files must be an object" in item for item in invalid.errors))
        self.assertTrue(any("branch coverage enabled" in item for item in invalid.errors))

    def test_load_report_rejects_non_object_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "root must be an object"):
                check_coverage.load_report(path)

            path.write_text('{"files": {}}', encoding="utf-8")
            self.assertEqual(check_coverage.load_report(path), {"files": {}})

    def test_coverage_schema_helpers_reject_corrupt_records(self) -> None:
        self.assertIsNone(check_coverage.normalize_source_path(None))
        self.assertIsNone(check_coverage.normalize_source_path("tests/example.py"))
        self.assertEqual(check_coverage.percentage(0, 0), 100.0)
        for summary, message in (
            (None, "summary must be an object"),
            ({}, "covered_lines must be"),
            (coverage_summary(covered=101), "covered_lines exceeds"),
            (coverage_summary(covered_branches=11), "covered_branches exceeds"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                check_coverage.summary_counts(summary, "scripts/example.py")

    def test_coverage_gate_reports_empty_duplicate_and_unknown_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = check_coverage.evaluate_coverage(
                {"meta": {"branch_coverage": True}, "files": {}}, root
            )
            self.assertIn("contains no Python sources", empty.errors[0])

            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "common.py").write_text("VALUE = 1\n", encoding="utf-8")
            report = {
                "meta": {"branch_coverage": True},
                "files": {
                    "scripts/common.py": {"summary": coverage_summary()},
                    r"C:\repo\scripts\common.py": {"summary": coverage_summary()},
                    "scripts/stale.py": {"summary": coverage_summary()},
                    "scripts/invalid.py": [],
                    "README.md": {},
                }
            }
            result = check_coverage.evaluate_coverage(report, root)

        joined = "\n".join(result.errors)
        self.assertIn("duplicate coverage data", joined)
        self.assertIn("file record must be an object", joined)
        self.assertIn("unknown sources", joined)
        self.assertIn("critical source is missing", joined)

    def test_coverage_main_reports_pass_fail_and_load_error(self) -> None:
        result = check_coverage.CoverageResult(99.5, ())
        with mock.patch.object(sys, "argv", ["check_coverage.py"]), mock.patch.object(
            check_coverage, "load_report", return_value={}
        ), mock.patch.object(
            check_coverage, "evaluate_coverage", return_value=result
        ), redirect_stdout(io.StringIO()) as stdout:
            check_coverage.main()
        self.assertIn("99.50%", stdout.getvalue())

        failing = check_coverage.CoverageResult(89.0, ("too low",))
        with mock.patch.object(sys, "argv", ["check_coverage.py"]), mock.patch.object(
            check_coverage, "load_report", return_value={}
        ), mock.patch.object(
            check_coverage, "evaluate_coverage", return_value=failing
        ), redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            check_coverage.main()
        self.assertIn("too low", stderr.getvalue())

        with mock.patch.object(sys, "argv", ["check_coverage.py"]), mock.patch.object(
            check_coverage, "load_report", side_effect=OSError("unreadable")
        ), redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            check_coverage.main()
        self.assertIn("unreadable", stderr.getvalue())


class ReleaseGateF8Tests(unittest.TestCase):
    def test_skill_routes_keep_basis_detail_to_one_compiler_contract(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        guide = (ROOT / "references" / "judgment-guide.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("references/judgment-guide.md", skill)
        self.assertIn("all detailed verdict, `keep_basis`", skill)
        required = (
            "An ordinary `keep` may omit `keep_basis`.",
            '"cml.keep-basis.v1"',
            "`narrative_context`, `plot_dependency`, and `rule_false_positive`",
            "cover every saved occurrence exactly once",
            "canonical coverage hash",
            "must not carry a basis",
            "never infers one from `reason` or `note`",
            "each final range and text hash once",
            "supported audited escape",
        )
        for fragment in required:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, guide)
        for detail in (
            "automatic_delete_gate",
            "family_similarity",
            '"rule_false_positive"',
        ):
            with self.subTest(no_duplicate=detail):
                self.assertNotIn(detail, skill)

    def make_release_root(self, root: Path) -> None:
        (root / "agents").mkdir()
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / "assets" / "config-templates").mkdir(parents=True)
        (root / "docs" / "images").mkdir(parents=True)
        (root / "tests" / "fixtures" / "malformed").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "SKILL.md").write_text(
            "---\n"
            "name: anonymous-skill\n"
            "description: Safely process anonymous local text when requested.\n"
            "---\n\n"
            "# Anonymous skill\n",
            encoding="utf-8",
        )
        (root / "README.md").write_text("# Anonymous skill\n", encoding="utf-8")
        (root / "LICENSE.txt").write_text(
            (ROOT / "LICENSE.txt").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (root / "AGENTS.md").write_text("# Agent rules\n", encoding="utf-8")
        (root / ".gitignore").write_text("*.cleanwork/\n", encoding="utf-8")
        (root / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
        (root / "pyproject.toml").write_text("[tool.example]\n", encoding="utf-8")
        (root / "requirements-dev.txt").write_text("ruff\n", encoding="utf-8")
        for name in ("hero.webp", "review-desktop.webp", "review-mobile.webp"):
            (root / "docs" / "images" / name).write_bytes(b"RIFF\x04\x00\x00\x00WEBP")
        (root / "agents" / "openai.yaml").write_text(
            "interface:\n"
            "  display_name: Anonymous\n"
            "  short_description: Safe anonymous text processing\n"
            "  default_prompt: Use $cml-novel-purifier for anonymous local text.\n",
            encoding="utf-8",
        )
        (root / ".github" / "workflows" / "ci.yml").write_text(
            'name: CI\n"on": [push]\npermissions:\n  contents: read\n',
            encoding="utf-8",
        )
        (root / "assets" / "config-templates" / "config.json").write_text(
            json.dumps({"enabled": True}),
            encoding="utf-8",
        )
        (root / "assets" / "config-templates" / "records.jsonl").write_text(
            '{"id": 1}\n{"id": 2}\n',
            encoding="utf-8",
        )
        (root / "tests" / "fixtures" / "malformed" / "intentional.jsonl").write_text(
            "not-json\n",
            encoding="utf-8",
        )
        (root / "scripts" / "helpful.py").write_text(
            "import argparse\n"
            "def main():\n"
            "    argparse.ArgumentParser().parse_args()\n"
            "if __name__ == '__main__':\n"
            "    main()\n",
            encoding="utf-8",
        )

    def test_accepts_release_contract_and_excludes_malformed_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            errors = check_release.validate_release(root)

        self.assertEqual(errors, [])

    def test_ci_workflow_structure_freezes_permissions_matrix_actions_and_browser(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )
        self.assertIsInstance(workflow, dict)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            set(workflow["on"]),
            {"push", "pull_request", "workflow_dispatch"},
        )

        jobs = workflow["jobs"]
        self.assertEqual(
            set(jobs),
            {"compatibility", "quality", "performance", "fixed-performance", "browser"},
        )
        for job in jobs.values():
            self.assertNotIn("permissions", job)

        includes = jobs["compatibility"]["strategy"]["matrix"]["include"]
        self.assertTrue(all(set(item) == {"os", "python"} for item in includes))
        self.assertEqual(
            {(item["os"], item["python"]) for item in includes},
            CI_MATRIX,
        )

        steps = [step for job in jobs.values() for step in job["steps"]]
        action_steps = [step for step in steps if "uses" in step]
        self.assertEqual(
            {step["uses"] for step in action_steps},
            {CHECKOUT_ACTION, SETUP_PYTHON_ACTION},
        )
        self.assertEqual(
            sum(step["uses"] == CHECKOUT_ACTION for step in action_steps),
            5,
        )
        self.assertEqual(
            sum(step["uses"] == SETUP_PYTHON_ACTION for step in action_steps),
            5,
        )
        for step in action_steps:
            if step["uses"] == CHECKOUT_ACTION:
                self.assertIs(step.get("with", {}).get("persist-credentials"), False)

        def strings(value: object):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield str(key)
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
            elif isinstance(value, str):
                yield value

        self.assertFalse(
            any(re.search(r"(?i)\bsecrets\s*(?:\.|\[)", value) for value in strings(workflow))
        )

        browser = jobs["browser"]
        self.assertEqual(browser["runs-on"], "ubuntu-latest")
        self.assertEqual(browser["env"]["CML_REQUIRE_BROWSER_TESTS"], "1")
        browser_runs = [step.get("run") for step in browser["steps"] if "run" in step]
        self.assertIn("python -m playwright install --with-deps chromium", browser_runs)
        self.assertIn("python -m unittest tests.test_review_browser_f6", browser_runs)
        for name, job in jobs.items():
            if name == "browser":
                continue
            other_runs = [str(step.get("run", "")) for step in job["steps"]]
            self.assertFalse(any("playwright install" in run for run in other_runs))
            self.assertFalse(any("test_review_browser_f6" in run for run in other_runs))

    def test_reports_frontmatter_yaml_json_and_cli_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            (root / "SKILL.md").write_text(
                "---\nname: anonymous-skill\ndescription: valid\nmetadata: {}\n---\n",
                encoding="utf-8",
            )
            (root / "agents" / "openai.yaml").write_text("interface: [\n", encoding="utf-8")
            (root / "assets" / "config-templates" / "broken.json").write_text(
                "{", encoding="utf-8"
            )
            (root / "assets" / "config-templates" / "broken.jsonl").write_text(
                '{"ok": true}\n{\n', encoding="utf-8"
            )
            (root / "scripts" / "helpful.py").write_text(
                "if __name__ == '__main__':\n    raise SystemExit(3)\n",
                encoding="utf-8",
            )

            errors = check_release.validate_release(root)

        joined = "\n".join(errors)
        self.assertIn("frontmatter keys", joined)
        self.assertIn("agents/openai.yaml", joined)
        self.assertIn("assets/config-templates/broken.json", joined)
        self.assertIn("assets/config-templates/broken.jsonl:2", joined)
        self.assertIn("scripts/helpful.py --help exited with 3", joined)

    def test_openai_interface_rejects_unknown_or_missing_host_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            path = root / "agents" / "openai.yaml"
            path.write_text(
                "interface:\n"
                "  display_name: Anonymous\n"
                "  unknown_contract: not-portable\n",
                encoding="utf-8",
            )

            errors = check_release.validate_openai_interface(root)

        self.assertEqual(
            errors,
            [
                "agents/openai.yaml interface keys must be exactly "
                "default_prompt, display_name, short_description"
            ],
        )

    def test_public_tree_rejects_unknown_private_paths_and_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            (root / "notes-private.md").write_text("history\n", encoding="utf-8")
            (root / "README.md").write_text(
                "private path " + "C:" + "\\Users\\example\\book.txt\n",
                encoding="utf-8",
            )
            (root / "assets" / "config-templates" / "oversized.json").write_bytes(
                b"x" * (check_release.MAX_PUBLIC_FILE_BYTES + 1)
            )
            (root / ".local-design").mkdir()
            (root / ".local-design" / "ignored.md").write_text(
                "C:" + "\\Users\\private\\ignored.txt\n",
                encoding="utf-8",
            )
            (root / "1小说").mkdir()
            (root / "1小说" / "private.txt").write_text("private\n", encoding="utf-8")
            (root / "sample.cleanwork").mkdir()
            (root / "sample.cleanwork" / "manifest.json").write_text("{", encoding="utf-8")

            errors = check_release.validate_public_tree(root)

        joined = "\n".join(errors)
        self.assertIn("top-level release allowlist", joined)
        self.assertIn("private absolute path", joined)
        self.assertIn("exceeds the public file size limit", joined)
        self.assertNotIn(".local-design/ignored.md", joined)
        self.assertNotIn("1小说/private.txt", joined)
        self.assertNotIn("sample.cleanwork/manifest.json", joined)

    def test_public_tree_rejects_site_signatures_outside_rule_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            token = check_release.SITE_SIGNATURE_TOKENS[0]
            (root / "tests" / "test_signature_leak.py").write_text(
                f"# duplicated production fact: {token}\n",
                encoding="utf-8",
            )
            (root / "scripts" / "ad_rules.py").write_text(
                f"# approved production fact source: {token}\n",
                encoding="utf-8",
            )

            errors = check_release.validate_public_tree(root)

        signature_errors = [
            error for error in errors if "production site signature" in error
        ]
        self.assertEqual(len(signature_errors), 1)
        self.assertIn("tests/test_signature_leak.py", signature_errors[0])

    def test_git_tracked_inventory_rejects_disallowed_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            (root / ".git").mkdir()
            private = root / "1小说" / "private.txt"
            private.parent.mkdir()
            private.write_text("private\n", encoding="utf-8")
            tracked = (
                "README.md\0"
                "1小说/private.txt\0"
                "scripts/missing.py\0"
            ).encode("utf-8")
            head = subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=b"f" * 40 + b"\n",
                stderr=b"",
            )
            completed = subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=tracked,
                stderr=b"",
            )

            with mock.patch.object(
                check_release.subprocess,
                "run",
                side_effect=[head, completed],
            ) as run:
                errors = check_release.validate_git_tracked_inventory(root)

        joined = "\n".join(errors)
        self.assertIn("1小说/private.txt is tracked but not allowed", joined)
        self.assertIn("scripts/missing.py is tracked but is not a regular", joined)
        self.assertNotIn("README.md", joined)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_CONFIG_NOSYSTEM"], "1")

    def test_git_inventory_requires_head_nonempty_tracking_and_every_public_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            (root / ".git").mkdir()
            no_head = subprocess.CompletedProcess(
                args=["git"], returncode=128, stdout=b"", stderr=b"no HEAD"
            )
            with mock.patch.object(
                check_release.subprocess, "run", return_value=no_head
            ):
                errors = check_release.validate_git_tracked_inventory(root)
            self.assertTrue(any("HEAD" in error for error in errors))

            head = subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout=b"f" * 40 + b"\n", stderr=b""
            )
            empty = subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout=b"", stderr=b""
            )
            with mock.patch.object(
                check_release.subprocess, "run", side_effect=[head, empty]
            ):
                errors = check_release.validate_git_tracked_inventory(root)
            joined = "\n".join(errors)
            self.assertIn("no tracked public files", joined)
            self.assertIn("is public but not tracked", joined)

    def test_validate_release_includes_git_inventory_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            with mock.patch.object(
                check_release,
                "validate_git_tracked_inventory",
                return_value=["tracked inventory rejected"],
            ) as inventory:
                errors = check_release.validate_release(root)

        inventory.assert_called_once_with(root.resolve())
        self.assertIn("tracked inventory rejected", errors)

    def test_project_gitignore_covers_every_local_release_artifact(self) -> None:
        lines = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertTrue(
            {
                "1小说/",
                "*.cleanwork/",
                ".experiment-work/",
                ".local-design/",
                "benchmarks/",
                "output/",
                "__pycache__/",
                "*.py[cod]",
                "*.pstats",
                ".coverage",
                "coverage*.json",
                ".mypy_cache/",
                ".ruff_cache/",
                ".pytest_cache/",
                ".venv/",
                ".vscode/",
            }.issubset(lines)
        )

    def test_public_documents_reject_generic_absolute_paths_but_tests_may_exercise_them(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            (root / "README.md").write_text(
                "Do not publish 路径D:\\private\\book.txt.\n"
                "Official reference: https://example.com/guide.\n",
                encoding="utf-8",
            )
            (root / "references").mkdir()
            (root / "references" / "private.md").write_text(
                "Do not publish /opt/private/book.txt.\n",
                encoding="utf-8",
            )
            (root / "references" / "root-file.md").write_text(
                "Do not publish /secret.txt.\n",
                encoding="utf-8",
            )
            (root / "references" / "windows-unc.md").write_text(
                "Do not publish 路径\\\\private-host\\share\\book.txt.\n",
                encoding="utf-8",
            )
            (root / "references" / "windows-drive-forward.md").write_text(
                "Do not publish 路径D:/private/book.txt.\n",
                encoding="utf-8",
            )
            (root / "AGENTS.md").write_text(
                "Do not publish 路径//private-host/share/book.txt.\n",
                encoding="utf-8",
            )
            (root / "agents" / "openai.yaml").write_text(
                'interface:\n  display_name: "路径file:///opt/private/book.txt"\n',
                encoding="utf-8",
            )
            skill = root / "SKILL.md"
            skill.write_text(
                skill.read_text(encoding="utf-8")
                + "Use <workspace>/versions/current.txt for 输入/输出.\n"
                + "</summary>\n",
                encoding="utf-8",
            )
            (root / "tests" / "path_vectors.py").write_text(
                "WINDOWS = r'D:\\private\\book.txt'\n"
                "WINDOWS_UNC = r'\\\\private-host\\share\\book.txt'\n"
                "FORWARD_UNC = '//private-host/share/book.txt'\n"
                "POSIX = '/opt/private/book.txt'\n"
                "POSIX_ROOT_FILE = '/secret.txt'\n"
                "FILE_URI = 'file:///opt/private/book.txt'\n",
                encoding="utf-8",
            )

            errors = check_release.validate_public_tree(root)

        joined = "\n".join(errors)
        self.assertIn("README.md contains a private absolute path", joined)
        self.assertIn(
            "references/private.md contains a private absolute path",
            joined,
        )
        self.assertIn(
            "references/root-file.md contains a private absolute path",
            joined,
        )
        self.assertIn(
            "references/windows-unc.md contains a private absolute path",
            joined,
        )
        self.assertIn(
            "references/windows-drive-forward.md contains a private absolute path",
            joined,
        )
        self.assertIn("AGENTS.md contains a private absolute path", joined)
        self.assertIn("agents/openai.yaml contains a private absolute path", joined)
        self.assertNotIn("SKILL.md contains a private absolute path", joined)
        self.assertNotIn("tests/path_vectors.py", joined)

    def test_public_tree_requires_release_metadata_and_recognized_license(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            (root / "LICENSE.txt").write_text("MIT License\n", encoding="utf-8")
            (root / ".gitattributes").unlink()
            (root / "docs" / "images" / "review-mobile.webp").unlink()

            errors = check_release.validate_public_tree(root)

        joined = "\n".join(errors)
        self.assertIn(".gitattributes is missing", joined)
        self.assertIn("docs/images/review-mobile.webp is missing", joined)
        self.assertIn("PolyForm Noncommercial 1.0.0", joined)

    def test_polyform_license_body_notice_and_public_docs_are_consistent(self) -> None:
        license_text = (ROOT / "LICENSE.txt").read_text(encoding="utf-8")

        self.assertEqual(check_release.validate_polyform_noncommercial_license(license_text), [])
        for relative in ("README.md", "SKILL.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("PolyForm Noncommercial License 1.0.0", text)
            self.assertIn("commercial", text)

        official_text, notice = license_text.rstrip("\r\n").rsplit(
            "\n\nRequired Notice: ", 1
        )
        self.assertEqual(
            check_release.validate_polyform_noncommercial_license(official_text + "\n"),
            [
                "LICENSE.txt must contain the complete official PolyForm Noncommercial "
                "1.0.0 text and at least one Required Notice line"
            ],
        )
        self.assertEqual(
            check_release.validate_polyform_noncommercial_license(
                official_text.replace("Acceptance", "Altered Acceptance", 1)
                + "\n\nRequired Notice: "
                + notice
            ),
            [
                "LICENSE.txt must contain the complete official PolyForm Noncommercial "
                "1.0.0 text"
            ],
        )

    def test_non_utf8_encoding_fixtures_must_disable_git_text_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            fixture_dir = root / "tests" / "fixtures" / "encodings"
            fixture_dir.mkdir()
            for relative in check_release.NON_UTF8_PUBLIC_PATHS:
                path = root / relative
                path.write_bytes(b"\xa4\x40\r\n")

            errors = check_release.validate_public_tree(root)

            self.assertEqual(
                sorted(
                    error for error in errors if "must be marked binary or -text" in error
                ),
                [
                    "tests/fixtures/encodings/big5.txt must be marked binary or -text in .gitattributes",
                    "tests/fixtures/encodings/gb18030.txt must be marked binary or -text in .gitattributes",
                ],
            )

            with (root / ".gitattributes").open("a", encoding="utf-8") as handle:
                for relative in sorted(check_release.NON_UTF8_PUBLIC_PATHS):
                    handle.write(f"{relative} binary\n")

            self.assertFalse(
                [
                    error
                    for error in check_release.validate_public_tree(root)
                    if "must be marked binary or -text" in error
                ]
            )

    def test_project_attributes_preserve_non_utf8_fixture_bytes(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        for relative in sorted(check_release.NON_UTF8_PUBLIC_PATHS):
            with self.subTest(relative=relative):
                self.assertIn(f"{relative} binary", attributes)

    def test_git_attribute_check_rejects_a_later_text_override_for_raw_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            fixture_dir = root / "tests" / "fixtures" / "encodings"
            fixture_dir.mkdir()
            for relative in check_release.NON_UTF8_PUBLIC_PATHS:
                (root / relative).write_bytes(b"\xa4\x40\r\n")
            (root / ".gitattributes").write_text(
                "* text=auto eol=lf\n"
                "tests/fixtures/encodings/big5.txt binary\n"
                "tests/fixtures/encodings/gb18030.txt binary\n"
                "tests/fixtures/encodings/*.txt text eol=lf\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(root)], check=True)

            errors = check_release.validate_public_tree(root)

        joined = "\n".join(errors)
        self.assertIn(
            "tests/fixtures/encodings/big5.txt is converted as Git text",
            joined,
        )
        self.assertIn(
            "tests/fixtures/encodings/gb18030.txt is converted as Git text",
            joined,
        )

    def test_public_tree_rejects_stray_file_types_and_scans_all_public_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            (root / "assets" / "private.epub").write_bytes(b"not-an-epub")
            (root / "tests" / "private.bin").write_bytes(b"private")
            (root / "tests" / "private.txt").write_text("private", encoding="utf-8")
            for image in (root / "docs" / "images").iterdir():
                image.unlink()
            (root / "docs" / "images").rmdir()
            (root / "docs").rmdir()
            (root / "docs").write_text("not a directory", encoding="utf-8")
            (root / "references").mkdir()
            (root / "references" / "private.txt").write_text(
                "private", encoding="utf-8"
            )
            (root / "scripts" / "leak.py").write_text(
                'SOURCE = r"' + "C:" + "\\Users\\example\\book.txt" + '"\n',
                encoding="utf-8",
            )

            errors = check_release.validate_public_tree(root)

        joined = "\n".join(errors)
        self.assertIn("assets/private.epub", joined)
        self.assertIn("tests/private.bin", joined)
        self.assertIn("tests/private.txt", joined)
        self.assertIn("docs must be a release-tree directory", joined)
        self.assertIn("references/private.txt", joined)
        self.assertIn("scripts/leak.py contains a private absolute path", joined)

    def test_public_tree_rejects_top_level_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            link = root / "references"
            try:
                link.symlink_to(root / "assets", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            errors = check_release.validate_public_tree(root)

        self.assertTrue(
            any("references is a release-tree link or junction" in error for error in errors)
        )

    def test_release_validators_reject_junction_without_traversing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            outside_root = Path(outside)
            self.make_release_root(root)
            (outside_root / "secret.json").write_text("{", encoding="utf-8")
            (outside_root / "secret.md").write_text(
                "[missing](outside-target.md)\n", encoding="utf-8"
            )
            link = root / "scripts" / "linked-private"
            if os.name == "nt":
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(outside_root)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if result.returncode != 0:
                    self.skipTest(f"directory junctions are unavailable: {result.stderr}")
                self.assertFalse(link.is_symlink())
            else:
                try:
                    link.symlink_to(outside_root, target_is_directory=True)
                except OSError as exc:
                    self.skipTest(f"directory symlinks are unavailable: {exc}")

            try:
                public_errors = check_release.validate_public_tree(root)
                json_errors = check_release.validate_json_files(root)
                markdown_errors = check_release.validate_markdown_links(root)
            finally:
                if link.exists():
                    os.rmdir(link) if os.name == "nt" else link.unlink()

        self.assertTrue(
            any("scripts/linked-private is a release-tree link or junction" in error for error in public_errors)
        )
        self.assertNotIn("secret.json", "\n".join(json_errors))
        self.assertNotIn("secret.md", "\n".join(markdown_errors))
        self.assertNotIn("outside-target.md", "\n".join(markdown_errors))

    def test_markdown_links_require_local_targets_and_image_alt_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            (root / "README.md").write_text(
                "[Skill](SKILL.md)\n"
                "[missing](references/missing.md)\n"
                "![](assets/missing.webp)\n"
                "[external](https://example.com/)\n",
                encoding="utf-8",
            )

            errors = check_release.validate_markdown_links(root)

        joined = "\n".join(errors)
        self.assertIn("references/missing.md", joined)
        self.assertIn("assets/missing.webp", joined)
        self.assertIn("image alt text", joined)
        self.assertNotIn("https://example.com", joined)

    def test_rejects_invalid_skill_name_and_missing_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            (root / "SKILL.md").write_text(
                "---\nname: Invalid_Name\ndescription: '<bad>'\n---\n",
                encoding="utf-8",
            )
            (root / ".github" / "workflows" / "ci.yml").unlink()

            errors = check_release.validate_release(root)

        joined = "\n".join(errors)
        self.assertIn("lowercase letters, digits, and hyphens", joined)
        self.assertIn("angle brackets", joined)
        self.assertIn(".github/workflows/ci.yml is missing", joined)

    def test_frontmatter_and_yaml_shape_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            skill = root / "SKILL.md"
            cases = (
                ("", "must start"),
                ("---\nname: value\n", "no closing"),
                ("---\n[\n---\n", "invalid YAML"),
                ("---\n[]\n---\n", "must be a YAML object"),
                ("---\nname: valid\ndescription: ''\n---\n", "non-empty"),
                (
                    "---\nname: valid\ndescription: " + "x" * 1025 + "\n---\n",
                    "at most 1024",
                ),
            )
            for content, message in cases:
                with self.subTest(message=message):
                    skill.write_text(content, encoding="utf-8")
                    self.assertTrue(
                        any(message in error for error in check_release.validate_frontmatter(root))
                    )

            scalar = root / "scalar.yaml"
            scalar.write_text("[]\n", encoding="utf-8")
            _, error = check_release.load_yaml_mapping(scalar, root)
            self.assertIn("root must be a YAML object", error)
            self.assertTrue(check_release.display_path(Path("Z:/outside.yaml"), root))

    def test_json_and_cli_inspection_failures_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            generated = root / "sample.cleanwork"
            generated.mkdir()
            (generated / "ignored.json").write_text("{", encoding="utf-8")
            (root / "assets" / "config-templates" / "blank-lines.jsonl").write_text(
                '\n{"ok": true}\n', encoding="utf-8"
            )
            (root / "assets" / "config-templates" / "non-utf8.json").write_bytes(
                b"\xff"
            )
            (root / "scripts" / "syntax.py").write_text("if [\n", encoding="utf-8")

            json_errors = check_release.validate_json_files(root)
            _, discovery_errors = check_release.discover_cli_scripts(root)

            self.assertEqual(len(json_errors), 1)
            self.assertIn("non-utf8.json", json_errors[0])
            self.assertEqual(len(discovery_errors), 1)
            self.assertIn("syntax.py", discovery_errors[0])

            helpful = root / "scripts" / "helpful.py"
            with mock.patch.object(
                check_release,
                "discover_cli_scripts",
                return_value=([helpful], []),
            ), mock.patch.object(
                check_release.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["python"], 20),
            ):
                help_errors = check_release.validate_cli_help(root)
            self.assertIn("--help failed", help_errors[0])

    def test_json_validation_never_reads_local_or_unknown_release_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_release_root(root)
            private_paths = (
                root / ".local-design" / "private.json",
                root / "1小说" / "metadata.json",
                root / "sample.cleanwork" / "manifest.json",
                root / "unknown" / "private.jsonl",
                root / "coverage-private.json",
            )
            for path in private_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{", encoding="utf-8")

            errors = check_release.validate_json_files(root)

        joined = "\n".join(errors)
        for path in private_paths:
            self.assertNotIn(path.name, joined)
        self.assertEqual(errors, [])

    def test_release_main_reports_success_and_failure(self) -> None:
        with mock.patch.object(sys, "argv", ["check_release.py"]), mock.patch.object(
            check_release, "validate_release", return_value=[]
        ), redirect_stdout(io.StringIO()) as stdout:
            check_release.main()
        self.assertIn("passed", stdout.getvalue())

        with mock.patch.object(sys, "argv", ["check_release.py"]), mock.patch.object(
            check_release, "validate_release", return_value=["bad contract"]
        ), redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            check_release.main()
        self.assertIn("bad contract", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
