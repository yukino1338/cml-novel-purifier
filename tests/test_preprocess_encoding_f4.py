from __future__ import annotations

import json
import hashlib
import random
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import preprocess  # noqa: E402
import apply_decisions  # noqa: E402
from tests.support_formal_ads import formalize_ads  # noqa: E402


SAMPLE_SIMPLIFIED = (
    "匿名编码样本\n第一章 起点\n灯光照在空白纸页上，角色甲安静地翻开书本。\n"
)
SAMPLE_TRADITIONAL = (
    "匿名編碼樣本\n第一章 起點\n燈光照在空白紙頁上，角色甲安靜地翻開書本。\n"
)


class PreprocessEncodingF4Tests(unittest.TestCase):
    def test_public_input_contract_lists_the_executable_encoding_set(self) -> None:
        contract = (ROOT / "references/text-input-contract.md").read_text(
            encoding="utf-8"
        ).lower()
        for encoding in preprocess.EXPLICIT_ENCODINGS:
            with self.subTest(encoding=encoding):
                self.assertIn(encoding.lower(), contract)

    def run_case(
        self,
        root: Path,
        name: str,
        raw: bytes,
        *,
        explicit_encoding: str | None = None,
    ) -> tuple[Path, dict[str, object]]:
        source = root / name
        source.write_bytes(raw)
        workspace = preprocess.run(source, encoding=explicit_encoding)
        report = json.loads(
            (workspace / "report/preprocess_report.json").read_text(encoding="utf-8")
        )
        return workspace, report

    def assert_done(
        self,
        workspace: Path,
        report: dict[str, object],
        expected_text: str,
        expected_encoding: str,
    ) -> None:
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        detection = report["encoding_detection"]
        self.assertEqual(manifest["stages"]["0_preprocess"]["status"], "done")
        self.assertEqual(manifest["current_head"], "versions/v1_preprocessed.txt")
        self.assertFalse(detection["blocked"])
        self.assertEqual(detection["selected_encoding"], expected_encoding)
        self.assertEqual(
            (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8"),
            expected_text,
        )

    def assert_blocked(self, workspace: Path, report: dict[str, object]) -> None:
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["stages"]["0_preprocess"]["status"], "blocked")
        self.assertEqual(manifest["current_head"], "versions/v0_original.txt")
        self.assertTrue(report["encoding_detection"]["blocked"])
        self.assertFalse((workspace / "versions/v1_preprocessed.txt").exists())

    def test_auto_detection_round_trips_supported_encodings_and_english(self) -> None:
        cases = (
            (
                "utf8.wrong",
                SAMPLE_SIMPLIFIED.encode("utf-8"),
                SAMPLE_SIMPLIFIED,
                "utf-8",
            ),
            (
                "utf8-bom.data",
                SAMPLE_SIMPLIFIED.encode("utf-8-sig"),
                SAMPLE_SIMPLIFIED,
                "utf-8-sig",
            ),
            (
                "gb18030.bin",
                SAMPLE_SIMPLIFIED.encode("gb18030"),
                SAMPLE_SIMPLIFIED,
                "gb18030",
            ),
            (
                "big5.unknown",
                SAMPLE_TRADITIONAL.encode("big5"),
                SAMPLE_TRADITIONAL,
                "big5",
            ),
            (
                "utf16-le.payload",
                b"\xff\xfe" + SAMPLE_SIMPLIFIED.encode("utf-16-le"),
                SAMPLE_SIMPLIFIED,
                "utf-16-le",
            ),
            (
                "utf16-be.payload",
                b"\xfe\xff" + SAMPLE_TRADITIONAL.encode("utf-16-be"),
                SAMPLE_TRADITIONAL,
                "utf-16-be",
            ),
            (
                "english.txt",
                b"Anonymous sample\nChapter 1\nA quiet room.\n",
                "Anonymous sample\nChapter 1\nA quiet room.\n",
                "utf-8",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (name, raw, text, encoding) in enumerate(cases):
                with self.subTest(encoding=encoding):
                    case_root = root / str(index)
                    case_root.mkdir()
                    workspace, report = self.run_case(case_root, name, raw)
                    self.assert_done(workspace, report, text, encoding)
                    reason = report["encoding_detection"]["selection_reason"]
                    self.assertIn(reason, {"bom", "strict_utf8", "quality_score"})

    def test_explicit_encoding_resolves_short_ambiguous_input_and_is_recorded(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, report = self.run_case(
                Path(directory),
                "short.data",
                bytes.fromhex("a440"),
                explicit_encoding="big5",
            )
            self.assert_done(workspace, report, "一", "big5")
            detection = report["encoding_detection"]
            self.assertEqual(detection["mode"], "explicit")
            self.assertEqual(detection["requested_encoding"], "big5")
            self.assertEqual(detection["selection_reason"], "explicit_override")

    def test_bom_has_priority_and_conflicting_explicit_encoding_is_blocked(
        self,
    ) -> None:
        raw = SAMPLE_SIMPLIFIED.encode("utf-8-sig")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auto_workspace, auto_report = self.run_case(root, "auto.bin", raw)
            self.assert_done(
                auto_workspace, auto_report, SAMPLE_SIMPLIFIED, "utf-8-sig"
            )

            conflict_root = root / "conflict"
            conflict_root.mkdir()
            workspace, report = self.run_case(
                conflict_root,
                "conflict.bin",
                raw,
                explicit_encoding="gb18030",
            )
            self.assert_blocked(workspace, report)
            self.assertEqual(
                report["encoding_detection"]["blocked_reason"],
                "explicit_encoding_conflicts_with_bom",
            )

    def test_ambiguous_truncated_mixed_and_bad_samples_fail_closed(self) -> None:
        rng = random.Random(20260715)
        mixed = "第一章\n正文甲。\n".encode("utf-8") + "第二章\n正文乙。\n".encode(
            "gb18030"
        )
        cases = (
            ("ambiguous.bin", bytes.fromhex("a440")),
            ("truncated.bin", bytes.fromhex("813081")),
            ("invalid.bin", bytes.fromhex("ffff80")),
            ("mixed.bin", mixed),
            ("controls.bin", b"Chapter 1\x00\x01\x02body\n"),
            ("replacement.txt", "正文�损坏\n".encode("utf-8")),
            ("random.bin", bytes(rng.randrange(0, 256) for _ in range(257))),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (name, raw) in enumerate(cases):
                with self.subTest(name=name):
                    case_root = root / str(index)
                    case_root.mkdir()
                    workspace, report = self.run_case(case_root, name, raw)
                    self.assert_blocked(workspace, report)
                    self.assertIn(
                        report["encoding_detection"]["blocked_reason"],
                        {
                            "ambiguous_strict_decoding",
                            "disallowed_control_character",
                            "no_strict_decoder",
                            "low_text_quality",
                            "replacement_character",
                        },
                    )

    def test_report_records_quantified_scores_and_selection_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, report = self.run_case(
                Path(directory),
                "sample.txt",
                SAMPLE_SIMPLIFIED.encode("gb18030"),
            )
            self.assertTrue(workspace.is_dir())
            detection = report["encoding_detection"]
            self.assertEqual(detection["selected_encoding"], "gb18030")
            self.assertEqual(detection["selection_reason"], "quality_score")
            self.assertIsInstance(detection["confidence"], float)
            self.assertTrue(detection["candidates"])
            for candidate in detection["candidates"]:
                self.assertIn("encoding", candidate)
                self.assertIn("strict_decode", candidate)
                self.assertIn("score", candidate)
                self.assertIn("metrics", candidate)
                if candidate["strict_decode"]:
                    metrics = candidate["metrics"]
                    self.assertIn("replacement_char_count", metrics)
                    self.assertIn("control_char_count", metrics)
                    self.assertIn("abnormal_punctuation_count", metrics)
                    self.assertIn("cjk_char_ratio", metrics)

    def test_report_binds_immutable_source_bytes_and_utf8_working_text(self) -> None:
        raw = SAMPLE_TRADITIONAL.encode("big5")
        expected = SAMPLE_TRADITIONAL.encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            workspace, report = self.run_case(Path(directory), "identity.bin", raw)

            self.assert_done(
                workspace,
                report,
                SAMPLE_TRADITIONAL,
                "big5",
            )
            self.assertEqual(
                report["source_identity"],
                {
                    "path": "versions/v0_original.txt",
                    "size_bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                },
            )
            self.assertEqual(
                report["working_text_identity"],
                {
                    "path": "versions/v1_preprocessed.txt",
                    "encoding": "utf-8",
                    "bom": False,
                    "size_bytes": len(expected),
                    "sha256": hashlib.sha256(expected).hexdigest(),
                },
            )

    def test_normalization_counts_line_endings_and_preserves_final_newline_state(
        self,
    ) -> None:
        original = "甲\r\n乙\r丙\u2028丁\u2029戊\u200b\ue123😀"
        normalized, metrics = preprocess.normalize_text(original)

        self.assertEqual(normalized, "甲\n乙\n丙\n丁\n戊\ue123😀")
        self.assertEqual(metrics["crlf_count"], 1)
        self.assertEqual(metrics["cr_count"], 1)
        self.assertEqual(metrics["unicode_line_separator_count"], 1)
        self.assertEqual(metrics["unicode_paragraph_separator_count"], 1)
        self.assertEqual(metrics["zero_width_removed"], 1)
        self.assertEqual(metrics["private_use_char_count"], 1)
        self.assertFalse(metrics["source_had_final_line_terminator"])
        self.assertFalse(metrics["output_has_final_lf"])

        ended, ended_metrics = preprocess.normalize_text("甲\r\n")
        self.assertEqual(ended, "甲\n")
        self.assertTrue(ended_metrics["source_had_final_line_terminator"])
        self.assertTrue(ended_metrics["output_has_final_lf"])

    def test_private_use_emoji_traditional_and_mixed_language_are_preserved(self) -> None:
        text = "第一章\n繁體與简体 English 123 😀 \ue123\n"
        with tempfile.TemporaryDirectory() as directory:
            workspace, report = self.run_case(
                Path(directory),
                "mixed.txt",
                text.encode("utf-8"),
            )
            self.assert_done(workspace, report, text, "utf-8")
            self.assertEqual(report["metrics"]["private_use_char_count"], 1)
            self.assertIn("private_use_characters_preserved", report["warnings"])

    def test_disallowed_control_character_has_stable_blocker_and_no_working_copy(
        self,
    ) -> None:
        raw = "第一章\n正文\x01仍在原文\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            workspace, report = self.run_case(
                Path(directory),
                "control.txt",
                raw,
                explicit_encoding="utf-8",
            )
            self.assert_blocked(workspace, report)
            self.assertEqual(
                report["encoding_detection"]["blocked_reason"],
                "disallowed_control_character",
            )
            self.assertEqual(
                (workspace / "versions/v0_original.txt").read_bytes(),
                raw,
            )
            self.assertEqual(
                report["source_identity"]["sha256"],
                hashlib.sha256(raw).hexdigest(),
            )

    def test_repeated_run_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "repeat.bin"
            source.write_bytes(SAMPLE_TRADITIONAL.encode("big5"))
            workspace = preprocess.run(source)
            before_text = (workspace / "versions/v1_preprocessed.txt").read_bytes()
            before_report = (workspace / "report/preprocess_report.json").read_bytes()

            self.assertEqual(preprocess.run(source), workspace)
            self.assertEqual(
                (workspace / "versions/v1_preprocessed.txt").read_bytes(), before_text
            )
            self.assertEqual(
                (workspace / "report/preprocess_report.json").read_bytes(),
                before_report,
            )

    def test_unchanged_run_after_apply_is_a_true_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "applied.txt"
            ad = "ADVERTISEMENT"
            source.write_text(
                ("body text " * 30) + "\n" + ad + "\n" + ("body text " * 30),
                encoding="utf-8",
            )
            workspace = preprocess.run(source, encoding="utf-8")
            input_path = workspace / "versions/v1_preprocessed.txt"
            formalize_ads(
                workspace,
                [
                    {
                        "original": ad,
                        "offset": input_path.read_text(encoding="utf-8").index(ad),
                    }
                ],
                verdict="delete",
                action="delete",
            )
            apply_decisions.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )
            manifest_path = workspace / "manifest.json"
            output_path = workspace / "versions/v2_ads_removed.txt"
            before_manifest = manifest_path.read_bytes()
            before = json.loads(before_manifest.decode("utf-8"))
            before_output = output_path.read_bytes()

            self.assertEqual(preprocess.run(source, encoding="utf-8"), workspace)

            after_manifest = manifest_path.read_bytes()
            after = json.loads(after_manifest.decode("utf-8"))
            self.assertEqual(after_manifest, before_manifest)
            self.assertEqual(after["current_head"], before["current_head"])
            self.assertEqual(after["stages"], before["stages"])
            self.assertEqual(output_path.read_bytes(), before_output)

            preprocess.run(source, encoding="ascii")
            changed = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(changed["current_head"], "versions/v1_preprocessed.txt")
            self.assertEqual(changed["stages"]["2_ads"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
