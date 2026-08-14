from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests/support_cross_platform.py"
NORMALIZED_TEXT = (
    "匿名😀样本\n"
    "第一章 起点\n"
    "正文甲，ASCII v1.2.3。\n"
)
SUPPORTED_CI = {
    ("ubuntu-latest", "3.11"),
    ("ubuntu-latest", "3.12"),
    ("ubuntu-latest", "3.13"),
    ("ubuntu-latest", "3.14"),
    ("windows-latest", "3.11"),
    ("windows-latest", "3.14"),
    ("macos-latest", "3.11"),
    ("macos-latest", "3.14"),
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CrossPlatformF7Tests(unittest.TestCase):
    def run_profile(self, profile: str) -> dict[str, object]:
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = "0"
        if profile == "utf8":
            env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
            encoding = "utf-8"
        elif profile == "cp936":
            env.update({"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp936"})
            encoding = "cp936"
        elif profile == "c-utf8":
            env.update(
                {
                    "LC_ALL": "C.UTF-8",
                    "LANG": "C.UTF-8",
                    "PYTHONUTF8": "0",
                    "PYTHONIOENCODING": "utf-8",
                }
            )
            encoding = "utf-8"
        else:  # pragma: no cover - test helper contract
            raise AssertionError(f"unknown locale profile: {profile}")

        process = subprocess.run(
            [sys.executable, str(PROBE)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            check=False,
        )
        stderr = process.stderr.decode(encoding, errors="replace")
        self.assertEqual(process.returncode, 0, f"profile={profile}: {stderr}")
        return json.loads(process.stdout.decode(encoding))

    def test_minimum_python_and_ci_matrix_contract(self) -> None:
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["tool"]["ruff"]["target-version"], "py311")

        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        entries = set(
            re.findall(
                r'- os: (ubuntu-latest|windows-latest|macos-latest)\s+python: "(3\.\d+)"',
                workflow,
            )
        )
        self.assertEqual(entries, SUPPORTED_CI)
        self.assertIn("permissions:\n  contents: read", workflow)
        action_refs = re.findall(r"uses:\s+[^@\s]+@([^\s#]+)", workflow)
        self.assertTrue(action_refs)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", value) for value in action_refs))
        self.assertIn("--require-comparable-baseline", workflow)

    def test_locale_path_crlf_and_epub_contract_is_repeatable(self) -> None:
        profiles = ["utf8", "cp936"]
        if sys.platform.startswith("linux"):
            profiles.append("c-utf8")

        expected_text_sha = sha256_text(NORMALIZED_TEXT)
        expected_semantic_sha = sha256_text(NORMALIZED_TEXT)
        semantic_results: list[tuple[object, ...]] = []
        for profile in profiles:
            with self.subTest(profile=profile):
                first = self.run_profile(profile)
                second = self.run_profile(profile)
                self.assertEqual(first, second)
                self.assertTrue(first["unicode_path_roundtrip"])
                self.assertEqual(first["crlf_count"], 3)
                self.assertEqual(first["v1_sha256"], expected_text_sha)
                self.assertEqual(first["semantic_sha256"], expected_semantic_sha)
                self.assertEqual(first["markdown_semantic_sha256"], expected_semantic_sha)
                self.assertEqual(first["epub_semantic_sha256"], expected_semantic_sha)
                self.assertTrue(first["epub_passed"])
                self.assertGreaterEqual(first["epub_chapter_count"], 1)
                if profile == "cp936":
                    self.assertIn(first["stdout_encoding"], {"cp936", "gbk"})
                else:
                    self.assertIn("utf", first["stdout_encoding"])
                semantic_results.append(
                    (
                        first["v1_sha256"],
                        first["semantic_sha256"],
                        first["markdown_semantic_sha256"],
                        first["epub_semantic_sha256"],
                        first["epub_chapter_count"],
                    )
                )
        self.assertEqual(len(set(semantic_results)), 1)


if __name__ == "__main__":
    unittest.main()
