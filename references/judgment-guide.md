# Judgment Guide

Use this reference when converting scanner candidates into decision JSONL.

## Contents

- [Global Rules](#global-rules)
- [Ads](#ads)
- [Verification](#verification)

## Global Rules

- The host Agent writes only structured artifacts that are not novel text, such as
  `meta/book_profile.json` and compact review JSONL; only scripts may modify novel text.
- Write complete local-Agent reviews to `decisions/ads_agent_reviews.jsonl`, then run `scripts/finalize_ad_decisions.py` to copy exact anchors into formal mutating decisions.
- Every formal mutating decision must contain exact anchors copied from the candidate; never provide alternate anchors in the compact Agent review file.
- `uncertain` and `keep` decisions must not include mutating actions.
- Every `uncertain` Agent review must contain explicit `blocking_reasons`.
- Do not infer missing text. If the candidate lacks enough context, mark it `uncertain`.

## Ads

Compact Agent review schema:

```json
{
  "scan_id": "<scan SHA-256>",
  "candidate_id": "AD-0001",
  "candidate_fingerprint": "<candidate SHA-256>",
  "verdict": "delete",
  "confidence": 0.97,
  "reason": "含外部域名和下载站水印，与剧情无关，多处重复出现",
  "splice_strategy": "remove_paragraph",
  "risk": "low"
}
```

Do not add anchors to this review. `finalize_ad_decisions.py` verifies the scan identity and
copies the complete saved anchor set into the formal decision.

Use `delete` only for non-story content:

- External URLs, domains, downloader markers, site watermarks.
- Repeated chapter-start or chapter-end boilerplate unrelated to story.
- Contact or group promotion unrelated to the novel world.
- A standalone site promotion whose occurrences consistently sit next to a high-confidence domain/email candidate.

Evidence propagation rules:

- Start only from original high-confidence URL, email, explicit download, or verifiable-domain seeds.
- Require two independent signals for promoted deletion, such as `known site + visit intent` or `same site + adjacent seed`.
- Normalize fullwidth text and obfuscated dots for comparison only; copy the untouched candidate anchors into the formal decision.
- Propagate one hop only. Similarity, repetition, `reader_site`, `watermark`, or copy-marker text alone must not trigger deletion.
- When a promotion and its following short domain form one ad unit, make a formal decision for every candidate in that unit so verification cannot leave an orphan line.

Use `keep` for literary or in-world repetition:

- Poetry, chants, slogans, character catchphrases, repeated dialogue.
- System-flow prompts, skill panels, item panels, or UI-like text that belongs to the story.
- Diegetic letters, screens, notices, paper inserts, URLs, or contact details whose exact content
  is required to understand a later plot action, inference, or return to the same evidence.
- Author notes that are part of a published chapter and do not contain external promotion.
- Ordinary narrative substrings such as `运转自身`, `圆转自如`, `精神扫描`, `整理衣服`, and `抬手打断`.

Narrative framing alone is not protection. A standalone promotion remains deletable when a
character merely notices, moves, discards, or calls it unrelated, and the later plot does not use
its exact content.

An ordinary `keep` may omit `keep_basis`. Add the structured basis below when a high-priority
residual has no other bound evidence, when a machine-delete draft conflicts with the review, or
when an exact audited explanation is useful:

```json
{
  "schema": "cml.keep-basis.v1",
  "type": "narrative_context",
  "reviewed_occurrences": [
    {"anchor_id": "A-...", "text_sha256": "<lowercase SHA-256>"}
  ],
  "occurrence_coverage_sha256": "<canonical occurrence coverage SHA-256>",
  "note": "已逐处确认这是人物打开网页的剧情动作，不是来源声明"
}
```

Allowed types are `narrative_context`, `plot_dependency`, and `rule_false_positive`.
`reviewed_occurrences` must cover every saved occurrence exactly once with its current text hash;
the candidate must be untruncated and its declared occurrence count must equal its anchor count.
The compiler normalizes ledger order and validates the canonical coverage hash. Missing, extra,
duplicate, stale, uppercase, truncated, or malformed coverage is rejected. `delete` and
`uncertain` must not carry a basis, and the compiler never infers one from `reason` or `note`.
Verification maps the original occurrences through the committed operation ledger and consumes
each final range and text hash once, so a basis cannot authorize another residual. If the
classification is unclear, use `uncertain` instead.

Any candidate overlap with protected terms from `book_profile.json` requires explicit
contextual review; overlap alone is not an automatic verdict. Check every saved occurrence.
Use `keep` when the overlap is narrative context, and use `uncertain` when its role remains
ambiguous or the context is insufficient. Never use the overlap alone as evidence for deletion.

Use `uncertain` for risky candidates:

- Ambiguous `作者的话`, `PS`, `求票`, or commentary without a strong external ad signal.
- Candidates where only one occurrence is shown and the context is insufficient.
- Any candidate marked `mutation_guard: "long_line_mixed_content"` unless the correct outcome
  is a clear `keep`. The formal compiler rejects whole-candidate deletion for this guard.

Recommended splice strategy:

- `remove_paragraph`: standalone ad line or paragraph.
- `exact`: delete exactly the saved candidate anchor without widening it.
- `fallback_newline`: uncertain line-break damage; prefer this only when deletion is confirmed but joining is unsafe.
- `exact_segment`: delete only the scanner-bound external-ad segment inside a mixed block. Use
  it only when the projection declares segment deletion allowed, and copy its current
  `edit_plan_id` into the review.

Scanner anchors identify complete text blocks; the Agent still cannot select a free-form
sub-span. For an authorized `exact_segment` review, do not add offsets, ranges, segments, parent
bindings, joiners, or replacement text. Compare the bounded keep/delete/after previews, then copy
only `edit_plan_id`. The formal compiler retrieves the current complete plan from the scanner
ledger; apply preflights the exact parent and commits atomically; verify replays the plan and proves
that retained narrative text is unchanged. If the boundary is wrong, keep or use `uncertain`
instead of attempting a hand-written repair.

Example mixed-content delete review:

```json
{
  "scan_id": "<scan SHA-256>",
  "candidate_id": "AD-0001",
  "candidate_fingerprint": "<candidate SHA-256>",
  "verdict": "delete",
  "confidence": 0.99,
  "reason": "保留预览是完整剧情，只删除扫描器锁定的外部推广后缀",
  "action": "delete",
  "splice_strategy": "exact_segment",
  "edit_plan_id": "EP-...",
  "risk": "high"
}
```

The instruction to never bypass a blocker forbids editing manifests, forging anchors, skipping
compiler checks, or treating an incomplete verification as passed. It does not forbid a
compiler-validated `keep` with complete occurrence coverage; that is the supported audited escape
path for a genuine rule false positive.

## Verification

After applying ad decisions, run `scripts/verify.py`. Treat any formal `uncertain`, deletion
above 8%, chapter identity change, missing operation, run or layout replay failure, current-run
anomaly, incomplete residual scan, or residual strong candidate as a delivery blocker.
