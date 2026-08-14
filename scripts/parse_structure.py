from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from common import WorkspaceTransaction, read_utf8, resolve_workspace_paths, sha256_file, write_json


CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "兩": 2,
    "壹": 1,
    "貳": 2,
    "贰": 2,
    "叁": 3,
    "參": 3,
    "肆": 4,
    "伍": 5,
    "陸": 6,
    "陆": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

CN_NUMBER_CHARS = "0-9零〇一二两兩三四五六七八九十百千万萬壹貳贰叁參肆伍陸陆柒捌玖拾佰仟"

CHAPTER_PATTERNS = (
    re.compile(
        rf"^\s*(?:正文\s*)?(?P<label>第\s*(?P<num>[{CN_NUMBER_CHARS}]+)\s*[章节節卷集部篇回话話])"
        r"(?P<title>[\s:：、.．-]*(?P<rest>.*?))?\s*$"
    ),
    re.compile(
        rf"^\s*(?P<label>卷\s*(?P<num>[{CN_NUMBER_CHARS}]+))"
        r"(?P<title>[\s:：、.．-]*(?P<rest>.*?))?\s*$"
    ),
    re.compile(
        rf"^\s*(?P<label>序|序章|序言|序文|序幕|楔子|引子|前言|后记|後記|尾声|尾聲|终章|終章|番外(?:篇)?(?:\s*[{CN_NUMBER_CHARS}]+)?)"
        r"(?P<title>[\s:：、.．-]*(?P<rest>.*?))?\s*$"
    ),
    re.compile(
        r"^\s*(?P<label>Chapter[\s-]*(?P<num>\d+))(?P<title>[\s:：、.．-]*(?P<rest>.*?))?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?P<label>(?:Part|Volume|Book)[\s-]*(?P<num>\d+))(?P<title>[\s:：、.．-]*(?P<rest>.*?))?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?P<label>Prologue|Epilogue|Afterword|Foreword|Introduction)"
        r"(?P<title>[\s:：、.．-]*(?P<rest>.*?))?\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?P<label>(?P<num>\d{1,5})[、.．])\s*(?P<rest>.+?)\s*$"),
    re.compile(r"^\s*(?P<label>(?P<num>\d{1,4}))\s+(?P<rest>[\u4e00-\u9fffA-Za-z][^。！？!?]{0,40})\s*$"),
)
BODY_PUNCTUATION_RE = re.compile(r"[。！？!?；;]")
CATALOG_TRAP_RE = re.compile(r"(?:目录|章节目录|章节列表|最新章节|全部章节|返回目录|上一章|下一章|无弹窗|更新最快)")
FALLBACK_MIN_TEXT_LENGTH = 120_000
FALLBACK_CHUNK_SIZE = 50_000


def parse_cn_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if not value:
        return None
    total = 0
    section = 0
    number = 0
    units = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
    for char in value:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
        elif char in units:
            unit = units[char]
            section += (number or 1) * unit
            number = 0
        elif char in {"万", "萬"}:
            total += (section + number) * 10000
            section = 0
            number = 0
        else:
            return None
    return total + section + number


def match_chapter(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 80:
        return None
    if stripped.startswith(("“", "\"", "「", "『")):
        return None
    if CATALOG_TRAP_RE.search(stripped):
        return None

    for pattern in CHAPTER_PATTERNS:
        match = pattern.match(line)
        if not match:
            continue
        data = match.groupdict()
        label = data.get("label") or stripped
        rest = (data.get("rest") or "").strip()
        if rest and len(rest) > 45:
            return None
        if rest and BODY_PUNCTUATION_RE.search(rest):
            return None
        num = data.get("num")
        number = parse_cn_number(num) if num else None
        return {
            "label": re.sub(r"\s+", "", label.strip()),
            "title": stripped,
            "heading_text": stripped,
            "number": number,
            "title_tail": rest,
        }
    return None


def chapter_flags(word_count: int) -> list[str]:
    flags: list[str] = []
    if word_count < 200:
        flags.append("very_short")
    if word_count > 20000:
        flags.append("very_long")
    return flags


def line_number_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def fallback_end(text: str, target: int, minimum: int) -> int:
    if target >= len(text):
        return len(text)
    newline = text.rfind("\n", minimum, target + 1)
    if newline > minimum:
        return newline + 1
    newline = text.find("\n", target)
    if newline >= 0:
        return newline + 1
    return len(text)


def build_fallback_chunks(text: str, chunk_size: int = FALLBACK_CHUNK_SIZE) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    start = 0
    index = 1
    while start < len(text):
        minimum = min(len(text), start + max(1, chunk_size // 2))
        end = fallback_end(text, min(len(text), start + chunk_size), minimum)
        if end <= start:
            end = min(len(text), start + chunk_size)
        chunks.append(
            {
                "index": index,
                "kind": "fallback_chunk",
                "title": f"Fallback chunk {index:03d}",
                "line": line_number_for_offset(text, start),
                "start_offset": start,
                "end_offset": end,
                "word_count": len(text[start:end].strip()),
                "flags": ["fallback_locator"],
            }
        )
        start = end
        index += 1
    return chunks


def build_document_slices(
    text: str,
    chapters: list[dict[str, Any]],
    fallback_chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if fallback_chunks:
        return [
            {
                **chunk,
                "heading_end_offset": int(chunk["start_offset"]),
            }
            for chunk in fallback_chunks
        ]
    if not chapters:
        return [
            {
                "index": 1,
                "kind": "body",
                "title": "正文",
                "line": 1,
                "start_offset": 0,
                "heading_end_offset": 0,
                "end_offset": len(text),
                "word_count": len(text.strip()),
                "flags": [],
            }
        ]

    slices: list[dict[str, Any]] = []
    first_start = int(chapters[0]["start_offset"])
    if first_start:
        slices.append(
            {
                "index": 0,
                "kind": "front_matter",
                "title": "前置内容",
                "line": 1,
                "start_offset": 0,
                "heading_end_offset": 0,
                "end_offset": first_start,
                "word_count": len(text[:first_start].strip()),
                "flags": ["front_matter"],
            }
        )
    slices.extend({**chapter, "kind": "chapter"} for chapter in chapters)
    return slices


def validate_document_slices(text: str, slices: list[dict[str, Any]]) -> bool:
    if not slices:
        return False
    cursor = 0
    for item in slices:
        start = item.get("start_offset")
        heading_end = item.get("heading_end_offset")
        end = item.get("end_offset")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(heading_end, int)
            or isinstance(heading_end, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start != cursor
            or not start <= heading_end <= end <= len(text)
        ):
            return False
        cursor = end
    return cursor == len(text)


def expected_min_chapters(text_length: int) -> int:
    if text_length < FALLBACK_MIN_TEXT_LENGTH:
        return 1
    return max(3, text_length // 100_000)


def estimate_structure_confidence(
    text_length: int,
    chapters: list[dict[str, Any]],
    duplicate_count: int,
    non_monotonic_count: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    chapter_count = len(chapters)
    if chapter_count == 0:
        score = 0.05
        reasons.append("no chapter headings detected")
    else:
        expected = expected_min_chapters(text_length)
        count_ratio = min(1.0, chapter_count / max(1, expected))
        score = 0.35 + 0.45 * count_ratio
        if chapter_count >= 20:
            score += 0.12
        if chapter_count >= 100:
            score += 0.08
        if text_length >= FALLBACK_MIN_TEXT_LENGTH and chapter_count < expected:
            reasons.append(f"chapter count {chapter_count} is low for {text_length} chars")

    if duplicate_count:
        score -= min(0.2, duplicate_count / max(1, chapter_count or 1) * 0.6)
        reasons.append("duplicate chapter labels detected")
    if non_monotonic_count:
        score -= min(0.25, non_monotonic_count / max(1, chapter_count or 1) * 0.8)
        reasons.append("non-monotonic chapter numbers detected")

    very_long_count = sum("very_long" in chapter.get("flags", []) for chapter in chapters)
    if chapter_count and very_long_count / chapter_count > 0.25:
        score -= 0.15
        reasons.append("many chapters are unusually long")

    score = max(0.0, min(1.0, score))
    if score >= 0.78:
        level = "high"
    elif score >= 0.45:
        level = "medium"
    else:
        level = "low"
    return {
        "score": round(score, 4),
        "level": level,
        "reasons": reasons,
    }


def parse(text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    matches: list[dict[str, Any]] = []
    offset = 0
    for line_no, line in enumerate(lines, 1):
        matched = match_chapter(line)
        if matched:
            matched["line"] = line_no
            matched["start_offset"] = offset
            matched["heading_end_offset"] = offset + len(line)
            matches.append(matched)
        offset += len(line)

    chapters: list[dict[str, Any]] = []
    for index, item in enumerate(matches):
        start = item["start_offset"]
        end = matches[index + 1]["start_offset"] if index + 1 < len(matches) else len(text)
        body = text[start:end]
        word_count = len(body.strip())
        chapters.append(
            {
                "index": index + 1,
                "label": item["label"],
                "title": item["title"],
                "title_tail": item.get("title_tail", ""),
                "number": item.get("number"),
                "line": item["line"],
                "start_offset": start,
                "heading_end_offset": item["heading_end_offset"],
                "end_offset": end,
                "word_count": word_count,
                "flags": chapter_flags(word_count),
            }
        )

    label_counts: dict[str, int] = {}
    for chapter in chapters:
        key = str(chapter["title"])
        label_counts[key] = label_counts.get(key, 0) + 1
    duplicates = sorted(label for label, count in label_counts.items() if count > 1)

    numbered = [c for c in chapters if isinstance(c.get("number"), int)]
    non_monotonic: list[dict[str, Any]] = []
    previous: int | None = None
    for chapter in numbered:
        number = chapter["number"]
        if previous is not None and number < previous:
            non_monotonic.append(
                {
                    "index": chapter["index"],
                    "label": chapter["label"],
                    "number": number,
                    "previous_number": previous,
                }
            )
        previous = number

    report = {
        "chapter_count": len(chapters),
        "duplicate_labels": duplicates,
        "non_monotonic_numbers": non_monotonic,
        "very_short_count": sum("very_short" in c["flags"] for c in chapters),
        "very_long_count": sum("very_long" in c["flags"] for c in chapters),
        "fallback_chunking": {
            "enabled": False,
            "chunk_count": 0,
            "reason": "",
        },
        "warnings": [],
    }
    confidence = estimate_structure_confidence(len(text), chapters, len(duplicates), len(non_monotonic))
    report["structure_confidence"] = confidence
    if not chapters:
        report["warnings"].append("no chapter headings detected")
    if duplicates:
        report["warnings"].append("duplicate chapter labels detected")
    if non_monotonic:
        report["warnings"].append("non-monotonic chapter numbers detected")
    if confidence["level"] == "low":
        report["warnings"].append("low structure confidence")
    fallback_chunks: list[dict[str, Any]] = []
    if confidence["level"] == "low" and len(text) >= FALLBACK_MIN_TEXT_LENGTH:
        fallback_chunks = build_fallback_chunks(text)
        report["fallback_chunking"] = {
            "enabled": True,
            "chunk_count": len(fallback_chunks),
            "reason": "low structure confidence on a large text; chunks are locators, not real chapters",
        }
    report["fallback_chunks"] = fallback_chunks
    slices = build_document_slices(text, chapters, fallback_chunks)
    if not validate_document_slices(text, slices):
        raise ValueError("chapter slices do not cover the document exactly")
    report["slices"] = slices
    report["locators"] = slices
    return chapters, report


def build_structure_artifact(
    text: str,
    input_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a complete structure artifact bound to the selected input file."""

    chapters, report = parse(text)
    return (
        {
            "schema_version": 2,
            "input_sha256": sha256_file(input_path),
            "chapters": chapters,
            "structure_confidence": report["structure_confidence"],
            "fallback_chunking": report["fallback_chunking"],
            "fallback_chunks": report["fallback_chunks"],
            "slices": report["slices"],
            "locators": report["locators"],
        },
        report,
    )


def run(workspace: Path) -> None:
    workspace, read_paths, write_paths = resolve_workspace_paths(
        workspace,
        reads={"input": "versions/v1_preprocessed.txt"},
        writes={
            "chapters": "meta/chapters.json",
            "report": "report/structure_report.json",
        },
    )
    input_path = read_paths["input"]
    text = read_utf8(input_path)
    structure, report = build_structure_artifact(text, input_path)
    report_summary = {
        key: value
        for key, value in report.items()
        if key not in {"fallback_chunks", "slices", "locators"}
    }
    with WorkspaceTransaction(workspace) as transaction:
        write_json(
            transaction.stage_path(write_paths["chapters"]),
            structure,
        )
        write_json(transaction.stage_path(write_paths["report"]), report_summary)
        transaction.commit(
            {
                "1_parse_structure": (
                    "done",
                    {
                        "input": "versions/v1_preprocessed.txt",
                        "output": "meta/chapters.json",
                        "report": "report/structure_report.json",
                    },
                )
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse novel chapter structure.")
    parser.add_argument("workspace", help="Path to the .cleanwork directory.")
    args = parser.parse_args()
    run(Path(args.workspace).resolve())
    print(f"chapters: {Path(args.workspace).resolve() / 'meta' / 'chapters.json'}")


if __name__ == "__main__":
    main()
