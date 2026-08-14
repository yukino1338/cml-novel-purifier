from __future__ import annotations

import builtins
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import normalize_layout as layout  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_blocked  # noqa: E402
import scan_titles  # noqa: E402


def healthy_candidate(encoding: str, score: float, text: str) -> tuple[str, dict[str, object]]:
    return text, {
        "encoding": encoding,
        "strict_decode": True,
        "score": score,
        "metrics": {
            "replacement_char_count": 0,
            "control_char_count": 0,
            "invalid_unicode_count": 0,
            "non_whitespace_char_count": len(text),
            "text_char_count": len(text),
        },
        "rejection_reason": None,
    }


def rejected_candidate(encoding: str, score: float = 0.0) -> tuple[None, dict[str, object]]:
    return None, {
        "encoding": encoding,
        "strict_decode": False,
        "score": score,
        "metrics": {},
        "rejection_reason": "strict_decode_failed",
    }


class PreprocessCoverageF7Tests(unittest.TestCase):
    def test_encoding_helpers_cover_alias_invalid_bom_and_strict_variants(self) -> None:
        self.assertEqual(preprocess._canonical_encoding("latin1"), "latin-1")
        self.assertEqual(preprocess._canonical_encoding("utf_16_le"), "utf-16-le")
        self.assertIsNone(preprocess._canonical_encoding("not-an-encoding"))
        self.assertIsNone(preprocess._canonical_encoding(None))  # type: ignore[arg-type]
        self.assertEqual(preprocess._bom_encoding(b"\xff\xfeA\x00"), "utf-16-le")
        self.assertIsNone(preprocess._bom_encoding(b"plain"))
        self.assertTrue(preprocess._bom_is_compatible("utf-8-sig", "utf-8"))
        self.assertTrue(preprocess._bom_is_compatible("utf-16-le", "utf-16"))
        self.assertFalse(preprocess._bom_is_compatible("utf-16-le", "utf-8"))
        self.assertEqual(preprocess._decode_strict(b"\xff\xfeA\x00", "utf-16-le"), "A")
        self.assertEqual(preprocess._decode_strict(b"\xfe\xff\x00A", "utf-16-be"), "A")
        self.assertEqual(preprocess._decode_strict(b"abc", "ascii"), "abc")

    def test_detection_failures_cover_every_selection_gate(self) -> None:
        cases = (
            (b"text", "cp1252", "unsupported_explicit_encoding"),
            (b"\xff\xfe\x00\x00", None, "unsupported_bom"),
            (b"\xef\xbb\xbf" + "正文".encode("utf-8"), "utf-16", "explicit_encoding_conflicts_with_bom"),
            (b"", None, "low_text_quality"),
        )
        for raw, encoding, reason in cases:
            with self.subTest(reason=reason):
                text, report = preprocess.detect_and_decode(raw, encoding)
                self.assertIsNone(text)
                self.assertEqual(report["blocked_reason"], reason)

        with mock.patch.object(preprocess, "_candidate", return_value=rejected_candidate("utf-8-sig")):
            text, report = preprocess.detect_and_decode(b"\xef\xbb\xbfbody")
        self.assertIsNone(text)
        self.assertEqual(report["blocked_reason"], "no_strict_decoder")

        low_text, low = healthy_candidate("utf-8-sig", 50.0, "正文")
        with (
            mock.patch.object(preprocess, "_candidate", return_value=(low_text, low)),
            mock.patch.object(preprocess, "_healthy_candidate", return_value=False),
        ):
            text, report = preprocess.detect_and_decode(b"\xef\xbb\xbfbody")
        self.assertIsNone(text)
        self.assertEqual(report["blocked_reason"], "low_text_quality")

        with mock.patch.object(preprocess, "_candidate", return_value=rejected_candidate("ascii")):
            text, report = preprocess.detect_and_decode(b"bad", "ascii")
        self.assertIsNone(text)
        self.assertEqual(report["blocked_reason"], "no_strict_decoder")

        low_text, low = healthy_candidate("ascii", 50.0, "正文")
        with (
            mock.patch.object(preprocess, "_candidate", return_value=(low_text, low)),
            mock.patch.object(preprocess, "_healthy_candidate", return_value=False),
        ):
            text, report = preprocess.detect_and_decode(b"body", "ascii")
        self.assertIsNone(text)
        self.assertEqual(report["blocked_reason"], "low_text_quality")

    def test_auto_detection_ranking_covers_no_decoder_ambiguity_and_confidence(self) -> None:
        def run_with(
            mapping: dict[str, tuple[str | None, dict[str, object]]],
            raw: bytes = b"12345678" + "正文".encode("utf-8"),
        ):
            with mock.patch.object(preprocess, "_candidate", side_effect=lambda _raw, enc: mapping[enc]):
                return preprocess.detect_and_decode(raw)

        none = {encoding: rejected_candidate(encoding) for encoding in preprocess.AUTO_ENCODINGS}
        text, report = run_with(none)
        self.assertIsNone(text)
        self.assertEqual(report["blocked_reason"], "no_strict_decoder")

        unhealthy: dict[str, tuple[str | None, dict[str, object]]] = dict(none)
        bad_text, bad = healthy_candidate("gb18030", 60.0, "正文")
        unhealthy["gb18030"] = (bad_text, bad)
        text, report = run_with(unhealthy)
        self.assertIsNone(text)
        self.assertEqual(report["blocked_reason"], "low_text_quality")

        good: dict[str, tuple[str | None, dict[str, object]]] = dict(none)
        good["gb18030"] = healthy_candidate("gb18030", 95.0, "正文内容")
        text, report = run_with(good)
        self.assertEqual(text, "正文内容")
        self.assertEqual(report["selection_reason"], "quality_score")

        ambiguous = dict(good)
        ambiguous["big5"] = healthy_candidate("big5", 94.0, "另一正文")
        text, report = run_with(ambiguous, b"short")
        self.assertIsNone(text)
        self.assertEqual(report["blocked_reason"], "ambiguous_strict_decoding")
        text, report = run_with(ambiguous)
        self.assertIsNone(text)
        self.assertEqual(report["blocked_reason"], "ambiguous_strict_decoding")

        low_confidence = dict(good)
        low_confidence["big5"] = healthy_candidate("big5", 80.0, "另一正文")
        text, report = run_with(low_confidence)
        self.assertIsNone(text)
        self.assertEqual(report["blocked_reason"], "low_text_quality")

    def test_decode_and_normalize_cover_success_failure_and_removed_characters(self) -> None:
        text, encoding, replacements = preprocess.decode_bytes("正文内容".encode(), "utf-8")
        self.assertEqual((text, encoding, replacements), ("正文内容", "utf-8", 0))
        with self.assertRaisesRegex(UnicodeError, "encoding detection blocked"):
            preprocess.decode_bytes(b"", None)

        normalized, metrics = preprocess.normalize_text("甲\r\n\ufeff乙\r丙\t丁")
        self.assertEqual(normalized, "甲\n乙\n丙\t丁")
        self.assertEqual(metrics["zero_width_removed"], 1)
        with self.assertRaisesRegex(ValueError, "disallowed_control_character"):
            preprocess.normalize_text("甲\x01乙")
        empty, empty_metrics = preprocess.normalize_text("")
        self.assertEqual(empty, "")
        self.assertEqual(empty_metrics["line_count"], 0)

    def test_main_prints_workspace_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            report = workspace / "report" / "preprocess_report.json"
            report.parent.mkdir()
            report.write_text(
                json.dumps({"encoding_detection": {"blocked": False}}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(sys, "argv", ["preprocess.py", "book.txt"]),
                mock.patch.object(preprocess, "run", return_value=workspace) as run,
                mock.patch("builtins.print") as printer,
            ):
                preprocess.main()
        run.assert_called_once()
        self.assertEqual(printer.call_count, 2)


class ParseStructureCoverageF7Tests(unittest.TestCase):
    def test_chinese_number_parser_covers_units_wan_empty_and_invalid(self) -> None:
        expected = {
            "12": 12,
            "": None,
            "十": 10,
            "二十": 20,
            "一百零二": 102,
            "一千二百三十四": 1234,
            "一万二千三百四十五": 12345,
            "甲": None,
        }
        for value, result in expected.items():
            with self.subTest(value=value):
                self.assertEqual(parse_structure.parse_cn_number(value), result)

    def test_match_chapter_covers_all_public_forms_and_rejection_guards(self) -> None:
        valid = ("第一章 开始", "卷二 风起", "序章", "Chapter-3 End", "4、标题", "5 标题")
        for line in valid:
            with self.subTest(line=line):
                self.assertIsNotNone(parse_structure.match_chapter(line))
        invalid = (
            "",
            "甲" * 81,
            "“第一章 对话”",
            "目录 第一章",
            "第一章 " + "长" * 46,
            "第一章 这是正文。",
            "普通正文",
        )
        for line in invalid:
            with self.subTest(line=line):
                self.assertIsNone(parse_structure.match_chapter(line))

    def test_flags_fallback_and_slice_validation_cover_boundaries(self) -> None:
        self.assertEqual(parse_structure.chapter_flags(100), ["very_short"])
        self.assertEqual(parse_structure.chapter_flags(300), [])
        self.assertEqual(parse_structure.chapter_flags(20_001), ["very_long"])
        text = "abc\ndef\nghi"
        self.assertEqual(parse_structure.fallback_end(text, len(text), 0), len(text))
        self.assertEqual(parse_structure.fallback_end(text, 8, 2), 8)
        self.assertEqual(parse_structure.fallback_end(text, 2, 2), 4)
        self.assertEqual(parse_structure.fallback_end("abcdef", 2, 2), 6)
        chunks = parse_structure.build_fallback_chunks("abcdefghij", 3)
        self.assertEqual(chunks[-1]["end_offset"], 10)
        self.assertEqual(parse_structure.expected_min_chapters(1), 1)
        self.assertEqual(parse_structure.expected_min_chapters(500_000), 5)

        valid = [{"start_offset": 0, "heading_end_offset": 1, "end_offset": 3}]
        self.assertTrue(parse_structure.validate_document_slices("abc", valid))
        self.assertFalse(parse_structure.validate_document_slices("abc", []))
        for bad in (
            [{"start_offset": True, "heading_end_offset": 1, "end_offset": 3}],
            [{"start_offset": 1, "heading_end_offset": 1, "end_offset": 3}],
            [{"start_offset": 0, "heading_end_offset": 4, "end_offset": 3}],
            [{"start_offset": 0, "heading_end_offset": 0, "end_offset": 2}],
        ):
            self.assertFalse(parse_structure.validate_document_slices("abc", bad))

    def test_document_slices_cover_fallback_body_and_front_matter(self) -> None:
        text = "作品说明\n第一章 开始\n正文"
        chapters, report = parse_structure.parse(text)
        self.assertEqual(report["slices"][0]["kind"], "front_matter")
        self.assertEqual(report["slices"][1]["kind"], "chapter")
        body = parse_structure.build_document_slices("正文", [], [])
        self.assertEqual(body[0]["kind"], "body")
        fallback = parse_structure.build_document_slices(
            "正文",
            [],
            [{"start_offset": 0, "end_offset": 2, "index": 1}],
        )
        self.assertEqual(fallback[0]["heading_end_offset"], 0)
        self.assertTrue(chapters)

    def test_confidence_and_parse_cover_duplicate_non_monotonic_long_and_fallback(self) -> None:
        none = parse_structure.estimate_structure_confidence(10, [], 1, 1)
        self.assertEqual(none["level"], "low")
        many = [
            {"flags": ["very_long"] if index < 30 else []}
            for index in range(100)
        ]
        high = parse_structure.estimate_structure_confidence(500_000, many, 0, 0)
        self.assertEqual(high["level"], "high")
        self.assertIn("many chapters are unusually long", high["reasons"])
        sparse = parse_structure.estimate_structure_confidence(500_000, [{"flags": []}], 0, 0)
        self.assertIn("chapter count", sparse["reasons"][0])

        text = "第三章\n正文\n第二章\n正文\n第二章\n正文"
        _, report = parse_structure.parse(text)
        self.assertTrue(report["duplicate_labels"])
        self.assertTrue(report["non_monotonic_numbers"])
        self.assertIn("duplicate chapter labels detected", report["warnings"])
        large = "普通正文。" * 30_000
        chapters, fallback_report = parse_structure.parse(large)
        self.assertEqual(chapters, [])
        self.assertTrue(fallback_report["fallback_chunking"]["enabled"])

        with (
            mock.patch.object(parse_structure, "build_document_slices", return_value=[]),
            self.assertRaisesRegex(ValueError, "cover the document"),
        ):
            parse_structure.parse("正文")

    def test_main_runs_parser_and_prints_output_path(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["parse_structure.py", "book.cleanwork"]),
            mock.patch.object(parse_structure, "run") as run,
            mock.patch("builtins.print") as printer,
        ):
            parse_structure.main()
        run.assert_called_once()
        printer.assert_called_once()


class TitleAndBlockedCoverageF7Tests(unittest.TestCase):
    def test_title_helpers_cover_locator_brackets_next_line_and_optional_proposal(self) -> None:
        records = scan_titles.line_records("  第一章\n\n正文")
        self.assertEqual(records[0]["start"], 2)
        self.assertEqual(scan_titles.next_non_empty(records, 0)["text"], "正文")
        self.assertIsNone(scan_titles.next_non_empty(records, 2))
        locators = [{"start_offset": 2, "end_offset": 5, "kind": "chapter", "index": 1}]
        self.assertIsNone(scan_titles.locator_lookup(locators, 1))
        self.assertIsNone(scan_titles.locator_lookup(locators, 5))
        self.assertEqual(scan_titles.locator_lookup(locators, 3)["index"], 1)
        self.assertTrue(scan_titles.bracket_issues("第一章（开始"))
        self.assertEqual(scan_titles.bracket_issues("第一章（开始）"), [])
        candidate = scan_titles.base_candidate("TT-1", "x", "low", "m", None, [], "新标题")
        self.assertEqual(candidate["proposed"], "新标题")

    def test_title_scan_covers_anomalies_duplicates_order_pseudo_and_low_confidence_cap(self) -> None:
        text = (
            "第一章（开始\n正文\n"
            "第二章\n短标题\n正文\n"
            "第三章 结束\n正文提到第五章内容。\n"
            "第二章 再来\n正文\n第二章 再来\n正文"
        )
        candidates, summary = scan_titles.scan_text(text)
        categories = {item["category"] for item in candidates}
        self.assertIn("unbalanced_brackets", categories)
        self.assertIn("broken_title_line", categories)
        self.assertIn("duplicate_chapter_label", categories)
        self.assertIn("non_monotonic_chapter_number", categories)
        self.assertIn("possible_pseudo_title", categories)
        self.assertFalse(summary["execution_suggestions_enabled"])

        low_text = "\n".join(f"正文提到第{i + 1}章内容。" for i in range(60))
        capped, capped_summary = scan_titles.scan_text(low_text)
        pseudo = [item for item in capped if item["category"] == "possible_pseudo_title"]
        self.assertEqual(len(pseudo), scan_titles.MAX_LOW_CONFIDENCE_PSEUDO_TITLES)
        self.assertEqual(capped_summary["suppressed_report_only_count"], 10)

        fake_report = {
            "structure_confidence": {"level": "medium"},
            "locators": "invalid",
            "duplicate_labels": [],
            "non_monotonic_numbers": [],
            "fallback_chunking": {},
        }
        with mock.patch.object(scan_titles, "parse_chapters", return_value=([], fake_report)):
            candidates, _ = scan_titles.scan_text("正文提到第一章内容。")
        self.assertIsNone(candidates[0]["locator"])

    def test_blocked_helpers_cover_limits_context_overlap_and_both_match_types(self) -> None:
        for value in (0, -1, True, 10_001, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    scan_blocked.validate_max_candidates(value)  # type: ignore[arg-type]
        scan_blocked.validate_max_candidates(1)
        scan_blocked.validate_max_candidates(10_000)
        context = scan_blocked.sentence_context("前句。中＊文！后句。", 3, 6, 1)
        self.assertEqual(context["original"], "中＊文")
        candidates, summary = scan_blocked.scan_text("甲＊乙。丙·丁。戊＊＊己。", 10)
        self.assertEqual(summary["candidate_count"], 3)
        self.assertEqual(set(summary["by_type"]), {"mask_chars", "word_separator"})
        limited, limited_summary = scan_blocked.scan_text("甲＊乙。丙＊丁。", 1)
        self.assertEqual(len(limited), 1)
        self.assertTrue(limited_summary["max_candidates_reached"])

        match = mock.Mock()
        match.span.return_value = (0, 3)
        fake_pattern = mock.Mock()
        fake_pattern.finditer.return_value = [match]
        with (
            mock.patch.object(scan_blocked, "MASK_RE", fake_pattern),
            mock.patch.object(scan_blocked, "SEPARATOR_RE", fake_pattern),
        ):
            deduplicated, _ = scan_blocked.scan_text("甲＊乙", 5)
        self.assertEqual(len(deduplicated), 1)

    def test_choose_input_and_cli_mains_cover_explicit_and_auto_paths(self) -> None:
        workspace = Path("C:/book.cleanwork")
        self.assertEqual(scan_titles.choose_input(workspace, "versions/custom.txt"), "versions/custom.txt")
        self.assertEqual(scan_blocked.choose_input(workspace, "versions/custom.txt"), "versions/custom.txt")
        with mock.patch.object(scan_titles, "resolve_current_head", return_value=workspace / "versions/v1.txt"):
            self.assertEqual(scan_titles.choose_input(workspace, "auto"), "versions/v1.txt")
        with mock.patch.object(scan_blocked, "resolve_current_head", return_value=workspace / "versions/v1.txt"):
            self.assertEqual(scan_blocked.choose_input(workspace, "auto"), "versions/v1.txt")

        with (
            mock.patch.object(sys, "argv", ["scan_titles.py", "book.cleanwork"]),
            mock.patch.object(scan_titles, "run", return_value={"summary": {}}) as title_run,
            mock.patch("builtins.print"),
        ):
            scan_titles.main()
        title_run.assert_called_once()
        with (
            mock.patch.object(sys, "argv", ["scan_blocked.py", "book.cleanwork"]),
            mock.patch.object(scan_blocked, "run", return_value={"summary": {}}) as blocked_run,
            mock.patch("builtins.print"),
        ):
            scan_blocked.main()
        blocked_run.assert_called_once()


class LayoutCoverageF7Tests(unittest.TestCase):
    def test_config_validation_rejects_every_public_type_and_enum_boundary(self) -> None:
        invalid: list[object] = [[], {**layout.DEFAULT_CONFIG, "extra": True}]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    layout.validate_config(value)  # type: ignore[arg-type]

        mutations = (
            ("layout", None),
            ("conversion", None),
            ("export", None),
            ("layout.enabled", 1),
            ("layout.indent", "four"),
            ("layout.max_blank_lines", True),
            ("layout.max_blank_lines", 11),
            ("layout.punctuation_mode", "all"),
            ("conversion.mode", "auto"),
            ("export.title", 1),
            ("export.language", " "),
        )
        for dotted, value in mutations:
            config = json.loads(json.dumps(layout.DEFAULT_CONFIG))
            parts = dotted.split(".")
            if len(parts) == 1:
                config[parts[0]] = value
            else:
                config[parts[0]][parts[1]] = value
            with self.subTest(option=dotted, value=value):
                with self.assertRaises(ValueError):
                    layout.validate_config(config)

        for obsolete in (
            {"modules": {"ads": True}},
            {"conversion": {"engine": "opencc"}},
            {"export": {"txt": True}},
        ):
            config = json.loads(json.dumps(layout.DEFAULT_CONFIG))
            for section, values in obsolete.items():
                if section in config and isinstance(values, dict):
                    config[section].update(values)
                else:
                    config[section] = values
            with self.subTest(obsolete=obsolete), self.assertRaisesRegex(
                ValueError, "unsupported option"
            ):
                layout.validate_config(config)

    def test_config_loader_covers_missing_root_inheritance_cycle_and_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                layout.load_config(root / "missing.json")
            list_root = root / "list.json"
            list_root.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "root must be an object"):
                layout.load_config(list_root)
            invalid_parent = root / "invalid-parent.json"
            invalid_parent.write_text('{"inherits": 1}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inherits"):
                layout.load_config(invalid_parent)
            cycle = root / "cycle.json"
            cycle.write_text('{"inherits": "cycle.json"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cycle"):
                layout.load_config(cycle)
            parent = root / "parent.json"
            child = root / "child.json"
            parent.write_text(json.dumps(layout.DEFAULT_CONFIG), encoding="utf-8")
            child.write_text('{"inherits":"parent.json","layout":{"max_blank_lines":0}}', encoding="utf-8")
            inputs: set[Path] = set()
            loaded = layout.load_config(child, inputs)
            self.assertEqual(loaded["layout"]["max_blank_lines"], 0)
            self.assertEqual(inputs, {parent.resolve(), child.resolve()})

    def test_layout_helpers_and_conversion_cover_protected_text_and_engines(self) -> None:
        self.assertEqual(layout.normalize_punctuation("中文, 测试!"), "中文， 测试！")
        self.assertEqual(layout.normalize_punctuation("v1.2 x,y"), "v1.2 x,y")
        line = "甲   乙 https://a.example/x  y `a  b`  丙"
        normalized = layout.normalize_ascii_spaces(line)
        self.assertIn("https://a.example/x", normalized)
        self.assertIn("`a  b`", normalized)
        collapsed, removed = layout.collapse_blank_lines(["甲", "", "", "乙"], 1)
        self.assertEqual((collapsed, removed), (["甲", "", "乙"], 1))

        config = json.loads(json.dumps(layout.DEFAULT_CONFIG))
        self.assertEqual(layout.convert_script("正文", config)[0], "正文")
        config["conversion"]["mode"] = "traditional"
        fake_opencc = types.SimpleNamespace(OpenCC=lambda mode: types.SimpleNamespace(convert=lambda text: f"{mode}:{text}"))
        with mock.patch.dict(sys.modules, {"opencc": fake_opencc}):
            converted, report = layout.convert_script("正文", config)
        self.assertEqual(converted, "s2t:正文")
        self.assertEqual(report["engine"], "opencc")

        original_import = builtins.__import__

        def fail_opencc(name: str, *args: object, **kwargs: object):
            if name == "opencc":
                raise ImportError("missing")
            return original_import(name, *args, **kwargs)

        with (
            mock.patch("builtins.__import__", side_effect=fail_opencc),
            self.assertRaisesRegex(RuntimeError, "OpenCC is required"),
        ):
            layout.convert_script("正文", config)

    def test_normalize_text_covers_disabled_and_enabled_layout(self) -> None:
        disabled = json.loads(json.dumps(layout.DEFAULT_CONFIG))
        disabled["layout"]["enabled"] = False
        text, metrics = layout.normalize_text("正文", disabled)
        self.assertEqual(text, "正文")
        self.assertFalse(metrics["layout_enabled"])

        enabled = json.loads(json.dumps(layout.DEFAULT_CONFIG))
        enabled["layout"]["enabled"] = True
        enabled["layout"]["punctuation_mode"] = "safe_chinese"
        enabled["layout"]["normalize_ascii_space"] = True
        text, metrics = layout.normalize_text("第一章 开始\r\n正文,   内容   \r\n\r\n\r\n", enabled)
        self.assertEqual(metrics["blank_lines_removed"], 2)
        self.assertTrue(text.endswith("\n"))
        self.assertIn("正文， 内容", text)

    def test_layout_main_passes_optional_config_and_prints_metrics(self) -> None:
        with (
            mock.patch.object(
                sys,
                "argv",
                ["normalize_layout.py", "book.cleanwork", "--config", "config.json"],
            ),
            mock.patch.object(layout, "run", return_value={"metrics": {}}) as run,
            mock.patch("builtins.print"),
        ):
            layout.main()
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
