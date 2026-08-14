from __future__ import annotations

import copy
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import parse_structure  # noqa: E402
import scan_ads  # noqa: E402
import scan_identity  # noqa: E402


FormalAdsResult = tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]

_REVIEW_KEYS = {
    "action",
    "blocking_reasons",
    "confidence",
    "keep_basis",
    "reason",
    "risk",
    "splice_strategy",
    "verdict",
}


def _anchor_from_spec(
    text: str,
    locators: list[dict[str, Any]],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    original = spec.get("original")
    offset = spec.get("offset")
    if not isinstance(original, str) or not original:
        raise ValueError("candidate anchor original must be a non-empty string")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("candidate anchor offset must be a non-negative integer")

    end = spec.get("end", offset + len(original))
    line = spec.get("line", text.count("\n", 0, min(offset, len(text))) + 1)
    anchor: dict[str, Any] = {
        "offset": offset,
        "end": end,
        "line": line,
        "original": original,
        "prefix": spec.get("prefix", text[max(0, offset - 10) : offset]),
        "suffix": spec.get("suffix", text[end : min(len(text), end + 10)]),
    }

    if "chapter" in spec:
        anchor["chapter"] = copy.deepcopy(spec["chapter"])
    elif "locator" in spec:
        anchor["locator"] = copy.deepcopy(spec["locator"])
    elif offset < len(text):
        locator = scan_ads.chapter_lookup(locators, offset)
        if locator is not None:
            reference = {
                "index": locator.get("index"),
                "title": locator.get("title"),
            }
            if locator.get("kind") == "chapter":
                anchor["chapter"] = reference
            else:
                anchor["locator"] = {
                    "kind": locator.get("kind"),
                    **reference,
                }
    return anchor


def _candidate_from_spec(
    text: str,
    locators: list[dict[str, Any]],
    spec: Mapping[str, Any],
    index: int,
) -> dict[str, Any]:
    raw_anchors = spec.get("anchors")
    if raw_anchors is None:
        raw_anchors = [spec]
    if not isinstance(raw_anchors, list) or not all(
        isinstance(anchor, Mapping) for anchor in raw_anchors
    ):
        raise ValueError("candidate anchors must be a list of objects")

    anchors = [
        _anchor_from_spec(text, locators, anchor)
        for anchor in raw_anchors
    ]
    candidate_id = spec.get("candidate_id", f"AD-TEST-{index:04d}")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate_id must be a non-empty string")
    return {
        "candidate_id": candidate_id,
        "risk_hint": spec.get("risk_hint", "low"),
        "occurrence_count": len(anchors),
        "anchors_truncated": spec.get("anchors_truncated", False),
        "anchors": anchors,
    }


def _review_for_candidate(
    candidate: Mapping[str, Any],
    spec: Mapping[str, Any],
    scan_id: str,
    *,
    verdict: str,
    action: str | None,
) -> dict[str, Any]:
    review_override = spec.get("review", {})
    if not isinstance(review_override, Mapping):
        raise ValueError("candidate review override must be an object")
    unknown = sorted(set(review_override) - _REVIEW_KEYS)
    if unknown:
        raise ValueError(f"candidate review override has unknown fields: {unknown}")

    selected_verdict = review_override.get("verdict", verdict)
    review: dict[str, Any] = {
        "scan_id": scan_id,
        "candidate_id": candidate["candidate_id"],
        "candidate_fingerprint": candidate["candidate_fingerprint"],
        "verdict": selected_verdict,
        "confidence": 0.5 if selected_verdict == "uncertain" else 0.99,
        "reason": f"test fixture review: {selected_verdict}",
    }
    if selected_verdict == "delete":
        if action is not None:
            review["action"] = action
        review["splice_strategy"] = "exact"
    elif selected_verdict == "uncertain":
        review["blocking_reasons"] = ["test fixture requires review"]
    review.update(copy.deepcopy(dict(review_override)))
    return review


def _bound_drafts(
    candidates: list[dict[str, Any]],
    drafts: Sequence[Mapping[str, Any]],
    scan_id: str,
) -> list[dict[str, Any]]:
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(drafts):
        record = copy.deepcopy(dict(raw))
        candidate_id = record.get("candidate_id")
        if candidate_id is None and index < len(candidates):
            candidate_id = candidates[index]["candidate_id"]
            record["candidate_id"] = candidate_id
        candidate = by_id.get(candidate_id)
        if candidate is None:
            raise ValueError("draft does not match a formalized candidate")
        record["scan_id"] = scan_id
        record["candidate_fingerprint"] = candidate["candidate_fingerprint"]
        record.setdefault(
            "anchor_ids",
            [anchor["anchor_id"] for anchor in candidate["anchors"]],
        )
        record.setdefault("anchors_truncated", candidate["anchors_truncated"])
        result.append(record)
    return result


def formalize_ads(
    workspace: Path,
    candidate_specs: Sequence[Mapping[str, Any]],
    *,
    verdict: str = "keep",
    action: str | None = None,
    drafts: Sequence[Mapping[str, Any]] | None = None,
) -> FormalAdsResult:
    """Publish a synthetic scan through the real scan and formalization pipeline.

    Each candidate spec may use ``original`` and ``offset`` for one anchor, or
    provide an ``anchors`` list for a repeated candidate. A spec may also
    override the shared review through a ``review`` object.
    """

    workspace = Path(workspace).resolve()
    parse_structure.run(workspace)
    input_path = workspace / "versions" / "v1_preprocessed.txt"
    structure_path = workspace / "meta" / "chapters.json"
    text = input_path.read_text(encoding="utf-8")
    locators = scan_ads.load_chapters(input_path, structure_path)
    specs = [copy.deepcopy(dict(spec)) for spec in candidate_specs]
    candidates = [
        _candidate_from_spec(text, locators, spec, index)
        for index, spec in enumerate(specs, 1)
    ]
    page_size = max(1, len(candidates))
    summary: dict[str, Any] = {
        "candidate_count": len(candidates),
        "total_candidate_count": len(candidates),
        "page_count": 1 if candidates else 0,
        "page_size": page_size,
    }

    with mock.patch.object(
        scan_ads,
        "scan_candidates",
        return_value=(candidates, summary),
    ):
        scan_report = scan_ads.run(
            workspace,
            "versions/v1_preprocessed.txt",
            "candidates/ads.jsonl",
            12,
            page_size,
            max(1, sum(len(candidate["anchors"]) for candidate in candidates)),
        )

    bound_candidates = scan_identity.load_validated_pages(workspace, scan_report)
    scan_id = str(scan_report["scan_id"])
    reviews = [
        _review_for_candidate(
            candidate,
            spec,
            scan_id,
            verdict=verdict,
            action=action,
        )
        for candidate, spec in zip(bound_candidates, specs)
    ]
    common.write_jsonl(
        workspace / "decisions" / "ads_agent_reviews.jsonl",
        reviews,
    )

    if drafts is None:
        make_ad_decisions.run(
            workspace,
            "candidates/ads_pages",
            "decisions/ads_decisions.draft.jsonl",
            "meta/book_profile.json",
            True,
        )
    else:
        bound_drafts = _bound_drafts(bound_candidates, drafts, scan_id)
        with mock.patch.object(
            make_ad_decisions,
            "build_draft_decisions",
            return_value=(bound_drafts, {}),
        ):
            make_ad_decisions.run(
                workspace,
                "candidates/ads_pages",
                "decisions/ads_decisions.draft.jsonl",
                "meta/book_profile.json",
                True,
            )

    formal_report = finalize_ad_decisions.run(
        workspace,
        "candidates/ads_pages",
        "decisions/ads_agent_reviews.jsonl",
        "decisions/ads_decisions.draft.jsonl",
        "decisions/ads_decisions.jsonl",
    )
    return scan_report, bound_candidates, formal_report


def formalize_clean_ads(workspace: Path) -> FormalAdsResult:
    """Formalize a current zero-candidate ad scan."""

    return formalize_ads(workspace, [], verdict="keep")
