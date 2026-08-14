from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import normalize_layout  # noqa: E402
import preprocess  # noqa: E402
import apply_decisions  # noqa: E402
from tests.support_formal_ads import formalize_ads  # noqa: E402


class LayoutSafetyF4Tests(unittest.TestCase):
    def test_default_layout_profile_preserves_every_input_character(self) -> None:
        text = "  第一章 起点  \n\n\n正文,   内容   "
        config = normalize_layout.load_config(None)

        output, metrics = normalize_layout.normalize_text(text, config)

        self.assertFalse(config["layout"]["enabled"])
        self.assertEqual(output, text)
        self.assertEqual(metrics["layout_profile"], "preserve")

    def test_default_cleaning_path_changes_only_preprocess_and_formal_delete_spans(
        self,
    ) -> None:
        source_text = "第一章 起点  \r\n\r\n正文甲。  \r\n广告甲\r\n正文乙。  "
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "preserve.txt"
            source.write_bytes(source_text.encode("utf-8"))
            workspace = preprocess.run(source)
            v1 = workspace / "versions/v1_preprocessed.txt"
            preprocessed = v1.read_text(encoding="utf-8")
            original = "广告甲\n"
            formalize_ads(
                workspace,
                [{"offset": preprocessed.index(original), "original": original}],
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
            applied = (workspace / "versions/v2_ads_removed.txt").read_bytes()

            normalize_layout.run(
                workspace,
                "versions/v2_ads_removed.txt",
                "versions/v5_layout_final.txt",
                None,
            )

            self.assertEqual(
                (workspace / "versions/v5_layout_final.txt").read_bytes(),
                applied,
            )

    def test_safe_punctuation_preserves_semantic_ascii_tokens_and_is_idempotent(self) -> None:
        text = (
            "第一章 起点\n"
            "价格3.14,版本v1.2.3,时间12:30。\n"
            "邮箱name@example.com,网址https://example.com/a?x=1.2!\n"
            "路径C:\\books\\v1.2\\a.txt,代码`call(x, y);`。\n"
            "他说,你好!真的可以?可以;继续:出发.\n"
        )
        config = normalize_layout.validate_config(
            {
                **normalize_layout.DEFAULT_CONFIG,
                "layout": {
                    **normalize_layout.DEFAULT_CONFIG["layout"],
                    "enabled": True,
                    "punctuation_mode": "safe_chinese",
                },
            }
        )

        once, _ = normalize_layout.normalize_text(text, config)
        twice, _ = normalize_layout.normalize_text(once, config)

        for token in (
            "3.14",
            "v1.2.3",
            "12:30",
            "name@example.com",
            "https://example.com/a?x=1.2",
            "C:\\books\\v1.2\\a.txt",
            "`call(x, y);`",
        ):
            self.assertIn(token, once)
        self.assertIn("他说，你好！真的可以？可以；继续：出发。", once)
        self.assertEqual(twice, once)

    def test_invalid_or_removed_public_options_fail_before_any_workspace_write(self) -> None:
        invalid_layouts = (
            {"max_blank_lines": -1},
            {"max_blank_lines": True},
            {"indent": "guess"},
            {"punctuation_mode": "all"},
            {"smart_line_merge": True},
            {"fullwidth_punctuation": True},
            {"author_note_strategy": "move_to_end"},
            {"unknown_option": 1},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, override in enumerate(invalid_layouts):
                with self.subTest(override=override):
                    case_root = root / str(index)
                    case_root.mkdir()
                    source = case_root / "sample.txt"
                    source.write_text("第一章 起点\n正文甲。\n", encoding="utf-8")
                    workspace = preprocess.run(source)
                    output = workspace / "versions/v5_layout_final.txt"
                    output.write_text("old output\n", encoding="utf-8")
                    config = case_root / "invalid.json"
                    config.write_text(
                        json.dumps({"layout": override}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    manifest_before = (workspace / "manifest.json").read_bytes()
                    output_before = output.read_bytes()

                    with self.assertRaisesRegex(ValueError, "config|layout|option|punctuation|author"):
                        normalize_layout.run(
                            workspace,
                            "auto",
                            "versions/v5_layout_final.txt",
                            config,
                        )

                    self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
                    self.assertEqual(output.read_bytes(), output_before)

    def test_only_one_nonredundant_public_config_template_remains(self) -> None:
        templates = sorted((ROOT / "assets/config-templates").glob("*.json"))

        self.assertEqual([path.name for path in templates], ["default.json"])
        config = normalize_layout.load_config(templates[0])
        self.assertFalse(config["layout"]["enabled"])
        self.assertEqual(set(config), {"layout", "conversion", "export"})
        self.assertEqual(set(config["conversion"]), {"mode"})
        self.assertEqual(set(config["export"]), {"title", "author", "language"})
        self.assertNotIn("smart_line_merge", config["layout"])
        self.assertNotIn("fullwidth_punctuation", config["layout"])
        self.assertNotIn("author_note_strategy", config["layout"])
        self.assertNotIn("fallback", config["conversion"])


if __name__ == "__main__":
    unittest.main()
