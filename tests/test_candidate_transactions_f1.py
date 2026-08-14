from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_ads  # noqa: E402
import scan_blocked  # noqa: E402
import scan_titles  # noqa: E402


class CandidateTransactionsF1Tests(unittest.TestCase):
    def bind_workspace(self, root: Path) -> Path:
        source = root / "sample-a.txt"
        source.write_text("第一章 示例\n正文甲。\n", encoding="utf-8")
        workspace = root / "sample-a.txt.cleanwork"
        preprocess.run(source, str(workspace))
        parse_structure.run(workspace)
        return workspace

    def write_old(self, workspace: Path, relative: str, content: bytes) -> Path:
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def assert_unchanged_after_failure(self, workspace: Path, paths: list[Path], before_manifest: bytes) -> None:
        self.assertEqual((workspace / "manifest.json").read_bytes(), before_manifest)
        self.assertFalse((workspace / ".runs").exists())
        for path in paths:
            self.assertEqual(path.read_bytes(), self.old_bytes[path])

    def remember(self, paths: list[Path]) -> None:
        self.old_bytes = {path: path.read_bytes() for path in paths}

    def test_ad_pages_and_report_are_not_published_when_report_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            ads = self.write_old(workspace, "candidates/ads.jsonl", b'{"old":"ads"}\n')
            page_one = self.write_old(workspace, "candidates/ads_pages/ads_page_001.jsonl", b'{"old":1}\n')
            page_two = self.write_old(workspace, "candidates/ads_pages/ads_page_002.jsonl", b'{"old":2}\n')
            stale = self.write_old(workspace, "candidates/ads_pages/ads_page_003.jsonl", b'{"old":3}\n')
            report = self.write_old(workspace, "report/ads_scan_report.json", b'{"old":"report"}\n')
            paths = [ads, page_one, page_two, stale, report]
            self.remember(paths)
            before_manifest = (workspace / "manifest.json").read_bytes()
            candidates = [
                {"candidate_id": "AD-1", "sample": "first"},
                {"candidate_id": "AD-2", "sample": "second"},
            ]
            summary = {
                "candidate_count": 1,
                "first_page_count": 1,
                "total_candidate_count": 2,
                "page_size": 1,
                "page_count": 2,
            }

            with (
                mock.patch.object(scan_ads, "scan_candidates", return_value=(candidates, summary)),
                mock.patch.object(scan_ads, "write_json", side_effect=OSError("injected report failure")),
                self.assertRaisesRegex(OSError, "injected report failure"),
            ):
                scan_ads.run(workspace, "versions/v1_preprocessed.txt", "candidates/ads.jsonl", 12, 1, 10)

            self.assert_unchanged_after_failure(workspace, paths, before_manifest)

    def test_empty_ad_scan_commits_an_empty_page_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            summary = {
                "candidate_count": 0,
                "first_page_count": 0,
                "total_candidate_count": 0,
                "page_size": 1,
                "page_count": 0,
            }

            with mock.patch.object(scan_ads, "scan_candidates", return_value=([], summary)):
                scan_ads.run(workspace, "versions/v1_preprocessed.txt", "candidates/ads.jsonl", 12, 1, 10)

            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stages"]["2_ads"]["status"], "candidates_ready")
            self.assertTrue((workspace / "candidates/ads_pages").is_dir())
            self.assertEqual(list((workspace / "candidates/ads_pages").iterdir()), [])
            self.assertFalse((workspace / ".runs").exists())

    def test_title_and_blocked_candidates_are_not_published_when_reports_fail(self) -> None:
        cases = (
            (scan_titles, "candidates/titles.jsonl", "report/titles_scan_report.json", lambda: scan_titles.run(workspace, "auto", "candidates/titles.jsonl")),
            (scan_blocked, "candidates/blocked.jsonl", "report/blocked_scan_report.json", lambda: scan_blocked.run(workspace, "auto", "candidates/blocked.jsonl", 10)),
        )
        for module, output_value, report_value, call in cases:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                workspace = self.bind_workspace(Path(directory))
                output = self.write_old(workspace, output_value, b'{"old":"candidate"}\n')
                report = self.write_old(workspace, report_value, b'{"old":"report"}\n')
                paths = [output, report]
                self.remember(paths)
                before_manifest = (workspace / "manifest.json").read_bytes()

                with (
                    mock.patch.object(module, "write_json", side_effect=OSError("injected report failure")),
                    self.assertRaisesRegex(OSError, "injected report failure"),
                ):
                    call()

                self.assert_unchanged_after_failure(workspace, paths, before_manifest)

    def test_draft_and_formal_decisions_are_not_published_when_reports_fail(self) -> None:
        for module, output_value, report_value in (
            (make_ad_decisions, "decisions/ads_decisions.draft.jsonl", "report/ad_decision_draft_report.json"),
            (finalize_ad_decisions, "decisions/ads_decisions.jsonl", "report/ad_decision_formal_report.json"),
        ):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                workspace = self.bind_workspace(Path(directory))
                candidate = {
                    "candidate_id": "AD-1",
                    "anchors_truncated": False,
                    "anchors": [
                        {
                            "offset": 0,
                            "end": 2,
                            "line": 1,
                            "original": "第一",
                            "prefix": "",
                            "suffix": "章",
                        }
                    ],
                }
                summary = {
                    "candidate_count": 1,
                    "first_page_count": 1,
                    "total_candidate_count": 1,
                    "page_size": 1,
                    "page_count": 1,
                    "performance": {"timings_seconds": {}},
                }
                with mock.patch.object(
                    scan_ads, "scan_candidates", return_value=([candidate], summary)
                ):
                    scan_report = scan_ads.run(
                        workspace,
                        "versions/v1_preprocessed.txt",
                        "candidates/ads.jsonl",
                        12,
                        1,
                        10,
                    )
                scanned_candidate = json.loads(
                    (workspace / "candidates/ads.jsonl").read_text(encoding="utf-8")
                )
                review = {
                    "scan_id": scan_report["scan_id"],
                    "candidate_id": "AD-1",
                    "candidate_fingerprint": scanned_candidate["candidate_fingerprint"],
                    "verdict": "keep",
                    "confidence": 1,
                    "reason": "normal",
                }
                self.write_old(
                    workspace,
                    "decisions/ads_agent_reviews.jsonl",
                    (json.dumps(review) + "\n").encode(),
                )
                if module is finalize_ad_decisions:
                    make_ad_decisions.run(
                        workspace,
                        "candidates/ads.jsonl",
                        "decisions/ads_decisions.draft.jsonl",
                        "meta/book_profile.json",
                        False,
                    )
                output = self.write_old(workspace, output_value, b'{"old":"decision"}\n')
                report = self.write_old(workspace, report_value, b'{"old":"report"}\n')
                paths = [output, report]
                self.remember(paths)
                before_manifest = (workspace / "manifest.json").read_bytes()

                if module is make_ad_decisions:
                    patches = [
                        mock.patch.object(module, "build_draft_decisions", return_value=([{"candidate_id": "AD-1", "verdict": "keep"}], {})),
                        mock.patch.object(module, "write_json", side_effect=OSError("injected report failure")),
                    ]
                    def call() -> object:
                        return module.run(
                            workspace,
                            "candidates/ads.jsonl",
                            output_value,
                            "meta/book_profile.json",
                            False,
                        )
                else:
                    patches = [
                        mock.patch.object(module, "compile_formal_decisions", return_value=[{"candidate_id": "AD-1", "verdict": "keep"}]),
                        mock.patch.object(module, "write_json", side_effect=OSError("injected report failure")),
                    ]
                    def call() -> object:
                        return module.run(
                            workspace,
                            "candidates/ads.jsonl",
                            "decisions/ads_agent_reviews.jsonl",
                            "decisions/ads_decisions.draft.jsonl",
                            output_value,
                        )

                with patches[0], patches[1], self.assertRaisesRegex(OSError, "injected report failure"):
                    call()

                self.assert_unchanged_after_failure(workspace, paths, before_manifest)


if __name__ == "__main__":
    unittest.main()
