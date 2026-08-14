from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any

from ad_rules import (
    BARE_COPY_MARKER_RE,
    SOURCE_MARKER_RE,
    normalize_match_text,
    signal_keys,
    site_alias_re,
    site_entities_from_normalized,
)
from common import (
    WorkspaceTransaction,
    read_utf8,
    resolve_workspace_paths,
    sha256_file,
    workspace_transaction_lock,
    write_json,
    write_jsonl,
)
from parse_structure import match_chapter
import ad_decision_policy
from scan_identity import (
    attach_anchor_ids,
    attach_candidate_fingerprints,
    build_scan_identity,
    load_bound_structure,
)


KNOWN_SITE_FINGERPRINT_RE = site_alias_re(short_only=True)

STRONG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", re.compile(r"https?://|www\.|[A-Za-z0-9_-]+\.(?:com|net|cn|org|cc|top|xyz|vip)\b", re.I)),
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.I)),
    ("contact", re.compile(r"(?:QQ|QQ群|微信|VX|vx|wx|公众号|书友群)[:：]?[A-Za-z0-9_\-]{4,}", re.I)),
    ("download", re.compile(r"(?:txt下载|全本下载|电子书下载|免费下载|下载地址|本书下载)", re.I)),
    (
        "watermark",
        re.compile(
            rf"(?:{SOURCE_MARKER_RE.pattern}|仅供学习交流|手机用户请访问|请访问|转载请注明|"
            r"在线阅读全文访问|免费.{0,6}阅读网站|优秀的在线阅读网站|欢迎.{0,4}[捧棒]场)",
            re.I,
        ),
    ),
    ("reader_site", re.compile(r"(?:最新章节|更新最快|无弹窗|在线阅读|阅读网站|起点中文网首发)", re.I)),
)
STRONG_SIGNAL_NAMES = signal_keys("strong")

WEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("author_note", re.compile(r"(?:作者的话|PS[:：]?|求收藏|求推荐|求月票|打赏)", re.I)),
    ("copy_marker", SOURCE_MARKER_RE),
)
SHORT_EXTERNAL_SIGNAL_NAMES = signal_keys("short")
NEIGHBOR_EXTERNAL_SIGNAL_NAMES = signal_keys("neighbor")
MAX_PAGE_SIZE = 10_000
MAX_ANCHORS_PER_CANDIDATE = 10_000
MAX_BOUNDARY_RADIUS = 1_000_000

def fold_for_matching(value: str) -> str:
    return normalize_match_text(value)


def _bounded_integer(name: str, value: object, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def validate_scan_parameters(
    min_chars: int,
    max_candidates: int,
    max_anchors: int,
    near_scan_scope: str,
    near_boundary_chars: int,
) -> None:
    _bounded_integer("min_chars", min_chars, 1, 500)
    _bounded_integer("max_candidates", max_candidates, 1, MAX_PAGE_SIZE)
    _bounded_integer("max_anchors", max_anchors, 1, MAX_ANCHORS_PER_CANDIDATE)
    _bounded_integer("near_boundary_chars", near_boundary_chars, 0, MAX_BOUNDARY_RADIUS)
    if near_scan_scope not in {"boundary", "all"}:
        raise ValueError("near_scan_scope must be 'boundary' or 'all'")


def normalize_text(value: str) -> str:
    return normalize_folded_text(fold_for_matching(value.strip()))


def normalize_folded_text(value: str) -> str:
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[0-9零〇一二两三四五六七八九十百千万]+", "#", value)
    return value.lower()


def stable_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def is_candidate_block(text: str, min_chars: int) -> bool:
    stripped = text.strip()
    if len(stripped) < min_chars or len(stripped) > 500:
        return False
    if match_chapter(stripped) is not None:
        return False
    if len(re.sub(r"[\W_]+", "", stripped, flags=re.UNICODE)) < min_chars // 2:
        return False
    return True


def signal_names_from_folded(folded: str) -> list[str]:
    names: list[str] = []
    for name, pattern in STRONG_PATTERNS + WEAK_PATTERNS:
        if pattern.search(folded):
            names.append(name)
    entities = site_entities_from_normalized(folded)
    if "reader_site" not in names and any(not entity.startswith("domain:") for entity in entities):
        names.append("reader_site")
    return names


def signal_names(text: str) -> list[str]:
    folded = fold_for_matching(text)
    return signal_names_from_folded(folded)


def is_short_external_block(text: str) -> bool:
    return short_external_analysis(text)[0]


def short_external_analysis(text: str) -> tuple[bool, str, list[str]]:
    stripped = text.strip()
    if not stripped or len(stripped) > 500 or match_chapter(stripped) is not None:
        return False, "", []
    folded = fold_for_matching(stripped)
    names = signal_names_from_folded(folded)
    matched = bool(
        SHORT_EXTERNAL_SIGNAL_NAMES.intersection(names)
        or KNOWN_SITE_FINGERPRINT_RE.search(folded)
    )
    return matched, folded, names


def has_strong_signal(text: str) -> bool:
    return bool(STRONG_SIGNAL_NAMES.intersection(signal_names(text)))


def signal_strength_from_names(signals: list[str]) -> str:
    if any(signal in STRONG_SIGNAL_NAMES for signal in signals):
        return "strong"
    if signals:
        return "weak"
    return "none"


def quota_bucket_for_candidate(candidate: dict[str, Any]) -> str:
    layer = str(candidate.get("layer", "")).upper()
    if layer == "L1":
        return "l1_strong" if candidate.get("signal_strength") == "strong" else "l1_weak"
    if layer == "L2":
        return "l2"
    if layer == "L3":
        return "l3"
    if layer == "L4":
        return "l4"
    return "other"


def same_chapter(
    chapters: list[dict[str, Any]],
    left_offset: int,
    right_offset: int,
) -> bool:
    if not chapters:
        return True
    return chapter_lookup(chapters, left_offset) is chapter_lookup(chapters, right_offset)


def annotate_neighbor_spans(
    blocks: list[dict[str, Any]],
    nonempty_lines: set[int],
    chapters: list[dict[str, Any]],
) -> int:
    by_line = {int(block["line"]): block for block in blocks}
    pair_count = 0
    for left in blocks:
        left_line = int(left["line"])
        for line_distance in (1, 2):
            right = by_line.get(left_line + line_distance)
            if right is None:
                continue
            right_line = int(right["line"])
            if any(line in nonempty_lines for line in range(left_line + 1, right_line)):
                continue
            left_signals = set(left.get("signals", []))
            right_signals = set(right.get("signals", []))
            if not left_signals or not right_signals or not (
                NEIGHBOR_EXTERNAL_SIGNAL_NAMES.intersection(left_signals)
                or NEIGHBOR_EXTERNAL_SIGNAL_NAMES.intersection(right_signals)
            ):
                continue
            if not same_chapter(chapters, int(left["start"]), int(right["start"])):
                continue

            left.setdefault("_neighbor_spans", []).append(
                {
                    "source_offset": int(left["start"]),
                    "source_line": left_line,
                    "neighbor_offset": int(right["start"]),
                    "neighbor_line": right_line,
                    "direction": "after",
                    "line_distance": line_distance,
                    "original": right["text"],
                    "signals": list(right.get("signals", [])),
                    "signal_strength": signal_strength_from_names(list(right.get("signals", []))),
                }
            )
            right.setdefault("_neighbor_spans", []).append(
                {
                    "source_offset": int(right["start"]),
                    "source_line": right_line,
                    "neighbor_offset": int(left["start"]),
                    "neighbor_line": left_line,
                    "direction": "before",
                    "line_distance": line_distance,
                    "original": left["text"],
                    "signals": list(left.get("signals", [])),
                    "signal_strength": signal_strength_from_names(list(left.get("signals", []))),
                }
            )
            pair_count += 1
    return pair_count


def split_blocks(
    text: str,
    min_chars: int,
    chapters: list[dict[str, Any]] | None = None,
    metrics: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    nonempty_lines: set[int] = set()
    short_external_count = 0
    long_external_count = 0
    suppressed_copy_marker_count = 0
    offset = 0
    for line_no, line in enumerate(text.splitlines(keepends=True), 1):
        raw = line.rstrip("\n")
        stripped = raw.strip()
        if stripped:
            nonempty_lines.add(line_no)
        leading = len(raw) - len(raw.lstrip())
        start = offset + leading
        end = start + len(stripped)
        regular_candidate = is_candidate_block(stripped, min_chars)
        short_external = False
        long_external = False
        folded = ""
        signals: list[str] = []
        if not regular_candidate:
            short_external, folded, signals = short_external_analysis(stripped)
            if not short_external and len(stripped) > 500:
                folded = fold_for_matching(stripped)
                signals = signal_names_from_folded(folded)
                long_external = bool(
                    STRONG_SIGNAL_NAMES.intersection(signals)
                    or KNOWN_SITE_FINGERPRINT_RE.search(folded)
                )
        if regular_candidate or short_external or long_external:
            if regular_candidate:
                folded = fold_for_matching(stripped)
                signals = signal_names_from_folded(folded)
            if short_external:
                short_external_count += 1
            if long_external:
                long_external_count += 1
            if BARE_COPY_MARKER_RE.search(folded) and not SOURCE_MARKER_RE.search(folded):
                suppressed_copy_marker_count += 1
            block = {
                "text": stripped,
                "start": start,
                "end": end,
                "line": line_no,
                "normalized": normalize_folded_text(folded),
                "signals": signals,
            }
            if long_external:
                block["mutation_guard"] = "long_line_mixed_content"
            blocks.append(block)
        offset += len(line)
    neighbor_pair_count = annotate_neighbor_spans(blocks, nonempty_lines, chapters or [])
    if metrics is not None:
        metrics.update(
            {
                "short_external_block_count": short_external_count,
                "long_external_block_count": long_external_count,
                "suppressed_copy_marker_block_count": suppressed_copy_marker_count,
                "neighbor_pair_count": neighbor_pair_count,
            }
        )
    return blocks


def load_chapters(input_path: Path, structure_path: Path) -> list[dict[str, Any]]:
    structure = load_bound_structure(input_path, structure_path)
    return list(structure["locators"])


def chapter_lookup(chapters: list[dict[str, Any]], offset: int) -> dict[str, Any] | None:
    low = 0
    high = len(chapters)
    while low < high:
        middle = (low + high) // 2
        if int(chapters[middle].get("start_offset", 0)) <= offset:
            low = middle + 1
        else:
            high = middle
    index = low - 1
    if index < 0 or index >= len(chapters):
        return None
    chapter = chapters[index]
    end = chapter.get("end_offset")
    if isinstance(end, int) and offset >= end:
        return None
    return chapter


def context_for(text: str, start: int, end: int, width: int) -> dict[str, str]:
    return {
        "before": text[max(0, start - width) : start],
        "original": text[start:end],
        "after": text[end : min(len(text), end + width)],
    }


def anchor_for(text: str, block: dict[str, Any], chapters: list[dict[str, Any]]) -> dict[str, Any]:
    start = int(block["start"])
    end = int(block["end"])
    chapter = chapter_lookup(chapters, start)
    anchor = {
        "offset": start,
        "end": end,
        "original": text[start:end],
        "prefix": text[max(0, start - 10) : start],
        "suffix": text[end : min(len(text), end + 10)],
        "line": block["line"],
    }
    if chapter:
        if chapter.get("kind") != "chapter":
            anchor["locator"] = {
                "kind": chapter.get("kind"),
                "index": chapter.get("index"),
                "title": chapter.get("title"),
            }
        else:
            anchor["chapter"] = {
                "index": chapter.get("index"),
                "title": chapter.get("title"),
            }
    return anchor


def candidate_from_blocks(
    candidate_id: str,
    layer: str,
    detector: str,
    reason: str,
    blocks: list[dict[str, Any]],
    text: str,
    chapters: list[dict[str, Any]],
    priority: str,
    risk_hint: str,
    max_anchors: int,
) -> dict[str, Any]:
    selected_blocks = blocks[:max_anchors]
    anchors = [anchor_for(text, block, chapters) for block in selected_blocks]
    contexts = [
        context_for(text, int(block["start"]), int(block["end"]), 120)
        for block in selected_blocks[:3]
    ]
    neighbor_spans = [
        span
        for block in selected_blocks
        for span in block.get("_neighbor_spans", [])
    ]
    total_neighbor_spans = sum(len(block.get("_neighbor_spans", [])) for block in blocks)
    sample = blocks[0]["text"]
    signals = sorted({name for block in blocks for name in block.get("signals", [])})
    signal_strength = signal_strength_from_names(signals)
    quota_bucket = "l1_strong" if layer == "L1" and signal_strength == "strong" else {
        "L1": "l1_weak",
        "L2": "l2",
        "L3": "l3",
        "L4": "l4",
    }.get(layer, "other")
    candidate = {
        "candidate_id": candidate_id,
        "layer": layer,
        "detector": detector,
        "priority": priority,
        "risk_hint": risk_hint,
        "reason": reason,
        "sample": sample,
        "signals": signals,
        "signal_strength": signal_strength,
        "quota_bucket": quota_bucket,
        "occurrence_count": len(blocks),
        "anchors_truncated": len(blocks) > max_anchors,
        "anchors": anchors,
        "contexts": contexts,
        "suggested_decision": {
            "candidate_id": candidate_id,
            "verdict": "uncertain",
            "confidence": None,
            "reason": "",
            "splice_strategy": "remove_paragraph",
            "risk": risk_hint,
            "anchors": anchors,
        },
    }
    mutation_guards = sorted(
        {
            str(block["mutation_guard"])
            for block in blocks
            if isinstance(block.get("mutation_guard"), str)
            and block["mutation_guard"]
        }
    )
    if mutation_guards:
        candidate["mutation_guard"] = mutation_guards[0]
        candidate["risk_hint"] = "high"
        candidate["suggested_decision"]["risk"] = "high"
    if neighbor_spans:
        candidate["neighbor_spans"] = neighbor_spans
        candidate["neighbor_spans_truncated"] = total_neighbor_spans > len(neighbor_spans)
    return candidate


def char_ngrams(value: str, n: int = 3) -> set[str]:
    value = normalize_text(value)
    if len(value) <= n:
        return {value} if value else set()
    return {value[i : i + n] for i in range(len(value) - n + 1)}


@lru_cache(maxsize=50_000)
def feature_bits(feature: str) -> tuple[int, ...]:
    digest = int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")
    return tuple(bit for bit in range(64) if digest & (1 << bit))


def simhash(features: set[str]) -> int:
    if not features:
        return 0
    weights = [-len(features)] * 64
    for feature in features:
        for bit in feature_bits(feature):
            weights[bit] += 2
    result = 0
    for bit, weight in enumerate(weights):
        if weight > 0:
            result |= 1 << bit
    return result


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class DSU:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        l_root = self.find(left)
        r_root = self.find(right)
        if l_root != r_root:
            self.parent[r_root] = l_root


def l1_exact(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for block in blocks:
        grouped[block["normalized"]].append(block)
    return [items for items in grouped.values() if len(items) >= 3]


def near_sorted_offset(offset: int, offsets: list[int], distance: int) -> bool:
    index = bisect.bisect_left(offsets, offset)
    return (
        (index < len(offsets) and offsets[index] - offset <= distance)
        or (index > 0 and offset - offsets[index - 1] <= distance)
    )


def l2_selection(
    blocks: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    already_covered: set[int],
    scope: str,
    boundary_chars: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    eligible = [
        block
        for block in blocks
        if int(block["start"]) not in already_covered and 20 <= len(block["normalized"]) <= 260
    ]
    if scope == "all" or not chapters or any(chapter.get("kind") == "fallback_chunk" for chapter in chapters):
        return eligible, {
            "scope": "all" if scope == "all" or not chapters else "fallback_all",
            "eligible_blocks": len(eligible),
            "selected_blocks": len(eligible),
            "boundary_chars": boundary_chars,
        }

    starts = [int(chapter.get("start_offset", 0)) for chapter in chapters]
    ends = [int(chapter.get("end_offset", 0)) for chapter in chapters]
    boundaries = sorted(starts + ends)
    first_start = starts[0] if starts else 0
    selected = [
        block
        for block in eligible
        if block["signals"]
        or int(block["start"]) < first_start
        or near_sorted_offset(int(block["start"]), boundaries, boundary_chars)
    ]
    return selected, {
        "scope": "boundary",
        "eligible_blocks": len(eligible),
        "selected_blocks": len(selected),
        "boundary_chars": boundary_chars,
    }


def l2_near(
    blocks: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    already_covered: set[int],
    scope: str,
    boundary_chars: int,
) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
    selected, metrics = l2_selection(blocks, chapters, already_covered, scope, boundary_chars)
    if len(selected) < 3:
        metrics["compared_pairs"] = 0
        metrics["group_count"] = 0
        return [], metrics

    features = [char_ngrams(block["text"]) for block in selected]
    hashes = [simhash(item) for item in features]
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, value in enumerate(hashes):
        for band in range(4):
            buckets[(band, (value >> (band * 16)) & 0xFFFF)].append(index)

    dsu = DSU(len(selected))
    checked: set[tuple[int, int]] = set()
    for bucket in buckets.values():
        if len(bucket) < 2 or len(bucket) > 200:
            continue
        for i_pos, left in enumerate(bucket):
            for right in bucket[i_pos + 1 :]:
                pair = (left, right) if left < right else (right, left)
                if pair in checked:
                    continue
                checked.add(pair)
                if hamming(hashes[left], hashes[right]) <= 3 or jaccard(features[left], features[right]) > 0.7:
                    dsu.union(left, right)

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, block in enumerate(selected):
        grouped[dsu.find(index)].append(block)
    groups = [items for items in grouped.values() if len(items) >= 3]
    metrics["compared_pairs"] = len(checked)
    metrics["group_count"] = len(groups)
    return groups, metrics


def l3_patterns(blocks: list[dict[str, Any]], already_covered: set[int]) -> list[list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for block in blocks:
        if int(block["start"]) in already_covered or not block["signals"]:
            continue
        strong = [name for name in block["signals"] if any(name == p_name for p_name, _ in STRONG_PATTERNS)]
        if strong:
            key = "strong:" + stable_hash(block["normalized"])
        else:
            key = "weak:" + stable_hash(block["normalized"])
        groups[key].append(block)
    return list(groups.values())


def l4_structural(blocks: list[dict[str, Any]], chapters: list[dict[str, Any]], already_covered: set[int]) -> list[list[dict[str, Any]]]:
    if not chapters:
        return []
    if any(ch.get("kind") == "fallback_chunk" for ch in chapters):
        return []
    chapter_starts = [int(ch.get("start_offset", 0)) for ch in chapters]
    chapter_ends = [int(ch.get("end_offset", 0)) for ch in chapters]
    boundaries = sorted(chapter_starts + chapter_ends)
    groups: list[list[dict[str, Any]]] = []
    for block in blocks:
        start = int(block["start"])
        if start in already_covered or len(block["text"]) > 160:
            continue
        before_first = start < chapter_starts[0]
        if (before_first or near_sorted_offset(start, boundaries, 299)) and block["signals"]:
            groups.append([block])
    return groups


def next_id(counter: list[int]) -> str:
    counter[0] += 1
    return f"AD-{counter[0]:04d}"


def candidate_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    risk_rank = {"low": 0, "medium": 1, "high": 2}
    layer_rank = {"L3": 0, "L1": 1, "L2": 2, "L4": 3}
    signal_rank = {"strong": 0, "weak": 1, "none": 2}
    return (
        signal_rank.get(str(item.get("signal_strength")), 9),
        priority_rank.get(str(item.get("priority")), 9),
        risk_rank.get(str(item.get("risk_hint")), 9),
        layer_rank.get(str(item.get("layer")), 9),
        -int(item.get("occurrence_count", 0)),
        str(item.get("sample", "")),
    )


def sort_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(candidates, key=candidate_sort_key)


def count_by(items: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get(field) or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def page_count(total: int, page_size: int) -> int:
    if total <= 0:
        return 0
    return (total + page_size - 1) // page_size


def assign_candidate_ids(candidates: list[dict[str, Any]]) -> None:
    for index, candidate in enumerate(candidates, 1):
        new_id = f"AD-{index:04d}"
        candidate["candidate_id"] = new_id
        if isinstance(candidate.get("suggested_decision"), dict):
            candidate["suggested_decision"]["candidate_id"] = new_id


def compose_candidate_order(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ranked = sort_candidates(candidates)
    for candidate in ranked:
        bucket = str(candidate.get("quota_bucket") or quota_bucket_for_candidate(candidate))
        candidate["quota_bucket"] = bucket
    assign_candidate_ids(ranked)
    return ranked


def build_candidate_pool(
    text: str,
    chapters: list[dict[str, Any]] | None = None,
    min_chars: int = 12,
    max_anchors: int = 120,
    near_scan_scope: str = "boundary",
    near_boundary_chars: int = 320,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    chapters = chapters or []
    timings: dict[str, float] = {}
    signal_metrics: dict[str, int] = {}
    stage_start = perf_counter()
    blocks = split_blocks(text, min_chars, chapters, signal_metrics)
    timings["split_blocks"] = round(perf_counter() - stage_start, 4)
    candidates: list[dict[str, Any]] = []
    covered: set[int] = set()
    counter = [0]

    stage_start = perf_counter()
    for group in l1_exact(blocks):
        risk = "low" if any(has_strong_signal(block["text"]) for block in group) else "medium"
        priority = "high" if risk == "low" else "medium"
        candidates.append(
            candidate_from_blocks(
                next_id(counter),
                "L1",
                "exact-repeat",
                "same standalone line or paragraph appears at least 3 times",
                group,
                text,
                chapters,
                priority,
                risk,
                max_anchors,
            )
        )
        covered.update(int(block["start"]) for block in group)

    timings["l1_exact"] = round(perf_counter() - stage_start, 4)
    stage_start = perf_counter()
    l2_groups, l2_metrics = l2_near(
        blocks,
        chapters,
        covered,
        near_scan_scope,
        near_boundary_chars,
    )
    for group in l2_groups:
        risk = "low" if any(has_strong_signal(block["text"]) for block in group) else "medium"
        candidates.append(
            candidate_from_blocks(
                next_id(counter),
                "L2",
                "near-repeat",
                "similar paragraphs clustered by SimHash/Jaccard",
                group,
                text,
                chapters,
                "high" if risk == "low" else "medium",
                risk,
                max_anchors,
            )
        )
        covered.update(int(block["start"]) for block in group)

    timings["l2_near"] = round(perf_counter() - stage_start, 4)
    stage_start = perf_counter()
    for group in l3_patterns(blocks, covered):
        risk = "low" if any(has_strong_signal(block["text"]) for block in group) else "medium"
        candidates.append(
            candidate_from_blocks(
                next_id(counter),
                "L3",
                "pattern-hit",
                "line contains ad/source/contact/download pattern",
                group,
                text,
                chapters,
                "high" if risk == "low" else "medium",
                risk,
                max_anchors,
            )
        )
        covered.update(int(block["start"]) for block in group)

    timings["l3_patterns"] = round(perf_counter() - stage_start, 4)
    stage_start = perf_counter()
    for group in l4_structural(blocks, chapters, covered):
        candidates.append(
            candidate_from_blocks(
                next_id(counter),
                "L4",
                "boundary-signal",
                "short signaled paragraph near chapter boundary",
                group,
                text,
                chapters,
                "low",
                "medium",
                max_anchors,
            )
        )
        covered.update(int(block["start"]) for block in group)

    timings["l4_structural"] = round(perf_counter() - stage_start, 4)
    return candidates, len(blocks), {
        "timings_seconds": timings,
        "l2": l2_metrics,
        "signal_metrics": signal_metrics,
    }


def scan_candidates(
    text: str,
    chapters: list[dict[str, Any]] | None = None,
    min_chars: int = 12,
    max_candidates: int = 300,
    max_anchors: int = 120,
    near_scan_scope: str = "boundary",
    near_boundary_chars: int = 320,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validate_scan_parameters(
        min_chars,
        max_candidates,
        max_anchors,
        near_scan_scope,
        near_boundary_chars,
    )

    scan_start = perf_counter()
    candidate_pool, block_count, performance = build_candidate_pool(
        text,
        chapters,
        min_chars,
        max_anchors,
        near_scan_scope,
        near_boundary_chars,
    )
    order_start = perf_counter()
    ordered_candidates = compose_candidate_order(candidate_pool)
    attach_edit_plan_blueprints(ordered_candidates)
    performance["timings_seconds"]["candidate_order"] = round(perf_counter() - order_start, 4)
    performance["timings_seconds"]["candidate_pool_total"] = round(perf_counter() - scan_start, 4)
    first_page = ordered_candidates[:max_candidates]

    summary = {
        "block_count": block_count,
        "candidate_count": len(first_page),
        "first_page_count": len(first_page),
        "total_candidate_count": len(ordered_candidates),
        "page_size": max_candidates,
        "page_count": page_count(len(ordered_candidates), max_candidates),
        "max_candidates_reached": len(ordered_candidates) > max_candidates,
        "by_layer": count_by(first_page, "layer"),
        "total_by_layer": count_by(ordered_candidates, "layer"),
        "by_quota_bucket": count_by(first_page, "quota_bucket"),
        "total_by_quota_bucket": count_by(ordered_candidates, "quota_bucket"),
        "strong_signal_count": sum(1 for item in ordered_candidates if item.get("signal_strength") == "strong"),
        "strong_signal_first_page_count": sum(1 for item in first_page if item.get("signal_strength") == "strong"),
        "performance": performance,
        "signal_metrics": performance.get("signal_metrics", {}),
    }
    summary["strong_signal_deferred_count"] = (
        int(summary["strong_signal_count"]) - int(summary["strong_signal_first_page_count"])
    )
    return ordered_candidates, summary


def attach_edit_plan_blueprints(candidates: list[dict[str, Any]]) -> None:
    """Attach conservative executable plans before candidate fingerprints exist."""
    for candidate in candidates:
        plan = ad_decision_policy.build_edit_plan(candidate)
        if plan is None:
            continue
        candidate["edit_plan"] = plan
        # A long line may carry an exact scanner plan, but it must retain its
        # stricter whole-line guard.  The formal compiler then accepts only a
        # bound exact-segment review for it.
        if not candidate.get("mutation_guard"):
            candidate["mutation_guard"] = "segment_review_required"
        candidate["risk_hint"] = "high"
        suggested = candidate.get("suggested_decision")
        if isinstance(suggested, dict):
            suggested["splice_strategy"] = "exact_segment"
            suggested["risk"] = "high"


def bind_edit_plans(candidates: list[dict[str, Any]]) -> None:
    for candidate in candidates:
        if candidate.get("edit_plan") is not None:
            ad_decision_policy.bind_edit_plan(candidate)


def scan_text(
    text: str,
    chapters: list[dict[str, Any]] | None = None,
    min_chars: int = 12,
    max_candidates: int = 300,
    max_anchors: int = 120,
    near_scan_scope: str = "boundary",
    near_boundary_chars: int = 320,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered_candidates, summary = scan_candidates(
        text=text,
        chapters=chapters,
        min_chars=min_chars,
        max_candidates=max_candidates,
        max_anchors=max_anchors,
        near_scan_scope=near_scan_scope,
        near_boundary_chars=near_boundary_chars,
    )
    return ordered_candidates, summary


def write_candidate_outputs(
    workspace: Path,
    transaction: WorkspaceTransaction,
    output_path: Path,
    candidates: list[dict[str, Any]],
    page_size: int,
    pages_dir: Path,
    page_paths: list[Path],
    stale_pages: list[Path],
) -> dict[str, Any]:
    first_page = candidates[:page_size]
    write_jsonl(transaction.stage_path(output_path), first_page)
    for stale_page in stale_pages:
        transaction.stage_delete(stale_page)

    page_manifest: list[dict[str, Any]] = []
    for page_number, (page_path, start) in enumerate(
        zip(page_paths, range(0, len(candidates), page_size)),
        1,
    ):
        page = candidates[start : start + page_size]
        staged_page = transaction.stage_path(page_path)
        write_jsonl(staged_page, page)
        page_manifest.append(
            {
                "file": str(page_path.relative_to(workspace)).replace("\\", "/"),
                "page_number": page_number,
                "record_count": len(page),
                "sha256": sha256_file(staged_page),
            }
        )

    return {
        "first_page": output_path.relative_to(workspace).as_posix(),
        "pages_dir": pages_dir.relative_to(workspace).as_posix(),
        "manifest": page_manifest,
        "page_count": len(page_manifest),
    }


def run(
    workspace: Path,
    input_value: str,
    output_value: str,
    min_chars: int,
    max_candidates: int,
    max_anchors: int,
    near_scan_scope: str = "boundary",
    near_boundary_chars: int = 320,
) -> dict[str, Any]:
    validate_scan_parameters(
        min_chars,
        max_candidates,
        max_anchors,
        near_scan_scope,
        near_boundary_chars,
    )
    with workspace_transaction_lock(workspace):
        return _run_locked(
            workspace,
            input_value,
            output_value,
            min_chars,
            max_candidates,
            max_anchors,
            near_scan_scope,
            near_boundary_chars,
        )


def _run_locked(
    workspace: Path,
    input_value: str,
    output_value: str,
    min_chars: int,
    max_candidates: int,
    max_anchors: int,
    near_scan_scope: str = "boundary",
    near_boundary_chars: int = 320,
) -> dict[str, Any]:
    validate_scan_parameters(
        min_chars,
        max_candidates,
        max_anchors,
        near_scan_scope,
        near_boundary_chars,
    )
    run_start = perf_counter()
    pages_dir_value = "candidates/ads_pages"
    report_value = "report/ads_scan_report.json"
    workspace, read_paths, initial_writes = resolve_workspace_paths(
        workspace,
        reads={
            "input": input_value,
            "chapters": "meta/chapters.json",
        },
        writes={
            "output": output_value,
            "pages_dir": pages_dir_value,
            "report": report_value,
        },
    )
    input_path = read_paths["input"]
    chapters_path = read_paths["chapters"]
    read_start = perf_counter()
    text = read_utf8(input_path)
    chapters = load_chapters(input_path, chapters_path)
    read_seconds = perf_counter() - read_start
    candidates, summary = scan_candidates(
        text,
        chapters,
        min_chars,
        max_candidates,
        max_anchors,
        near_scan_scope,
        near_boundary_chars,
    )
    attach_candidate_fingerprints(candidates)
    attach_anchor_ids(candidates)
    bind_edit_plans(candidates)
    scan_config = {
        "min_chars": min_chars,
        "max_candidates": max_candidates,
        "max_anchors": max_anchors,
        "near_scan_scope": near_scan_scope,
        "near_boundary_chars": near_boundary_chars,
    }
    scan_identity = build_scan_identity(
        "ads",
        input_path,
        chapters_path,
        scan_config,
        candidates,
    )

    pages_dir = initial_writes["pages_dir"]
    stale_pages = sorted(pages_dir.glob("ads_page_*.jsonl")) if pages_dir.exists() else []
    generated_page_values = [
        f"{pages_dir_value}/ads_page_{index:03d}.jsonl"
        for index, _ in enumerate(range(0, len(candidates), max_candidates), 1)
    ]
    stale_page_values = [path.relative_to(workspace).as_posix() for path in stale_pages]
    page_values = sorted(set(generated_page_values) | set(stale_page_values))
    page_write_names = {value: f"page_{index:03d}" for index, value in enumerate(page_values, 1)}
    all_write_values = {
        "output": output_value,
        "pages_dir": pages_dir_value,
        "report": report_value,
        **{page_write_names[value]: value for value in page_values},
    }
    workspace, _, write_paths = resolve_workspace_paths(
        workspace,
        reads={
            "input": input_value,
            "chapters": "meta/chapters.json",
        },
        writes=all_write_values,
    )
    output_path = write_paths["output"]
    pages_dir = write_paths["pages_dir"]
    page_paths = [write_paths[page_write_names[value]] for value in generated_page_values]

    with WorkspaceTransaction(workspace) as transaction:
        transaction.stage_directory(pages_dir)
        write_start = perf_counter()
        page_info = write_candidate_outputs(
            workspace,
            transaction,
            output_path,
            candidates,
            max_candidates,
            pages_dir,
            page_paths,
            [path for path in stale_pages if path not in page_paths],
        )
        performance = summary.setdefault("performance", {})
        timings = performance.setdefault("timings_seconds", {})
        timings["read_input"] = round(read_seconds, 4)
        timings["write_outputs"] = round(perf_counter() - write_start, 4)
        timings["total"] = round(perf_counter() - run_start, 4)
        report = {
            **scan_identity,
            "input": str(input_path.relative_to(workspace)),
            "output": str(output_path.relative_to(workspace)),
            "scan_config": scan_config,
            "pages": page_info,
            "summary": summary,
        }
        write_json(transaction.stage_path(write_paths["report"]), report)
        transaction.commit(
            {
                "2_ads": (
                    "candidates_ready",
                    {
                        "input": str(input_path.relative_to(workspace)),
                        "candidates": str(output_path.relative_to(workspace)),
                        "pages": page_info["pages_dir"],
                        "report": "report/ads_scan_report.json",
                        "candidate_count": summary["candidate_count"],
                        "total_candidate_count": summary["total_candidate_count"],
                        "page_count": summary["page_count"],
                        **scan_identity,
                    },
                )
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan preprocessed novel text for ad candidates.")
    parser.add_argument("workspace", help="Path to the .cleanwork directory.")
    parser.add_argument("--input", default="versions/v1_preprocessed.txt")
    parser.add_argument("--output", default="candidates/ads.jsonl")
    parser.add_argument("--min-chars", type=int, default=12)
    parser.add_argument("--max-candidates", type=int, default=300)
    parser.add_argument("--max-anchors", type=int, default=120)
    parser.add_argument(
        "--near-scan-scope",
        choices=("boundary", "all"),
        default="boundary",
        help="L2 near-repeat scope. boundary is the fast default; all scans every eligible paragraph.",
    )
    parser.add_argument(
        "--near-boundary-chars",
        type=int,
        default=320,
        help="Character radius around chapter boundaries for --near-scan-scope boundary.",
    )
    args = parser.parse_args()

    report = run(
        workspace=Path(args.workspace).resolve(),
        input_value=args.input,
        output_value=args.output,
        min_chars=args.min_chars,
        max_candidates=args.max_candidates,
        max_anchors=args.max_anchors,
        near_scan_scope=args.near_scan_scope,
        near_boundary_chars=args.near_boundary_chars,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
