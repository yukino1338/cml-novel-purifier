from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_ads  # noqa: E402
import scan_blocked  # noqa: E402
import scan_identity  # noqa: E402
import scan_titles  # noqa: E402


class ScanContractsF5Tests(unittest.TestCase):
    def bound_files(self, root: Path, text: str) -> tuple[Path, Path, dict[str, object]]:
        input_path = root / "input.txt"
        structure_path = root / "chapters.json"
        input_path.write_text(text, encoding="utf-8")
        chapters, report = parse_structure.parse(text)
        structure: dict[str, object] = {
            "schema_version": 2,
            "input_sha256": common.sha256_file(input_path),
            "chapters": chapters,
            "structure_confidence": report["structure_confidence"],
            "fallback_chunking": report["fallback_chunking"],
            "fallback_chunks": report["fallback_chunks"],
            "slices": report["slices"],
            "locators": report["locators"],
        }
        common.write_json(structure_path, structure)
        return input_path, structure_path, structure

    def test_structure_artifact_accepts_chapters_body_and_complete_fallback(self) -> None:
        cases = (
            "第一章 起点\n正文。\n",
            "没有章节的短正文。\n",
            "长正文。" * 30_001,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, text in enumerate(cases):
                with self.subTest(index=index):
                    case_root = root / str(index)
                    case_root.mkdir()
                    input_path, structure_path, _ = self.bound_files(case_root, text)
                    loaded = scan_identity.load_bound_structure(input_path, structure_path)
                    self.assertEqual(loaded["input_sha256"], common.sha256_file(input_path))

    def test_stale_malformed_and_incomplete_structure_artifacts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path, structure_path, baseline = self.bound_files(
                root,
                "第一章 起点\n正文。\n",
            )
            cases: list[dict[str, object]] = []

            stale = copy.deepcopy(baseline)
            stale["input_sha256"] = "0" * 64
            cases.append(stale)

            unknown_kind = copy.deepcopy(baseline)
            unknown_kind["slices"][0]["kind"] = "unknown"  # type: ignore[index]
            unknown_kind["locators"] = copy.deepcopy(unknown_kind["slices"])
            cases.append(unknown_kind)

            mismatched_chapter = copy.deepcopy(baseline)
            mismatched_chapter["chapters"][0]["title"] = "伪造标题"  # type: ignore[index]
            cases.append(mismatched_chapter)

            undeclared_fallback = copy.deepcopy(baseline)
            undeclared_fallback["slices"][0]["kind"] = "fallback_chunk"  # type: ignore[index]
            undeclared_fallback["locators"] = copy.deepcopy(undeclared_fallback["slices"])
            cases.append(undeclared_fallback)

            for index, structure in enumerate(cases):
                with self.subTest(index=index):
                    common.write_json(structure_path, structure)
                    with self.assertRaises(scan_identity.ScanIdentityError):
                        scan_identity.load_bound_structure(input_path, structure_path)

    def test_invalid_scanner_parameters_are_rejected(self) -> None:
        ads_cases = (
            {"min_chars": 0},
            {"max_candidates": 0},
            {"max_candidates": scan_ads.MAX_PAGE_SIZE + 1},
            {"max_anchors": 0},
            {"max_anchors": True},
            {"near_scan_scope": "partial"},
            {"near_boundary_chars": -1},
        )
        for values in ads_cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                scan_ads.scan_candidates("正文。", **values)
        for value in (0, -1, True, scan_blocked.MAX_CANDIDATES + 1):
            with self.subTest(blocked=value), self.assertRaises(ValueError):
                scan_blocked.scan_text("正文。", value)

    def test_all_scanners_validate_structure_before_candidate_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "anonymous.txt"
            source.write_text("第一章 起点\n正文。\n", encoding="utf-8")
            workspace = preprocess.run(source)
            parse_structure.run(workspace)

            cases = (
                (
                    scan_ads,
                    lambda: scan_ads.run(
                        workspace,
                        "versions/v1_preprocessed.txt",
                        "candidates/ads.jsonl",
                        12,
                        20,
                        20,
                    ),
                ),
                (
                    scan_titles,
                    lambda: scan_titles.run(workspace, "auto", "candidates/titles.jsonl"),
                ),
                (
                    scan_blocked,
                    lambda: scan_blocked.run(workspace, "auto", "candidates/blocked.jsonl", 20),
                ),
            )
            for module, call in cases:
                with self.subTest(module=module.__name__), mock.patch.object(
                    module,
                    "load_bound_structure",
                    side_effect=scan_identity.ScanIdentityError("stale structure"),
                ), self.assertRaisesRegex(scan_identity.ScanIdentityError, "stale structure"):
                    call()


if __name__ == "__main__":
    unittest.main()
