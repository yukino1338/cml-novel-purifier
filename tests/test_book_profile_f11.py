from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import book_profile  # noqa: E402
import export_outputs  # noqa: E402
import make_ad_decisions  # noqa: E402


class BookProfileF11Tests(unittest.TestCase):
    def test_valid_profile_is_preserved_and_shared_helpers_use_only_public_schema(
        self,
    ) -> None:
        profile = {
            "title": "书名😀",
            "author": "作者",
            "genre": "奇幻",
            "narrative_style": "第三人称",
            "main_characters": ["甲", "乙"],
            "places": ["城中"],
            "factions": ["书院"],
            "terms": ["灵契"],
            "legitimate_structures": ["系统面板"],
            "summary": "匿名摘要",
            "evidence": ["首章出现书名"],
            "rename_verified": True,
        }
        self.assertEqual(book_profile.validate_book_profile(profile), profile)
        self.assertEqual(book_profile.verified_title(profile), "书名😀")
        self.assertEqual(
            book_profile.protection_terms(profile),
            {"书名😀", "作者", "甲", "乙", "城中", "书院", "灵契", "系统面板"},
        )

    def test_unknown_legacy_nested_and_wrong_types_fail_closed(self) -> None:
        invalid = (
            {"tags": ["同人"]},
            {"rename_approved": True},
            {"rename": {"verified": True}},
            {"characters": ["甲"]},
            {"title": ["书名"]},
            {"main_characters": "甲"},
            {"main_characters": [{"name": "甲"}]},
            {"rename_verified": 1},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                book_profile.validate_book_profile(value)

    def test_duplicate_key_nan_bad_root_and_size_limits_fail_closed(self) -> None:
        cases = (
            b'{"title":"a","title":"b"}',
            b'{"title":NaN}',
            b"[]",
            b"\xff",
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                book_profile.parse_book_profile_bytes(raw)

        with self.assertRaisesRegex(ValueError, "code-point"):
            book_profile.validate_book_profile({"title": "书" * 301})
        with self.assertRaisesRegex(ValueError, "item limit"):
            book_profile.validate_book_profile({"terms": ["词"] * 501})
        with self.assertRaisesRegex(ValueError, "byte size"):
            book_profile.parse_book_profile_bytes(
                b" " * (book_profile.MAX_PROFILE_BYTES + 1)
            )

    def test_loader_distinguishes_missing_from_invalid_and_accepts_bom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "profile.json"
            self.assertEqual(book_profile.load_book_profile(path), {})

            path.write_bytes(json.dumps({"title": "书名"}).encode("utf-8-sig"))
            self.assertEqual(book_profile.load_book_profile(path), {"title": "书名"})

            directory_path = root / "profile-dir.json"
            directory_path.mkdir()
            with self.assertRaisesRegex(ValueError, "regular file"):
                book_profile.load_book_profile(directory_path)

    def test_draft_protection_and_export_naming_share_the_public_profile(self) -> None:
        profile = {
            "title": "书名😀",
            "main_characters": ["主角"],
            "terms": ["灵契"],
            "rename_verified": True,
        }
        self.assertEqual(
            make_ad_decisions.profile_terms(profile),
            ["主角", "书名😀", "灵契"],
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            profile_path = workspace / "meta/book_profile.json"
            profile_path.parent.mkdir()
            profile_path.write_text(
                json.dumps(profile, ensure_ascii=False), encoding="utf-8"
            )
            self.assertEqual(export_outputs.load_profile(workspace), profile)

            profile_path.write_text('{"rename_approved":true}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported key"):
                export_outputs.load_profile(workspace)
            with self.assertRaisesRegex(ValueError, "unsupported key"):
                make_ad_decisions.profile_terms({"rename_approved": True})


if __name__ == "__main__":
    unittest.main()
