from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import parse_structure  # noqa: E402


class ChapterModelF4Tests(unittest.TestCase):
    def assert_exact_cover(self, text: str, slices: list[dict]) -> None:
        self.assertTrue(parse_structure.validate_document_slices(text, slices))
        self.assertEqual("".join(text[item["start_offset"] : item["end_offset"]] for item in slices), text)
        self.assertEqual(slices[0]["start_offset"], 0)
        self.assertEqual(slices[-1]["end_offset"], len(text))

    def test_front_matter_real_chapters_extra_and_body_pseudo_title_have_exact_offsets(self) -> None:
        text = (
            "匿名说明甲\n匿名说明乙\n\n"
            "第一章 起点\n正文甲。\n第一章 起点\n"
            "第3章。她说这只是正文。\n"
            "番外 一封信\n正文乙。\n"
        )

        chapters, report = parse_structure.parse(text)
        slices = report["slices"]

        self.assertEqual(
            [item["kind"] for item in slices],
            ["front_matter", "chapter", "chapter", "chapter"],
        )
        self.assertEqual(
            [item["title"] for item in chapters],
            ["第一章 起点", "第一章 起点", "番外 一封信"],
        )
        self.assertEqual(text[: slices[0]["end_offset"]], "匿名说明甲\n匿名说明乙\n\n")
        first = slices[1]
        self.assertEqual(
            text[first["start_offset"] : first["heading_end_offset"]],
            "第一章 起点\n",
        )
        repeated = slices[2]
        self.assertEqual(
            text[repeated["start_offset"] : repeated["heading_end_offset"]],
            "第一章 起点\n",
        )
        self.assert_exact_cover(text, slices)

    def test_no_chapter_document_is_one_body_slice(self) -> None:
        text = "匿名说明\n这是一篇没有章节标题的短正文。\n"

        chapters, report = parse_structure.parse(text)

        self.assertEqual(chapters, [])
        self.assertEqual(report["slices"], [{
            "index": 1,
            "kind": "body",
            "title": "正文",
            "line": 1,
            "start_offset": 0,
            "heading_end_offset": 0,
            "end_offset": len(text),
            "word_count": len(text.strip()),
            "flags": [],
        }])
        self.assert_exact_cover(text, report["slices"])

    def test_low_confidence_fallback_chunks_cover_zero_to_eof_without_becoming_chapters(self) -> None:
        text = ("没有章节结构的匿名正文。" * 12_000) + "\n"

        chapters, report = parse_structure.parse(text)
        slices = report["slices"]

        self.assertEqual(chapters, [])
        self.assertTrue(report["fallback_chunking"]["enabled"])
        self.assertTrue(all(item["kind"] == "fallback_chunk" for item in slices))
        self.assertTrue(all("fallback_locator" in item["flags"] for item in slices))
        self.assert_exact_cover(text, slices)


if __name__ == "__main__":
    unittest.main()
