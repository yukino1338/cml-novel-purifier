from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from time import perf_counter
from typing import Any

from common import (
    WorkspaceTransaction,
    load_jsonl,
    resolve_in_workspace,
    resolve_workspace_paths,
    sha256_file,
    workspace_transaction_lock,
    write_json,
    write_jsonl,
)
from ad_rules import (
    EMAIL_RE,
    GENERIC_DOMAIN_RE,
    domain_tokens,
    family_template,
    fold_external_text,
    has_bound_visit_locator,
    is_narrative_external_reference,
    normalize_match_text,
    promotion_intents,
    signal_keys,
    site_entities,
)
import scan_identity
import ad_review_protocol
import ad_decision_policy
from book_profile import load_book_profile, protection_terms


FORMAL_DECISIONS_NAME = "ads_decisions.jsonl"
DRAFT_DECISIONS_NAME = "ads_decisions.draft.jsonl"
STRONG_EXTERNAL_SIGNALS = signal_keys("strong")
LOCATOR_SIGNALS = signal_keys("locator")
TERM_STOPWORDS = {
    "小说",
    "章节",
    "正文",
    "作者",
    "读者",
    "系统",
    "世界",
    "大陆",
    "学院",
}

NARRATIVE_WATERMARK_COLLISION_RE = re.compile(
    r"(?:运转自(?:身|己|如)|回转自(?:身|己)|圆转自如|扭转自己)"
)
FAMILY_SIMILARITY_THRESHOLD = 0.72


def unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def candidate_original_parts(candidate: dict[str, Any]) -> list[str]:
    """Return candidate-owned text only, excluding surrounding prose."""
    parts: list[str] = []
    sample = candidate.get("sample")
    if isinstance(sample, str):
        parts.append(sample)
    for anchor in candidate.get("anchors", []):
        if isinstance(anchor, dict):
            original = anchor.get("original")
            if isinstance(original, str):
                parts.append(original)
    return unique_strings(parts)


def feature_text(candidate: dict[str, Any]) -> str:
    parts = candidate_original_parts(candidate)
    if not parts:
        for context in candidate.get("contexts", []):
            if isinstance(context, dict):
                original = context.get("original")
                if isinstance(original, str):
                    parts.append(original)
    return "\n".join(unique_strings(parts))


def character_ngrams(value: str, size: int = 3) -> frozenset[str]:
    if len(value) <= size:
        return frozenset({value}) if value else frozenset()
    return frozenset(value[index : index + size] for index in range(len(value) - size + 1))


def ngram_similarity(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def profile_terms(profile: dict[str, Any]) -> list[str]:
    normalized: dict[str, str] = {}
    for term in protection_terms(profile):
        key = normalize_match_text(term)
        if len(key) < 2 or key in TERM_STOPWORDS:
            continue
        normalized.setdefault(key, term)
    return [normalized[key] for key in sorted(normalized)]


def candidate_text(candidate: dict[str, Any]) -> str:
    # Protection terms describe the book itself.  Surrounding context is useful
    # to an Agent, but including it here would make virtually every real ad next
    # to a protagonist name look protected.
    return "\n".join(candidate_original_parts(candidate))


def protected_hits(candidate: dict[str, Any], terms: list[str]) -> list[str]:
    haystack = normalize_match_text(candidate_text(candidate))
    hits: list[str] = []
    for term in terms:
        normalized = normalize_match_text(term)
        if normalized and normalized in haystack:
            hits.append(term)
    return hits


def has_anchors(candidate: dict[str, Any]) -> bool:
    anchors = candidate.get("anchors")
    return isinstance(anchors, list) and any(isinstance(anchor, dict) for anchor in anchors)


def candidate_signals(candidate: dict[str, Any]) -> set[str]:
    signals = candidate.get("signals")
    if not isinstance(signals, list):
        return set()
    return {str(signal) for signal in signals}


def inferred_external_signals(value: str) -> set[str]:
    signals: set[str] = set()
    folded = fold_external_text(value).lower()
    if GENERIC_DOMAIN_RE.search(folded):
        signals.add("domain")
    if EMAIL_RE.search(folded):
        signals.add("email")
    if site_entities(value):
        signals.add("reader_site")
    return signals


def immediate_neighbor_line(fragment: str, direction: str) -> tuple[str, int] | None:
    if direction == "after":
        if not fragment.startswith(("\n", "\r")):
            return None
        lines = fragment.splitlines()
        for index, line in enumerate(lines[1:], 1):
            if line.strip():
                return line.strip(), index
        return None

    if not fragment.endswith(("\n", "\r")):
        return None
    lines = fragment.splitlines()
    distance = 0
    for line in reversed(lines):
        if not line.strip():
            distance += 1
            continue
        return line.strip(), max(1, distance)
    return None


def sanitized_neighbor_span(span: dict[str, Any]) -> dict[str, Any] | None:
    original = span.get("original")
    if not isinstance(original, str) or not original.strip():
        return None
    result: dict[str, Any] = {"original": original.strip()}
    for key in (
        "source_offset",
        "source_line",
        "neighbor_offset",
        "neighbor_line",
        "direction",
        "line_distance",
        "signal_strength",
    ):
        value = span.get(key)
        if isinstance(value, (str, int, float, bool)):
            result[key] = value
    signals = span.get("signals")
    if isinstance(signals, list):
        result["signals"] = sorted({str(signal) for signal in signals})
    else:
        result["signals"] = sorted(inferred_external_signals(original))
    return result


def inferred_neighbor_spans(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    anchors = [anchor for anchor in candidate.get("anchors", []) if isinstance(anchor, dict)]
    contexts = [context for context in candidate.get("contexts", []) if isinstance(context, dict)]

    for index, anchor in enumerate(anchors):
        for direction, key in (("before", "prefix"), ("after", "suffix")):
            fragment = anchor.get(key)
            if not isinstance(fragment, str):
                continue
            neighbor = immediate_neighbor_line(fragment, direction)
            if not neighbor:
                continue
            original, line_distance = neighbor
            signals = inferred_external_signals(original)
            if not signals:
                continue
            span = {
                "source_offset": anchor.get("offset"),
                "source_line": anchor.get("line"),
                "direction": direction,
                "line_distance": line_distance,
                "original": original,
                "signals": sorted(signals),
                "inferred": True,
            }
            spans.append(span)

        # Old candidates have wider context only for a few representative
        # anchors.  Use it solely to enrich an already adjacent external line.
        if index >= len(contexts):
            continue
        context = contexts[index]
        for direction, key in (("before", "before"), ("after", "after")):
            fragment = context.get(key)
            if not isinstance(fragment, str):
                continue
            neighbor = immediate_neighbor_line(fragment, direction)
            if not neighbor:
                continue
            original, line_distance = neighbor
            signals = inferred_external_signals(original)
            if not signals:
                continue
            spans.append(
                {
                    "source_offset": anchor.get("offset"),
                    "source_line": anchor.get("line"),
                    "direction": direction,
                    "line_distance": line_distance,
                    "original": original,
                    "signals": sorted(signals),
                    "inferred": True,
                }
            )

    deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
    for span in spans:
        key = (
            span.get("source_offset"),
            span.get("direction"),
            normalize_match_text(str(span.get("original") or "")),
        )
        # Prefer the wider context over a ten-character anchor suffix.
        previous = deduplicated.get(key)
        if previous is None or len(str(span.get("original") or "")) > len(
            str(previous.get("original") or "")
        ):
            deduplicated[key] = span
    return list(deduplicated.values())


def candidate_neighbor_spans(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    supplied = candidate.get("neighbor_spans")
    if isinstance(supplied, list):
        spans = [
            sanitized
            for span in supplied
            if isinstance(span, dict)
            for sanitized in [sanitized_neighbor_span(span)]
            if sanitized is not None
        ]
        return spans
    return inferred_neighbor_spans(candidate)


def candidate_features(candidate: dict[str, Any]) -> dict[str, Any]:
    text = feature_text(candidate)
    normalized = normalize_match_text(text)
    signals = candidate_signals(candidate)
    core_sites = site_entities(text)
    intents = promotion_intents(text)
    domains = domain_tokens(text)
    neighbors = candidate_neighbor_spans(candidate)
    neighbor_sites: set[str] = set()
    neighbor_signals: set[str] = set()
    neighbor_intents: set[str] = set()
    for span in neighbors:
        original = str(span.get("original") or "")
        neighbor_sites.update(site_entities(original))
        neighbor_signals.update(str(signal) for signal in span.get("signals", []))
        neighbor_signals.update(inferred_external_signals(original))
        neighbor_intents.update(promotion_intents(original))

    all_sites = core_sites | neighbor_sites
    all_intents = intents | neighbor_intents
    template = family_template(str(candidate.get("sample") or text))
    signature = {
        "site_entities": sorted(all_sites),
        "intents": sorted(all_intents),
        "signals": sorted(signals),
        "template": template,
    }
    return {
        "text": text,
        "normalized": normalized,
        "signals": signals,
        "core_sites": core_sites,
        "neighbor_sites": neighbor_sites,
        "all_sites": all_sites,
        "intents": intents,
        "neighbor_intents": neighbor_intents,
        "all_intents": all_intents,
        "domains": domains,
        "neighbors": neighbors,
        "neighbor_signals": neighbor_signals,
        "template": template,
        "ngrams": character_ngrams(template),
        "signature": signature,
    }


def cluster_key(features: dict[str, Any]) -> str:
    sites = sorted(features["all_sites"])
    intents = sorted(features["all_intents"])
    signals = sorted(features["signals"])
    if sites:
        return "site=" + ",".join(sites) + "|intent=" + (intents[0] if intents else "source")
    signal_group = ",".join(signals) if signals else "repetition"
    return "signal=" + signal_group + "|template=" + features["template"]


def cluster_id_for(features: dict[str, Any]) -> str:
    digest = hashlib.sha1(cluster_key(features).encode("utf-8")).hexdigest()[:12]
    return f"ADF-{digest}"


def feature_evidence(features: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    direct = sorted(features["signals"] & LOCATOR_SIGNALS)
    if direct:
        evidence.append({"type": "direct_signal", "value": direct})
    if features["domains"]:
        evidence.append({"type": "verified_domain", "value": sorted(features["domains"])})
    if features["core_sites"]:
        evidence.append({"type": "site_entity", "value": sorted(features["core_sites"])})
    if features["intents"]:
        evidence.append({"type": "promotion_intent", "value": sorted(features["intents"])})
    if features["neighbor_sites"] or features["neighbor_signals"]:
        evidence.append(
            {
                "type": "adjacent_external",
                "value": {
                    "sites": sorted(features["neighbor_sites"]),
                    "signals": sorted(features["neighbor_signals"]),
                    "span_count": len(features["neighbors"]),
                },
            }
        )
    if features["neighbor_intents"]:
        evidence.append(
            {"type": "adjacent_promotion_intent", "value": sorted(features["neighbor_intents"])}
        )
    return evidence


def copied_anchors(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    anchors = candidate.get("anchors")
    if not isinstance(anchors, list):
        return []
    return [copy.deepcopy(anchor) for anchor in anchors if isinstance(anchor, dict)]


def suggested_splice_strategy(candidate: dict[str, Any]) -> str:
    suggested = candidate.get("suggested_decision")
    if isinstance(suggested, dict):
        strategy = suggested.get("splice_strategy")
        if isinstance(strategy, str) and strategy:
            return strategy
    return "remove_paragraph"


def draft_for_candidate(candidate: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    candidate_id = str(candidate.get("candidate_id") or "")
    signals = candidate_signals(candidate)
    hits = protected_hits(candidate, terms)
    anchors = copied_anchors(candidate)
    base = {
        "candidate_id": candidate_id,
        "candidate_fingerprint": candidate.get("candidate_fingerprint"),
        "anchor_ids": [anchor.get("anchor_id") for anchor in anchors],
        "anchors_truncated": candidate.get("anchors_truncated", False),
        "draft": True,
        "confidence": None,
        "reason": "",
        "splice_strategy": suggested_splice_strategy(candidate),
        "risk": candidate.get("risk_hint", "medium"),
        "anchors": anchors,
        "source_candidate": {
            "layer": candidate.get("layer"),
            "detector": candidate.get("detector"),
            "signals": sorted(signals),
            "signal_strength": candidate.get("signal_strength"),
            "occurrence_count": candidate.get("occurrence_count"),
            "quota_bucket": candidate.get("quota_bucket"),
        },
    }
    mutation_guard = candidate.get("mutation_guard")
    if isinstance(mutation_guard, str) and mutation_guard:
        base["mutation_guard"] = mutation_guard

    if not anchors:
        base.update(
            {
                "verdict": "uncertain",
                "confidence": 0.2,
                "reason": "missing anchors; mutating decision would not be executable",
                "review_required": True,
            }
        )
        return base

    if candidate.get("edit_plan") is not None:
        plan = ad_decision_policy.normalize_edit_plan(candidate["edit_plan"], candidate)
        base.update(
            {
                "verdict": "uncertain",
                "confidence": 0.5,
                "reason": "scanner isolated an external promotion segment; review only the bound edit plan",
                "splice_strategy": "exact_segment",
                "edit_plan_id": plan["edit_plan_id"],
                "review_required": True,
            }
        )
        return base

    if mutation_guard == "long_line_mixed_content":
        base.update(
            {
                "verdict": "uncertain",
                "confidence": 0.15,
                "reason": "strong external signal occurs inside a line over 500 characters; "
                "whole-line mutation is forbidden",
                "blocking_reasons": ["long_line_mixed_content"],
                "review_required": True,
            }
        )
        return base

    if candidate.get("anchors_truncated") is True:
        base.update(
            {
                "verdict": "uncertain",
                "confidence": 0.2,
                "reason": "anchors truncated; rescan with a higher --max-anchors before deletion",
                "blocking_reasons": ["anchors_truncated"],
                "review_required": True,
            }
        )
        return base

    if hits:
        base.update(
            {
                "verdict": "uncertain",
                "confidence": 0.3,
                "reason": "candidate overlaps protected book_profile terms: " + ", ".join(hits[:8]),
                "protected_terms": hits,
                "review_required": True,
            }
        )
        return base

    if candidate.get("signal_strength") == "strong" and signals & STRONG_EXTERNAL_SIGNALS:
        base.update(
            {
                "verdict": "uncertain",
                "confidence": 0.5,
                "reason": "strong but context-sensitive signal requires review: "
                + ", ".join(sorted(signals & STRONG_EXTERNAL_SIGNALS)),
                "review_required": True,
            }
        )
        return base

    base.update(
        {
            "verdict": "uncertain",
            "confidence": 0.45,
            "reason": "no strong external ad signal; repeated or weak candidate requires review",
            "review_required": True,
        }
    )
    return base


def decision_is_safety_blocked(decision: dict[str, Any]) -> bool:
    return (
        bool(decision.get("protected_terms"))
        or bool(decision.get("anchors_truncated"))
        or bool(decision.get("mutation_guard"))
        or not bool(decision.get("anchors"))
    )


def set_delete_decision(
    decision: dict[str, Any],
    *,
    confidence: float,
    reason: str,
    promoted_from: list[str] | None = None,
) -> None:
    decision.update(
        {
            "verdict": "delete",
            "confidence": confidence,
            "reason": reason,
            "risk": "low" if confidence >= 0.95 else "medium",
            "review_required": False,
            "promoted_from": list(promoted_from or []),
        }
    )


def is_narrative_watermark_collision(
    candidate: dict[str, Any], features: dict[str, Any]
) -> bool:
    if features["signals"] != {"watermark"}:
        return False
    if features["all_sites"] or features["domains"] or features["intents"]:
        return False
    originals = candidate_original_parts(candidate)
    return bool(originals) and all(NARRATIVE_WATERMARK_COLLISION_RE.search(text) for text in originals)


def neighbor_source_coverage(candidate: dict[str, Any], features: dict[str, Any]) -> float:
    anchors = [anchor for anchor in candidate.get("anchors", []) if isinstance(anchor, dict)]
    if not anchors:
        return 0.0
    source_offsets = {
        span.get("source_offset")
        for span in features["neighbors"]
        if span.get("source_offset") is not None
        and (site_entities(str(span.get("original") or "")) or span.get("signals"))
    }
    if source_offsets:
        return min(1.0, len(source_offsets) / len(anchors))
    # Externally supplied spans may omit source offsets in custom integrations.
    return min(1.0, len(features["neighbors"]) / len(anchors))


def automatic_delete_evidence(
    candidate: dict[str, Any],
    features: dict[str, Any],
) -> tuple[float, str, float] | None:
    if candidate.get("mutation_guard") == "long_line_mixed_content":
        return None
    if is_narrative_external_reference(features["text"]):
        return None
    core_locator = bool(
        features["signals"] & LOCATOR_SIGNALS
        or features["domains"]
        or features["core_sites"]
    )
    neighbor_locator = bool(
        features["neighbor_sites"]
        or features["neighbor_signals"] & LOCATOR_SIGNALS
    )
    core_intent = bool(features["intents"])
    neighbor_intent = bool(features["neighbor_intents"])
    if not (core_locator or neighbor_locator) or not (core_intent or neighbor_intent):
        return None
    if (
        core_locator
        and features["intents"] == {"visit"}
        and not has_bound_visit_locator(features["text"])
    ):
        return None
    coverage = neighbor_source_coverage(candidate, features)
    if not (core_locator and core_intent) and coverage < 0.5:
        return None
    if core_locator and core_intent:
        return 0.97, "external locator plus explicit promotion intent", coverage
    return 0.95, "adjacent external locator and promotion intent", coverage


def family_summaries(
    candidates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    seed_ids: set[str],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for candidate, decision in zip(candidates, decisions):
        grouped[str(decision["cluster_id"])].append((candidate, decision))

    summaries: list[dict[str, Any]] = []
    for cluster_id in sorted(grouped):
        members = grouped[cluster_id]
        member_ids = [str(candidate.get("candidate_id") or "") for candidate, _ in members]
        seeds = [candidate_id for candidate_id in member_ids if candidate_id in seed_ids]
        promoted = [
            str(candidate.get("candidate_id") or "")
            for candidate, decision in members
            if decision.get("promoted_from")
        ]
        blocked = [
            str(candidate.get("candidate_id") or "")
            for candidate, decision in members
            if decision_is_safety_blocked(decision)
        ]
        summaries.append(
            {
                "cluster_id": cluster_id,
                "representative_candidate_id": member_ids[0],
                "member_count": len(members),
                "seed_candidate_ids": seeds,
                "promoted_candidate_ids": promoted,
                "safety_blocked_candidate_ids": blocked,
                "signature": copy.deepcopy(members[0][1]["family_signature"]),
            }
        )
    return summaries


def build_draft_decisions(
    candidates: list[dict[str, Any]], terms: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    started = perf_counter()
    decisions = [draft_for_candidate(candidate, terms) for candidate in candidates]
    features_by_id: dict[str, dict[str, Any]] = {}

    for candidate, decision in zip(candidates, decisions):
        candidate_id = str(candidate.get("candidate_id") or "")
        features = candidate_features(candidate)
        features_by_id[candidate_id] = features
        decision.update(
            {
                "cluster_id": cluster_id_for(features),
                "family_signature": copy.deepcopy(features["signature"]),
                "evidence": feature_evidence(features),
                "promoted_from": [],
                # Evidence only.  These are not copied into decision anchors and
                # therefore cannot cause overlapping removals.
                "neighbor_span": copy.deepcopy(features["neighbors"]),
            }
        )

    narrative_keep_count = 0
    narrative_external_guard_count = 0
    for candidate, decision in zip(candidates, decisions):
        if decision.get("verdict") != "uncertain" or decision_is_safety_blocked(decision):
            continue
        candidate_id = str(candidate.get("candidate_id") or "")
        features = features_by_id[candidate_id]
        if is_narrative_external_reference(features["text"]):
            decision.update(
                {
                    "confidence": 0.25,
                    "reason": "external marker appears inside narrative evidence or a negated reference",
                    "review_required": True,
                }
            )
            decision["evidence"].append(
                {"type": "false_positive_guard", "value": "narrative_external_reference"}
            )
            narrative_external_guard_count += 1
            continue
        if is_narrative_watermark_collision(candidate, features):
            decision.update(
                {
                    "verdict": "keep",
                    "confidence": 0.98,
                    "reason": "watermark substring occurs only inside ordinary narrative wording",
                    "risk": "low",
                    "review_required": False,
                }
            )
            decision["evidence"].append(
                {"type": "false_positive_guard", "value": "narrative_watermark_substring"}
            )
            narrative_keep_count += 1

    rule_upgrade_count = 0
    site_intent_upgrade_count = 0
    neighbor_upgrade_count = 0
    for candidate, decision in zip(candidates, decisions):
        if decision.get("verdict") != "uncertain" or decision_is_safety_blocked(decision):
            continue
        candidate_id = str(candidate.get("candidate_id") or "")
        features = features_by_id[candidate_id]
        evidence = automatic_delete_evidence(candidate, features)
        if evidence is None:
            continue
        confidence, reason, coverage = evidence
        set_delete_decision(decision, confidence=confidence, reason=reason)
        decision["evidence"].append(
            {
                "type": "automatic_delete_gate",
                "value": {
                    "locator": True,
                    "promotion_intent": True,
                    "neighbor_coverage": round(coverage, 4),
                },
            }
        )
        rule_upgrade_count += 1
        if features["core_sites"] and features["intents"]:
            site_intent_upgrade_count += 1
        if not (features["domains"] or features["core_sites"]):
            neighbor_upgrade_count += 1

    # Seeds are frozen once, before family similarity.  A promoted member can
    # never seed another member, so propagation remains one hop.
    seed_ids = {
        str(candidate.get("candidate_id") or "")
        for candidate, decision in zip(candidates, decisions)
        if decision.get("verdict") == "delete" and not decision_is_safety_blocked(decision)
    }

    seed_buckets: dict[str, list[str]] = defaultdict(list)
    for seed_id in sorted(seed_ids):
        for site in features_by_id[seed_id]["all_sites"]:
            seed_buckets[site].append(seed_id)

    comparison_count = 0
    family_upgrade_count = 0
    for candidate, decision in zip(candidates, decisions):
        if decision.get("verdict") != "uncertain" or decision_is_safety_blocked(decision):
            continue
        candidate_id = str(candidate.get("candidate_id") or "")
        features = features_by_id[candidate_id]
        # Similarity is only corroboration.  Independent site and promotion
        # evidence are mandatory even when a template is nearly identical.
        if not features["all_sites"] or not features["all_intents"]:
            continue
        compatible_seeds = sorted(
            {
                seed_id
                for site in features["all_sites"]
                for seed_id in seed_buckets.get(site, [])
                if seed_id != candidate_id
            }
        )
        best_seed = ""
        best_similarity = 0.0
        for seed_id in compatible_seeds:
            comparison_count += 1
            similarity = ngram_similarity(features["ngrams"], features_by_id[seed_id]["ngrams"])
            if similarity > best_similarity:
                best_similarity = similarity
                best_seed = seed_id
        if best_seed and best_similarity >= FAMILY_SIMILARITY_THRESHOLD:
            set_delete_decision(
                decision,
                confidence=0.94,
                reason=f"one-hop match to original external seed {best_seed} ({best_similarity:.3f})",
                promoted_from=[best_seed],
            )
            decision["evidence"].append(
                {
                    "type": "family_similarity",
                    "value": {"seed": best_seed, "score": round(best_similarity, 4)},
                }
            )
            family_upgrade_count += 1

    summaries = family_summaries(candidates, decisions, seed_ids)
    protected_block_count = sum(1 for decision in decisions if decision.get("protected_terms"))
    anchor_block_count = sum(1 for decision in decisions if not decision.get("anchors"))
    truncated_anchor_block_count = sum(
        1 for decision in decisions if decision.get("anchors_truncated") is True
    )
    metrics = {
        "elapsed_seconds": round(perf_counter() - started, 6),
        "family_count": len(summaries),
        "comparison_count": comparison_count,
        "original_seed_count": len(seed_ids),
        "rule_upgrade_count": rule_upgrade_count,
        "site_intent_upgrade_count": site_intent_upgrade_count,
        "neighbor_upgrade_count": neighbor_upgrade_count,
        "family_upgrade_count": family_upgrade_count,
        "upgraded_count": rule_upgrade_count + family_upgrade_count,
        "narrative_keep_count": narrative_keep_count,
        "narrative_external_guard_count": narrative_external_guard_count,
        "protected_block_count": protected_block_count,
        "anchor_block_count": anchor_block_count,
        "truncated_anchor_block_count": truncated_anchor_block_count,
        "safety_block_count": (
            protected_block_count + anchor_block_count + truncated_anchor_block_count
        ),
        "neighbor_evidence_candidate_count": sum(
            1 for features in features_by_id.values() if features["neighbors"]
        ),
        "families": summaries,
    }
    return decisions, metrics


def candidate_input_paths(workspace: Path, input_value: str, all_pages: bool) -> list[Path]:
    workspace, reads, _ = resolve_workspace_paths(
        workspace,
        reads={"candidate_input": "candidates/ads_pages" if all_pages else input_value},
    )
    input_path = reads["candidate_input"]

    if input_path.is_dir():
        paths = [
            resolve_in_workspace(
                workspace,
                path.relative_to(workspace).as_posix(),
                role="read",
            )
            for path in sorted(input_path.glob("*.jsonl"))
        ]
    else:
        paths = [input_path]
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError("candidate input not found: " + str(missing[0]))
    return paths


def load_candidates(paths: list[Path]) -> tuple[list[dict[str, Any]], int]:
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_fingerprints: set[str] = set()
    for path in paths:
        for candidate in load_jsonl(path):
            candidate_id = candidate.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError("candidate record has an invalid candidate_id")
            if candidate_id in seen_ids:
                raise ValueError(f"duplicate candidate_id: {candidate_id}")
            fingerprint = candidate.get("candidate_fingerprint")
            if fingerprint is not None:
                if not isinstance(fingerprint, str) or fingerprint in seen_fingerprints:
                    raise ValueError("candidate fingerprint is invalid or duplicated")
                seen_fingerprints.add(fingerprint)
            seen_ids.add(candidate_id)
            candidates.append(candidate)
    return candidates, 0


def read_ads_scan_report(workspace: Path) -> dict[str, Any]:
    workspace, reads, _ = resolve_workspace_paths(
        workspace,
        reads={"scan_report": "report/ads_scan_report.json"},
    )
    path = reads["scan_report"]
    if not path.is_file():
        raise FileNotFoundError(f"ad scan report not found: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("ad scan report is not valid UTF-8 JSON") from error
    if not isinstance(report, dict) or report.get("scanner") != "ads":
        raise ValueError("ad scan report identity is invalid")
    return report


def _declared_page_paths(workspace: Path, report: dict[str, Any]) -> list[Path]:
    pages = report.get("pages")
    manifest = pages.get("manifest") if isinstance(pages, dict) else None
    if not isinstance(manifest, list):
        raise scan_identity.ScanIdentityError("page manifest is missing or invalid")
    paths: list[Path] = []
    for entry in manifest:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise scan_identity.ScanIdentityError("page manifest entry is invalid")
        paths.append(
            resolve_in_workspace(workspace, entry["file"], role="read")
        )
    return paths


def load_current_ad_candidates(
    workspace: Path,
    input_value: str,
    *,
    all_pages: bool,
    require_complete: bool = False,
    allow_pending_scan: bool = False,
) -> tuple[list[dict[str, Any]], list[Path], dict[str, Any]]:
    workspace, _, _ = resolve_workspace_paths(workspace)
    report = read_ads_scan_report(workspace)
    complete = scan_identity.load_validated_pages(
        workspace,
        report,
        allow_pending=allow_pending_scan,
    )
    page_paths = _declared_page_paths(workspace, report)
    pages = report["pages"]
    declared_values = {
        str(pages.get("pages_dir") or "").replace("\\", "/"),
        str(pages.get("first_page") or "").replace("\\", "/"),
    }
    if input_value.replace("\\", "/") not in declared_values:
        raise ValueError("candidate input must match the current scan report")

    if all_pages or require_complete:
        return complete, page_paths, report

    paths = candidate_input_paths(workspace, input_value, all_pages=False)
    selected, duplicate_count = load_candidates(paths)
    if duplicate_count:
        raise ValueError("candidate input contains duplicates")
    scan_identity.validate_anchor_ids(selected)
    selected_fingerprints = [item["candidate_fingerprint"] for item in selected]
    complete_prefix = [
        item["candidate_fingerprint"] for item in complete[: len(selected)]
    ]
    if selected_fingerprints != complete_prefix:
        raise scan_identity.ScanIdentityError(
            "first-page candidates do not match the current scan"
        )
    return selected, paths, report


def bind_draft_identity(
    candidates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    scan_id: str,
    *,
    scan_rule_pack_sha256: str | None = None,
    draft_rule_pack_sha256: str | None = None,
    profile_identity: dict[str, Any] | None = None,
) -> None:
    if len(candidates) != len(decisions):
        raise ValueError("draft decision count does not match candidate count")
    if scan_rule_pack_sha256 is None:
        scan_rule_pack_sha256 = scan_identity.canonical_json_sha256(
            scan_identity.build_scan_rule_pack("ads")
        )
    if draft_rule_pack_sha256 is None:
        draft_rule_pack_sha256 = scan_identity.canonical_json_sha256(
            scan_identity.build_draft_rule_pack()
        )
    if profile_identity is None:
        profile_identity = {
            "profile_present": False,
            "book_profile_sha256": scan_identity.canonical_json_sha256({}),
            "book_profile_file_sha256": None,
        }
    for candidate, decision in zip(candidates, decisions):
        if decision.get("candidate_id") != candidate.get("candidate_id"):
            raise ValueError("draft decision order does not match candidate order")
        decision["scan_id"] = scan_id
        decision["scan_rule_pack_sha256"] = scan_rule_pack_sha256
        decision["draft_rule_pack_sha256"] = draft_rule_pack_sha256
        decision["book_profile_sha256"] = profile_identity["book_profile_sha256"]
        decision["book_profile_file_sha256"] = profile_identity[
            "book_profile_file_sha256"
        ]
        decision["profile_present"] = profile_identity["profile_present"]
        decision["candidate_fingerprint"] = candidate["candidate_fingerprint"]
        decision["anchor_ids"] = [
            anchor["anchor_id"] for anchor in candidate["anchors"]
        ]
        decision["anchors_truncated"] = candidate["anchors_truncated"]


def count_by_verdict(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        verdict = str(decision.get("verdict") or "unknown")
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def assert_draft_output(output_path: Path) -> None:
    if output_path.name == FORMAL_DECISIONS_NAME and output_path.parent.name == "decisions":
        raise ValueError(
            "refusing to write formal ads_decisions.jsonl; write a draft and promote it after review"
        )


def run(
    workspace: Path,
    input_value: str,
    output_value: str,
    profile_value: str,
    all_pages: bool,
) -> dict[str, Any]:
    if not isinstance(all_pages, bool):
        raise ValueError("all_pages must be boolean")
    with workspace_transaction_lock(workspace):
        return _run_locked(
            workspace,
            input_value,
            output_value,
            profile_value,
            all_pages,
        )


def _run_locked(
    workspace: Path,
    input_value: str,
    output_value: str,
    profile_value: str,
    all_pages: bool,
) -> dict[str, Any]:
    if not isinstance(all_pages, bool):
        raise ValueError("all_pages must be boolean")
    workspace, _, _ = resolve_workspace_paths(workspace)
    candidates, input_paths, scan_report = load_current_ad_candidates(
        workspace,
        input_value,
        all_pages=True,
        require_complete=True,
    )
    candidate_base = "candidates/ads_pages"
    candidate_reads = {
        f"candidate_page_{index}": path.relative_to(workspace).as_posix()
        for index, path in enumerate(input_paths, 1)
    }
    workspace, reads, writes = resolve_workspace_paths(
        workspace,
        reads={
            "candidate_input": candidate_base,
            **candidate_reads,
            "source_text": str(scan_report.get("input") or "versions/v1_preprocessed.txt"),
            "profile": profile_value,
            "formal_decisions": f"decisions/{FORMAL_DECISIONS_NAME}",
        },
        writes={
            "output": output_value,
            "report": "report/ad_decision_draft_report.json",
        },
    )
    input_paths = [reads[name] for name in candidate_reads]
    output_path = writes["output"]
    assert_draft_output(output_path)

    duplicate_count = 0
    profile_path = reads["profile"]
    profile = load_book_profile(profile_path)
    draft_rule_pack = scan_identity.build_draft_rule_pack()
    draft_rule_pack_sha256 = scan_identity.canonical_json_sha256(draft_rule_pack)
    profile_identity = scan_identity.build_profile_identity(profile_path)
    terms = profile_terms(profile)
    decisions, secondary_review = build_draft_decisions(candidates, terms)
    bind_draft_identity(
        candidates,
        decisions,
        scan_report["scan_id"],
        scan_rule_pack_sha256=scan_report["scan_rule_pack_sha256"],
        draft_rule_pack_sha256=draft_rule_pack_sha256,
        profile_identity=profile_identity,
    )
    formal_path = reads["formal_decisions"]
    source_text = reads["source_text"].read_text(encoding="utf-8")
    previous_formal = load_jsonl(formal_path) if formal_path.is_file() else []
    review_protocol_identity = scan_identity.build_review_protocol_identity(
        target_page_bytes=ad_review_protocol.TARGET_PAGE_BYTES,
        hard_page_bytes=ad_review_protocol.HARD_PAGE_BYTES,
    )
    review_protocol_identity_sha256 = scan_identity.canonical_json_sha256(
        review_protocol_identity
    )
    review_projection = ad_review_protocol.build_review_projection(
        candidates,
        decisions,
        source_text,
        {
            "scan_id": scan_report["scan_id"],
            "candidate_set_sha256": scan_report["candidate_set_sha256"],
            "scan_rule_pack_sha256": scan_report["scan_rule_pack_sha256"],
            "draft_rule_pack_sha256": draft_rule_pack_sha256,
            **profile_identity,
            "review_protocol_identity": review_protocol_identity,
            "review_protocol_identity_sha256": review_protocol_identity_sha256,
        },
        previous_formal=previous_formal,
    )
    review_artifacts = ad_review_protocol.projection_artifacts(review_projection)
    review_manifest = review_projection["manifest"]
    review_manifest_sha256 = hashlib.sha256(
        review_artifacts["candidates/ads_review_pages/manifest.json"]
    ).hexdigest()
    report = {
        "scan_id": scan_report["scan_id"],
        "scan_rule_pack_sha256": scan_report["scan_rule_pack_sha256"],
        "candidate_set_sha256": scan_report["candidate_set_sha256"],
        "draft_rule_pack": draft_rule_pack,
        "draft_rule_pack_sha256": draft_rule_pack_sha256,
        **profile_identity,
        "inputs": [str(path.relative_to(workspace)) for path in input_paths],
        "output": str(output_path.relative_to(workspace)),
        "profile": profile_path.relative_to(workspace).as_posix(),
        "formal_decisions_exists": formal_path.exists(),
        "candidate_count": len(candidates),
        "duplicate_candidate_count": duplicate_count,
        "decision_count": len(decisions),
        "by_verdict": count_by_verdict(decisions),
        "delete_count": sum(1 for decision in decisions if decision.get("verdict") == "delete"),
        "keep_count": sum(1 for decision in decisions if decision.get("verdict") == "keep"),
        "uncertain_count": sum(1 for decision in decisions if decision.get("verdict") == "uncertain"),
        "protected_term_count": len(terms),
        "protected_downgrade_count": sum(1 for decision in decisions if decision.get("protected_terms")),
        "secondary_review": secondary_review,
        "review_pages_manifest": "candidates/ads_review_pages/manifest.json",
        "review_pages_manifest_sha256": review_manifest_sha256,
        "review_protocol_identity": review_protocol_identity,
        "review_protocol_identity_sha256": review_protocol_identity_sha256,
        "review_page_count": len(review_projection["pages"]),
        "review_projection_bytes": sum(
            entry["bytes"] for entry in review_manifest["pages"]
        ),
        "review_record_oversize_count": review_manifest[
            "review_record_oversize_count"
        ],
    }
    with WorkspaceTransaction(workspace) as transaction:
        staged_output = transaction.stage_path(output_path)
        write_jsonl(staged_output, decisions)
        draft_sha256 = sha256_file(staged_output)
        report["draft_sha256"] = draft_sha256
        review_pages_dir = workspace / "candidates" / "ads_review_pages"
        declared_review_paths = {
            (workspace / relative).resolve() for relative in review_artifacts
        }
        if review_pages_dir.is_dir():
            for stale_path in review_pages_dir.glob("page_*.json"):
                if stale_path.resolve() not in declared_review_paths:
                    transaction.stage_delete(stale_path)
        for relative, encoded in review_artifacts.items():
            transaction.stage_path(workspace / relative).write_bytes(encoded)
        staged_report = transaction.stage_path(writes["report"])
        write_json(staged_report, report)
        draft_report_sha256 = sha256_file(staged_report)
        transaction.commit(
            {
                "2_ads": (
                    "draft_decisions_ready",
                    {
                        "draft_decisions": str(output_path.relative_to(workspace)),
                        "draft_report": "report/ad_decision_draft_report.json",
                        "draft_run_id": transaction.run_id,
                        "draft_decisions_sha256": draft_sha256,
                        "draft_report_sha256": draft_report_sha256,
                        "draft_delete_count": report["delete_count"],
                        "draft_keep_count": report["keep_count"],
                        "draft_uncertain_count": report["uncertain_count"],
                        "scan_id": scan_report["scan_id"],
                        "scan_rule_pack_sha256": scan_report["scan_rule_pack_sha256"],
                        "candidate_set_sha256": scan_report["candidate_set_sha256"],
                        "draft_rule_pack": draft_rule_pack,
                        "draft_rule_pack_sha256": draft_rule_pack_sha256,
                        "review_pages_manifest": "candidates/ads_review_pages/manifest.json",
                        "review_pages_manifest_sha256": review_manifest_sha256,
                        "review_protocol_identity": review_protocol_identity,
                        "review_protocol_identity_sha256": review_protocol_identity_sha256,
                        "review_page_count": len(review_projection["pages"]),
                        "review_projection_bytes": report["review_projection_bytes"],
                        "review_record_oversize_count": report[
                            "review_record_oversize_count"
                        ],
                        **profile_identity,
                    },
                )
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate draft ad decisions from ad candidates.")
    parser.add_argument("workspace", help="Path to the .cleanwork directory.")
    parser.add_argument("--input", default="candidates/ads_pages")
    parser.add_argument("--output", default=f"decisions/{DRAFT_DECISIONS_NAME}")
    parser.add_argument("--profile", default="meta/book_profile.json")
    args = parser.parse_args()

    report = run(
        workspace=Path(args.workspace).resolve(),
        input_value=args.input,
        output_value=args.output,
        profile_value=args.profile,
        all_pages=True,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
