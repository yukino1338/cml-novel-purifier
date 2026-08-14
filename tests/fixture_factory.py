from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Any


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
PROVENANCE = "synthetic-original-for-this-project"
SCHEMA_VERSION = 1
DEFAULT_LARGE_SEED = 20260713

MUST_SUPPRESS_VARIANTS: dict[int, tuple[str, ...]] = {
    0: (
        "人物甲观察装置运转并完成精神扫描，随后把数据记在纸页上。",
        "人物甲让齿轮回转自己原来的方向，然后整理散落零件。",
        "人物甲抬手打断同伴的话，转身检查扫描仪的刻度。",
        "圆盘运转自如，人物甲只记录机械结构的变化。",
        "人物甲整理衣袖并校对手写清单，没有处理外部文本。",
        "设备完成内部扫描，人物甲把结果录入值班表。",
    ),
    1: (
        "人物甲讨论匿名作者的排版习惯，并画出网站页面草图。",
        "课堂分析网站设计，讲义同时解释作者署名格式。",
        "人物甲说这张网站草图出自匿名作者，随后合上纸页。",
        "展板介绍作者与网站历史，但现场没有任何访问地址。",
        "人物甲核对作者字段和网站栏目，只是在整理本地档案。",
        "对话提到作者、读者和网站三个词，却没有推广含义。",
    ),
    2: (
        "终端正在下载场景数据，人物甲等待本地进度结束。",
        "设备显示下载完成，随后自动更新室内温度记录。",
        "人物甲暂停模型下载，把离线终端放回工具箱。",
        "面板上的下载条属于机器状态，不是文件获取邀请。",
        "本地更新完成后，人物甲关闭没有联网的测试设备。",
        "人物甲查看下载速度，只为判断设备是否发生故障。",
    ),
    3: (
        "工坊每天更新值班记录，人物甲负责整理和校对纸页。",
        "档案员录入旧清单，另一人手打缺失的编号。",
        "人物甲扫描桌面的纸页，再整理自己的观察笔记。",
        "校对员检查本地表格，录入员同步修正两个数字。",
        "人物甲转身整理工具，并把手写记录放进抽屉。",
        "扫描设备停止后，人物甲完成整理并更新值班表。",
    ),
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(path: Path, data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _write_bytes(path, payload.encode("utf-8"))


def _candidate_case(
    case_id: str,
    text: str,
    classification: str,
    expected_action: str,
    expected_chapter: str,
    evidence_family: str,
    surface_expectation: str,
    evidence_tags: list[str],
) -> dict[str, Any]:
    expected_output = "" if expected_action == "delete" else text
    return {
        "case_id": case_id,
        "classification": classification,
        "text": text,
        "span": {"start": 0, "end": len(text)},
        "allowed_actions": [expected_action],
        "expected_action": expected_action,
        "expected_chapter": expected_chapter,
        "evidence_family": evidence_family,
        "surface_expectation": surface_expectation,
        "evidence_tags": evidence_tags,
        "input_sha256": sha256_text(text),
        "expected_output_sha256": sha256_text(expected_output),
        "provenance": PROVENANCE,
    }


def make_ad_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for index in range(1, 49):
        group = (index - 1) // 6
        local = (index - 1) % 6 + 1
        chapter = f"chapter-{(index - 1) % 4 + 1}"
        if group == 0:
            text = (
                f"站外更新提示 {local:02d}：请访问 "
                f"https://reader.example.com/update/{local:02d} 获取后续内容。"
            )
            family = "explicit-url-promotion"
            tags = ["url", "visit_intent", "standalone"]
        elif group == 1:
            text = (
                f"站外下载提示 {local:02d}：请联系 "
                f"notice{local:02d}@example.com 获取文件。"
            )
            family = "email-download-promotion"
            tags = ["email", "download_intent", "standalone"]
        elif group == 2:
            text = (
                f"站外交流提示 {local:02d}：请加入QQ群："
                f"TEST{local:04d} 获取更新通知。"
            )
            family = "contact-promotion"
            tags = ["contact", "promotion_intent", "standalone"]
        elif group == 3:
            text = (
                f"TXT下载地址 {local:02d}：download.example.net/file/{local:02d}，"
                "仅供站外获取文件。"
            )
            family = "download-domain-promotion"
            tags = ["download", "domain", "standalone"]
        elif group == 4:
            text = (
                f"来源水印 {local:02d}：本书由匿名整理组校对，仅供学习交流；"
                f"请访问 reader.example.com/source/{local:02d}。"
            )
            family = "source-watermark"
            tags = ["watermark", "copy_marker", "domain"]
        elif group == 5:
            text = (
                f"阅读站提示 {local:02d}：更新最快、无弹窗；"
                f"reader.example点com/read/{local:02d}。"
            )
            family = "reader-site-obfuscated-domain"
            tags = ["reader_site", "obfuscated_domain", "promotion_intent"]
        elif group == 6:
            text = (
                f"作者的话：站外更新 {local:02d} 请访问 "
                f"https://reader.example.com/note/{local:02d}。"
            )
            family = "author-note-external-promotion"
            tags = ["author_note", "url", "visit_intent"]
        else:
            text = (
                f"相邻站外提示 {local:02d}：请访问以下地址\n"
                f"reader.example点com/multi/{local:02d}"
            )
            family = "multiline-adjacent-domain"
            tags = ["multiline", "neighbor_evidence", "obfuscated_domain"]
        cases.append(
            _candidate_case(
                f"ad-positive-{index:03d}",
                text,
                "explicit_ad",
                "delete",
                chapter,
                family,
                "must_surface",
                tags,
            )
        )

    for index in range(1, 49):
        group = (index - 1) // 6
        local = (index - 1) % 6 + 1
        chapter = f"chapter-{(index - 1) % 4 + 1}"
        if group == 0:
            text = f"正文负例 {local:02d}：{MUST_SUPPRESS_VARIANTS[group][local - 1]}"
            family = "narrative-scan"
            expectation = "must_suppress"
            tags = ["narrative", "scan_word"]
        elif group == 1:
            text = f"正文负例 {local:02d}：{MUST_SUPPRESS_VARIANTS[group][local - 1]}"
            family = "narrative-author-site"
            expectation = "must_suppress"
            tags = ["narrative", "author_word", "site_word"]
        elif group == 2:
            text = f"正文负例 {local:02d}：{MUST_SUPPRESS_VARIANTS[group][local - 1]}"
            family = "narrative-download"
            expectation = "must_suppress"
            tags = ["narrative", "device_download"]
        elif group == 3:
            text = f"正文负例 {local:02d}：{MUST_SUPPRESS_VARIANTS[group][local - 1]}"
            family = "narrative-copy-work"
            expectation = "must_suppress"
            tags = ["narrative", "copy_marker_words"]
        elif group == 4:
            text = (
                f"正文负例 {local:02d}：人物甲把“"
                f"https://reader.example.com/clue/{local:02d}”写进调查记录，"
                "明确说明它只是场景线索。"
            )
            family = "narrative-url-clue"
            expectation = "may_surface"
            tags = ["narrative", "quoted_url", "protected_context"]
        elif group == 5:
            text = (
                f"正文负例 {local:02d}：纸页上的 "
                f"clue{local:02d}@example.com 是剧情中的旧邮箱，"
                "人物甲没有发出联系请求。"
            )
            family = "narrative-email-clue"
            expectation = "may_surface"
            tags = ["narrative", "email", "protected_context"]
        elif group == 6:
            text = (
                f"正文负例 {local:02d}：面板显示“QQ群：LOG{local:04d}”"
                "是旧日志里的字符串，并非邀请。"
            )
            family = "narrative-contact-log"
            expectation = "may_surface"
            tags = ["narrative", "contact", "negated_intent"]
        else:
            if local <= 3:
                text = (
                    f"正文负例 {local:02d}：作者的话只解释本章结构，"
                    "没有收藏、推荐、打赏或站外访问请求。"
                )
                family = "legitimate-author-note"
                tags = ["author_note", "no_promotion"]
            else:
                text = (
                    f"正文负例 {local:02d}：告示写着“请访问 "
                    f"https://reader.example.com/evidence/{local:02d}”，"
                    "人物甲随即将它封存为场景证物。"
                )
                family = "quoted-ad-evidence"
                tags = ["narrative", "quoted_ad", "protected_context"]
            expectation = "may_surface"
        cases.append(
            _candidate_case(
                f"ad-negative-{index:03d}",
                text,
                "narrative",
                "keep",
                chapter,
                family,
                expectation,
                tags,
            )
        )
    return cases


def _document_record(
    document_id: str,
    path: str,
    text: str,
    expected_output: str,
    expected_chapters: list[dict[str, Any]],
    candidates: list[dict[str, Any]] | None = None,
    expected_output_path: str | None = None,
    front_matter_span: dict[str, int] | None = None,
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    if front_matter_span:
        segments.append(
            {
                "kind": "front_matter",
                "start": front_matter_span["start"],
                "end": front_matter_span["end"],
                "text_sha256": sha256_text(
                    text[front_matter_span["start"] : front_matter_span["end"]]
                ),
            }
        )
    for chapter in expected_chapters:
        segments.append(
            {
                "kind": "chapter",
                "chapter_id": chapter["chapter_id"],
                "title": chapter["title"],
                "start": chapter["start"],
                "end": chapter["end"],
                "text_sha256": sha256_text(text[chapter["start"] : chapter["end"]]),
            }
        )
    if not segments:
        segments.append(
            {
                "kind": "unstructured",
                "start": 0,
                "end": len(text),
                "text_sha256": sha256_text(text),
            }
        )
    record: dict[str, Any] = {
        "document_id": document_id,
        "path": path,
        "input_sha256": sha256_text(text),
        "expected_output_sha256": sha256_text(expected_output),
        "expected_chapter_count": len(expected_chapters),
        "expected_chapters": expected_chapters,
        "expected_segments": segments,
        "candidates": candidates or [],
        "provenance": PROVENANCE,
    }
    if expected_output_path:
        record["expected_output_path"] = expected_output_path
    if front_matter_span:
        record["front_matter_span"] = front_matter_span
    return record


def _section_document(
    document_id: str,
    path: str,
    front_matter: str,
    sections: list[tuple[str, str, list[str]]],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    chunks: list[str] = [front_matter]
    cursor = len(front_matter)
    chapters: list[dict[str, Any]] = []
    for chapter_id, title, body_lines in sections:
        start = cursor
        section_text = title + "\n" + "\n".join(body_lines) + "\n\n"
        chunks.append(section_text)
        cursor += len(section_text)
        chapters.append(
            {
                "chapter_id": chapter_id,
                "title": title,
                "start": start,
                "end": cursor,
            }
        )
    text = "".join(chunks)
    front_span = {"start": 0, "end": len(front_matter)} if front_matter else None
    record = _document_record(
        document_id,
        path,
        text,
        text,
        chapters,
        front_matter_span=front_span,
    )
    return record, {path: text.encode("utf-8")}


def _compose_ads_document(
    cases: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    path = "texts/ads_mixed.txt"
    expected_path = "expected/ads_mixed.cleaned.txt"
    front_matter = "匿名合成文本\n用途：广告正负例金标。\n\n"
    input_chunks: list[str] = [front_matter]
    clean_chunks: list[str] = [front_matter]
    cursor = len(front_matter)
    annotations: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []

    for chapter_index in range(1, 5):
        chapter_id = f"chapter-{chapter_index}"
        title = f"第{chapter_index}章"
        chapter_start = cursor
        heading = title + "\n"
        input_chunks.append(heading)
        clean_chunks.append(heading)
        cursor += len(heading)

        positives = [
            case
            for case in cases
            if case["classification"] == "explicit_ad"
            and case["expected_chapter"] == chapter_id
        ]
        negatives = [
            case
            for case in cases
            if case["classification"] == "narrative"
            and case["expected_chapter"] == chapter_id
        ]
        if len(positives) != len(negatives):
            raise ValueError(f"{chapter_id} has unbalanced gold cases")
        for positive, negative in zip(positives, negatives):
            for case in (positive, negative):
                start = cursor
                input_chunks.append(case["text"])
                cursor += len(case["text"])
                end = cursor
                input_chunks.append("\n")
                cursor += 1
                operation_end = cursor
                if case["expected_action"] == "keep":
                    clean_chunks.append(case["text"] + "\n")
                annotations.append(
                    {
                        "candidate_id": case["case_id"],
                        "classification": case["classification"],
                        "surface_expectation": case["surface_expectation"],
                        "evidence_family": case["evidence_family"],
                        "evidence_tags": case["evidence_tags"],
                        "allowed_actions": case["allowed_actions"],
                        "expected_action": case["expected_action"],
                        "expected_chapter": chapter_id,
                        "spans": [
                            {
                                "start": start,
                                "end": end,
                                "operation_start": start,
                                "operation_end": operation_end,
                                "chapter_id": chapter_id,
                                "original": case["text"],
                            }
                        ],
                    }
                )
        input_chunks.append("\n")
        clean_chunks.append("\n")
        cursor += 1
        chapters.append(
            {
                "chapter_id": chapter_id,
                "title": title,
                "start": chapter_start,
                "end": cursor,
            }
        )

    text = "".join(input_chunks)
    expected_output = "".join(clean_chunks)
    _enrich_document_candidates(text, chapters, annotations)
    record = _document_record(
        "ads-mixed",
        path,
        text,
        expected_output,
        chapters,
        annotations,
        expected_output_path=expected_path,
        front_matter_span={"start": 0, "end": len(front_matter)},
    )
    return record, {
        path: text.encode("utf-8"),
        expected_path: expected_output.encode("utf-8"),
    }


def _chapter_for_offset(
    chapters: list[dict[str, Any]],
    offset: int,
) -> str:
    for chapter in chapters:
        if chapter["start"] <= offset < chapter["end"]:
            return str(chapter["chapter_id"])
    raise ValueError(f"offset {offset} is outside every expected chapter")


def _delete_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    result = text
    for start, end in sorted(ranges, reverse=True):
        result = result[:start] + result[end:]
    return result


def _enrich_document_candidates(
    text: str,
    chapters: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> None:
    chapter_by_id = {
        str(chapter["chapter_id"]): chapter
        for chapter in chapters
    }
    input_sha256 = sha256_text(text)
    for candidate in candidates:
        originals: list[str] = []
        mutation_ranges: list[tuple[int, int]] = []
        for anchor_index, span in enumerate(candidate["spans"], 1):
            start = int(span["start"])
            end = int(span["end"])
            original = str(span["original"])
            chapter_id = str(span["chapter_id"])
            chapter = chapter_by_id[chapter_id]
            originals.append(original)
            span["anchor_id"] = (
                f"{candidate['candidate_id']}-anchor-{anchor_index:03d}"
            )
            span["line"] = text.count("\n", 0, start) + 1
            span["prefix"] = text[max(0, start - 24) : start]
            span["suffix"] = text[end : min(len(text), end + 24)]
            span["expected_locator"] = {
                "kind": "chapter",
                "chapter_id": chapter_id,
                "title": chapter["title"],
            }
            if candidate["expected_action"] == "delete":
                mutation = {
                    "start": int(span.pop("operation_start")),
                    "end": int(span.pop("operation_end")),
                    "replacement": "",
                }
                span["mutation"] = mutation
                mutation_ranges.append((mutation["start"], mutation["end"]))
            else:
                span.pop("operation_start")
                span.pop("operation_end")
                span["mutation"] = None

        candidate["candidate_text_sha256"] = sha256_text("\n".join(originals))
        candidate["input_sha256"] = input_sha256
        expected_output = (
            _delete_ranges(text, mutation_ranges)
            if candidate["expected_action"] == "delete"
            else text
        )
        candidate["expected_output_sha256"] = sha256_text(expected_output)


def _compose_rollback_document() -> tuple[dict[str, Any], dict[str, bytes]]:
    path = "texts/rollback.txt"
    expected_path = "expected/rollback.cleaned.txt"
    shared = "站外提示：请访问 https://reader.example.com/shared 获取更新。"
    unique = "下载提示：请访问 https://reader.example.com/only-one 获取文件。"
    sections = [
        (
            "chapter-1",
            "第一章",
            ["人物甲记录第一段场景。", shared, "第一段场景继续。"],
        ),
        (
            "chapter-2",
            "第二章",
            [
                "人物甲记录第二段场景。",
                shared,
                unique,
                "第二段场景继续。",
            ],
        ),
        (
            "chapter-3",
            "第三章",
            ["人物甲记录第三段场景。", "第三段场景继续。"],
        ),
    ]
    base_record, files = _section_document(
        "rollback",
        path,
        "匿名回退样本\n\n",
        sections,
    )
    text = files[path].decode("utf-8")
    chapters = base_record["expected_chapters"]

    def spans_for(original: str) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        search_from = 0
        while True:
            start = text.find(original, search_from)
            if start < 0:
                break
            end = start + len(original)
            operation_end = end + 1 if text[end : end + 1] == "\n" else end
            spans.append(
                {
                    "start": start,
                    "end": end,
                    "operation_start": start,
                    "operation_end": operation_end,
                    "chapter_id": _chapter_for_offset(chapters, start),
                    "original": original,
                }
            )
            search_from = end
        return spans

    shared_spans = spans_for(shared)
    unique_spans = spans_for(unique)
    candidates = [
        {
            "candidate_id": "rollback-shared",
            "classification": "explicit_ad",
            "allowed_actions": ["delete"],
            "expected_action": "delete",
            "surface_expectation": "must_surface",
            "evidence_tags": ["url", "multi_anchor", "cross_chapter"],
            "expected_chapters": ["chapter-1", "chapter-2"],
            "spans": shared_spans,
        },
        {
            "candidate_id": "rollback-single",
            "classification": "explicit_ad",
            "allowed_actions": ["delete"],
            "expected_action": "delete",
            "surface_expectation": "must_surface",
            "evidence_tags": ["url", "single_anchor"],
            "expected_chapters": ["chapter-2"],
            "spans": unique_spans,
        },
    ]
    _enrich_document_candidates(text, chapters, candidates)
    operation_ranges = [
        (span["mutation"]["start"], span["mutation"]["end"])
        for candidate in candidates
        for span in candidate["spans"]
    ]
    expected_output = _delete_ranges(text, operation_ranges)
    all_anchor_ids = {
        span["anchor_id"]
        for candidate in candidates
        for span in candidate["spans"]
    }

    def outcome(
        outcome_id: str,
        restored_anchor_ids: set[str],
    ) -> tuple[dict[str, Any], str, str]:
        remaining_ranges: list[tuple[int, int]] = []
        remaining_ids: list[str] = []
        for candidate in candidates:
            for span in candidate["spans"]:
                if span["anchor_id"] in restored_anchor_ids:
                    continue
                remaining_ids.append(span["anchor_id"])
                remaining_ranges.append(
                    (span["mutation"]["start"], span["mutation"]["end"])
                )
        outcome_text = _delete_ranges(text, remaining_ranges)
        outcome_path = f"expected/rollback.{outcome_id}.txt"
        return (
            {
                "outcome_id": outcome_id,
                "path": outcome_path,
                "sha256": sha256_text(outcome_text),
                "restored_anchor_ids": sorted(restored_anchor_ids),
                "remaining_deleted_anchor_ids": sorted(remaining_ids),
                "invalidated_stages": ["5_layout", "6_verify", "7_export", "review"],
            },
            outcome_path,
            outcome_text,
        )

    shared_ids = {
        span["anchor_id"]
        for span in candidates[0]["spans"]
    }
    single_ids = {
        span["anchor_id"]
        for span in candidates[1]["spans"]
    }
    chapter_1_ids = {
        span["anchor_id"]
        for candidate in candidates
        for span in candidate["spans"]
        if span["chapter_id"] == "chapter-1"
    }
    chapter_2_ids = {
        span["anchor_id"]
        for candidate in candidates
        for span in candidate["spans"]
        if span["chapter_id"] == "chapter-2"
    }
    outcome_specs = [
        outcome("all", all_anchor_ids),
        outcome("module-ads", all_anchor_ids),
        outcome("chapter-1", chapter_1_ids),
        outcome("chapter-2", chapter_2_ids),
        outcome("point-shared", shared_ids),
        outcome("point-single", single_ids),
    ]
    record = _document_record(
        "rollback",
        path,
        text,
        expected_output,
        chapters,
        candidates,
        expected_output_path=expected_path,
        front_matter_span=base_record["front_matter_span"],
    )
    record["rollback_outcomes"] = [spec[0] for spec in outcome_specs]
    files[expected_path] = expected_output.encode("utf-8")
    for _, outcome_path, outcome_text in outcome_specs:
        files[outcome_path] = outcome_text.encode("utf-8")
    return record, files


def make_large_novel(
    minimum_chars: int = 200_000,
    seed: int = DEFAULT_LARGE_SEED,
) -> str:
    if minimum_chars < 1_000:
        raise ValueError("minimum_chars must be at least 1000")
    rng = random.Random(seed)
    actions = ("观察", "记录", "整理", "核对", "等待")
    objects = ("纸页", "窗影", "灯光", "脚步", "雨声")
    chunks = ["匿名运行时大文本\n\n"]
    paragraph = 0
    current_length = len(chunks[0])
    while current_length < minimum_chars:
        paragraph += 1
        if paragraph % 80 == 1:
            chunk = f"第{(paragraph - 1) // 80 + 1}章\n"
        elif paragraph % 37 == 0:
            chunk = (
                f"站外提示 {paragraph:05d}：请访问 "
                f"https://reader.example.com/generated/{paragraph:05d} 获取更新。\n"
            )
        else:
            chunk = (
                f"生成段落 {paragraph:05d}：人物甲{rng.choice(actions)}"
                f"{rng.choice(objects)}，随后继续当前场景。\n"
            )
        chunks.append(chunk)
        current_length += len(chunk)
    return "".join(chunks)


def make_candidate_explosion_fixture(candidate_count: int = 500) -> dict[str, Any]:
    if candidate_count < 1:
        raise ValueError("candidate_count must be positive")
    chunks = ["匿名候选密集文本\n", "第一章\n"]
    cursor = sum(len(chunk) for chunk in chunks)
    candidates: list[dict[str, Any]] = []
    for index in range(1, candidate_count + 1):
        original = (
            f"站外提示 {index:05d}：请访问 "
            f"https://reader.example.com/bulk/{index:05d} 获取更新。"
        )
        start = cursor
        line = original + "\n"
        chunks.append(line)
        cursor += len(line)
        candidates.append(
            {
                "candidate_id": f"runtime-ad-{index:05d}",
                "classification": "explicit_ad",
                "surface_expectation": "must_surface",
                "allowed_actions": ["delete"],
                "expected_action": "delete",
                "expected_chapter": "chapter-1",
                "span": {
                    "start": start,
                    "end": start + len(original),
                    "line": index + 2,
                    "original": original,
                    "mutation": {
                        "start": start,
                        "end": cursor,
                        "replacement": "",
                    },
                },
                "candidate_text_sha256": sha256_text(original),
            }
        )
    text = "".join(chunks)
    input_sha256 = sha256_text(text)
    for candidate in candidates:
        mutation = candidate["span"]["mutation"]
        candidate["input_sha256"] = input_sha256
        candidate["expected_output_sha256"] = sha256_text(
            text[: mutation["start"]] + text[mutation["end"] :]
        )
    return {
        "text": text,
        "input_sha256": input_sha256,
        "candidates": candidates,
    }


def make_candidate_explosion(candidate_count: int = 500) -> str:
    return str(make_candidate_explosion_fixture(candidate_count)["text"])


def _encoding_records() -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    definitions = [
        (
            "utf8",
            "encodings/utf8.txt",
            "utf-8",
            "匿名编码样本\n第一章\n纸页保持整洁。\n",
        ),
        (
            "utf8-bom",
            "encodings/utf8-bom.txt",
            "utf-8-sig",
            "匿名编码样本\n第二章\n窗边的风声逐渐安静。\n",
        ),
        (
            "gb18030",
            "encodings/gb18030.txt",
            "gb18030",
            "匿名编码样本\r\n第三章\r\n灯光照在空白纸页上。\r\n",
        ),
        (
            "big5",
            "encodings/big5.txt",
            "big5",
            "匿名編碼樣本\r\n第四章\r\n窗邊的風聲逐漸安靜。\r\n",
        ),
    ]
    records: list[dict[str, Any]] = []
    files: dict[str, bytes] = {}
    for case_id, path, encoding, text in definitions:
        encoded = text.encode(encoding)
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        files[path] = encoded
        records.append(
            {
                "case_id": case_id,
                "path": path,
                "encoding": encoding,
                "decoded_text": text,
                "decoded_text_sha256": sha256_text(text),
                "encoded_sha256": sha256_bytes(encoded),
                "source_newline": "CRLF" if "\r\n" in text else "LF",
                "expected_normalized_text": normalized,
                "expected_output_sha256": sha256_text(normalized),
                "provenance": PROVENANCE,
            }
        )
    return records, files


def _blocked_encoding_records() -> list[dict[str, Any]]:
    definitions = [
        ("ambiguous-big5-gb18030", "a440", "ambiguous_strict_decoding"),
        ("truncated-multibyte", "813081", "no_strict_decoder"),
        ("invalid-byte-sequence", "ffff80", "no_strict_decoder"),
    ]
    return [
        {
            "case_id": case_id,
            "raw_hex": raw_hex,
            "input_sha256": sha256_bytes(bytes.fromhex(raw_hex)),
            "expected_status": "blocked",
            "expected_reason": reason,
            "provenance": PROVENANCE,
        }
        for case_id, raw_hex, reason in definitions
    ]


def _malformed_jsonl_records() -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    definitions = [
        (
            "valid-control",
            "malformed/valid-control.jsonl",
            (
                b'{"candidate_id":"case-001","decision":"keep"}\n'
                b'{"candidate_id":"case-002","decision":"delete","anchors":[]}\n'
            ),
            "ok",
            None,
            None,
        ),
        (
            "bad-json",
            "malformed/bad-json.jsonl",
            b'{"candidate_id":"case-001","decision":"delete"\n',
            "load_error",
            "invalid_json",
            1,
        ),
        (
            "non-object",
            "malformed/non-object.jsonl",
            b'["case-001","delete"]\n',
            "load_error",
            "non_object_record",
            1,
        ),
        (
            "mixed-records",
            "malformed/mixed-records.jsonl",
            (
                b'{"candidate_id":"case-001","decision":"keep"}\n'
                b'{"candidate_id":"case-002",}\n'
            ),
            "load_error",
            "invalid_json",
            2,
        ),
        (
            "duplicate-id",
            "malformed/duplicate-id.jsonl",
            (
                b'{"candidate_id":"case-001","decision":"keep"}\n'
                b'{"candidate_id":"case-001","decision":"delete","anchors":[]}\n'
            ),
            "semantic_error",
            "duplicate_candidate_id",
            2,
        ),
        (
            "wrong-anchors-type",
            "malformed/wrong-anchors-type.jsonl",
            b'{"candidate_id":"case-001","decision":"delete","anchors":"invalid"}\n',
            "semantic_error",
            "anchors_not_array",
            1,
        ),
        (
            "negative-offset",
            "malformed/negative-offset.jsonl",
            (
                b'{"candidate_id":"case-001","decision":"delete",'
                b'"anchors":[{"offset":-1,"original":"sample"}]}\n'
            ),
            "semantic_error",
            "negative_offset",
            1,
        ),
    ]
    records: list[dict[str, Any]] = []
    files: dict[str, bytes] = {}
    for (
        case_id,
        path,
        payload,
        expected_result,
        expected_error_kind,
        expected_error_line,
    ) in definitions:
        files[path] = payload
        records.append(
            {
                "case_id": case_id,
                "path": path,
                "expected_result": expected_result,
                "expected_error_kind": expected_error_kind,
                "expected_error_line": expected_error_line,
                "input_sha256": sha256_bytes(payload),
                "provenance": PROVENANCE,
            }
        )
    return records, files


def build_fixture_bundle(root: Path = FIXTURE_ROOT) -> dict[str, Any]:
    root = Path(root)
    files: dict[str, bytes] = {}
    cases = make_ad_cases()
    documents: list[dict[str, Any]] = []

    ads_document, ads_files = _compose_ads_document(cases)
    documents.append(ads_document)
    files.update(ads_files)

    chapter_document, chapter_files = _section_document(
        "chapter-variants",
        "texts/chapter_variants.txt",
        "匿名章节格式样本\n用途：验证章节边界。\n\n",
        [
            (
                "chapter-1",
                "第一章",
                [
                    "段落甲保持安静。",
                    "人物甲说：“第九章只是对话中的编号。”",
                    "第十章 这一行带有句末标点。",
                    "最新章节列表只是场景中的界面文字。",
                    "段落乙继续记录。",
                ],
            ),
            ("chapter-2", "第2章：样例段", ["段落甲描述灯光。", "段落乙描述窗影。"]),
            ("chapter-3", "第三回", ["段落甲描述脚步。", "段落乙描述雨声。"]),
            ("chapter-4", "卷四", ["段落甲描述纸页。", "段落乙描述墨迹。"]),
            ("chapter-5", "序章", ["段落甲建立背景。", "段落乙结束背景。"]),
            ("chapter-6", "Chapter-6", ["段落甲使用英文编号。", "段落乙保持中文正文。"]),
            ("chapter-7", "7、样例节", ["段落甲使用列表编号。", "段落乙仍是正文。"]),
            ("chapter-8", "8 样例段", ["段落甲使用空格编号。", "段落乙结束样本。"]),
        ],
    )
    documents.append(chapter_document)
    files.update(chapter_files)

    front_document, front_files = _section_document(
        "front-matter",
        "texts/front_matter.txt",
        "匿名前置内容\n用途：验证前置说明不会丢失。\n版本：测试数据。\n\n",
        [
            ("chapter-1", "第一章", ["正文从这里开始。"]),
            ("chapter-2", "第二章", ["正文从这里继续。"]),
        ],
    )
    documents.append(front_document)
    files.update(front_files)

    no_chapter_text = (
        "匿名无章节文本\n\n"
        "段落甲只描述纸页与灯光。\n\n"
        "段落乙包含对话，但没有章节标题。\n\n"
        "段落丙结束这个合成样本。\n"
    )
    no_chapter_path = "texts/no_chapters.txt"
    files[no_chapter_path] = no_chapter_text.encode("utf-8")
    documents.append(
        _document_record(
            "no-chapters",
            no_chapter_path,
            no_chapter_text,
            no_chapter_text,
            [],
        )
    )

    layout_document, layout_files = _section_document(
        "layout-tokens",
        "texts/layout_tokens.txt",
        "匿名排版样本\n\n",
        [
            (
                "chapter-1",
                "第一章",
                [
                    "数值为 3.14，版本为 v1.2.3，时间是 08:30。",
                    "邮箱是 notice@example.com。",
                    "地址是 https://reader.example.com/path?q=1&part=2。",
                    r"相对路径是 folder\sample-a.txt，另一种写法是 folder/sample-a.txt。",
                    "中文句子,可能带有半角标点?但保护字段不能改变.",
                    "作者的话：这一行只用于布局策略测试。",
                ],
            )
        ],
    )
    layout_text = layout_files["texts/layout_tokens.txt"].decode("utf-8")
    protected_tokens = [
        "3.14",
        "v1.2.3",
        "08:30",
        "notice@example.com",
        "https://reader.example.com/path?q=1&part=2",
        r"folder\sample-a.txt",
        "folder/sample-a.txt",
    ]
    layout_document["must_preserve"] = [
        {
            "token": token,
            "input_count": layout_text.count(token),
            "expected_output_count": layout_text.count(token),
        }
        for token in protected_tokens
    ]
    layout_document["layout_invariants"] = {
        "protected_tokens_unchanged": True,
        "headings_indented": False,
        "author_note_kept_once": True,
        "final_newline": True,
    }
    documents.append(layout_document)
    files.update(layout_files)

    rollback_document, rollback_files = _compose_rollback_document()
    documents.append(rollback_document)
    files.update(rollback_files)

    encodings, encoding_files = _encoding_records()
    files.update(encoding_files)
    blocked_encodings = _blocked_encoding_records()
    malformed, malformed_files = _malformed_jsonl_records()
    files.update(malformed_files)

    large_text = make_large_novel()
    explosion_fixture = make_candidate_explosion_fixture()
    explosion_text = str(explosion_fixture["text"])
    explosion_catalog = json.dumps(
        explosion_fixture["candidates"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "provenance": PROVENANCE,
        "offset_contract": {
            "unit": "python_unicode_code_point",
            "interval": "half_open",
            "canonical_encoding": "utf-8",
            "canonical_newline": "LF",
            "anchor_span": "start/end",
            "mutation_span": "span.mutation.start/end",
        },
        "summary": {
            "explicit_ad_count": sum(
                case["classification"] == "explicit_ad" for case in cases
            ),
            "hard_negative_count": sum(
                case["classification"] == "narrative" for case in cases
            ),
            "document_count": len(documents),
            "encoding_case_count": len(encodings),
            "blocked_encoding_case_count": len(blocked_encodings),
            "jsonl_case_count": len(malformed),
            "malformed_jsonl_case_count": sum(
                case["expected_result"] != "ok" for case in malformed
            ),
            "must_surface_count": sum(
                case["surface_expectation"] == "must_surface" for case in cases
            ),
            "must_suppress_count": sum(
                case["surface_expectation"] == "must_suppress" for case in cases
            ),
            "may_surface_count": sum(
                case["surface_expectation"] == "may_surface" for case in cases
            ),
        },
        "candidate_catalog": cases,
        "documents": documents,
        "artifacts": {
            relative_path: {
                "sha256": sha256_bytes(payload),
                "size_bytes": len(payload),
            }
            for relative_path, payload in sorted(files.items())
        },
        "encoding_cases": encodings,
        "blocked_encoding_cases": blocked_encodings,
        "malformed_jsonl_cases": malformed,
        "runtime_generators": {
            "large_novel": {
                "minimum_chars": 200_000,
                "seed": DEFAULT_LARGE_SEED,
                "char_count": len(large_text),
                "sha256": sha256_text(large_text),
            },
            "candidate_explosion": {
                "candidate_count": 500,
                "char_count": len(explosion_text),
                "sha256": sha256_text(explosion_text),
                "candidate_catalog_sha256": sha256_text(explosion_catalog),
            },
        },
    }
    manifest["bundle_files"] = sorted([*files, "gold_manifest.json"])

    for relative_path, payload in sorted(files.items()):
        _write_bytes(root / relative_path, payload)
    _write_json(root / "gold_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate deterministic anonymous gold fixtures."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=FIXTURE_ROOT,
        help="Output fixture directory.",
    )
    args = parser.parse_args()
    manifest = build_fixture_bundle(args.output)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "files": len(manifest["bundle_files"]),
                "explicit_ads": manifest["summary"]["explicit_ad_count"],
                "hard_negatives": manifest["summary"]["hard_negative_count"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
