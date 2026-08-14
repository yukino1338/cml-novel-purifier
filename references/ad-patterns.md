# Ad Evidence Guide

Use this reference for stage 2 candidate review. The scanner intentionally favors recall; a candidate is not permission to edit text.

## Evidence boundary

- Automatic delete drafts require both an external locator and explicit promotion intent. A URL, email address, site name, download word, watermark phrase, repetition, or text similarity alone is insufficient.
- External locators include a domain, email address, contact destination, or canonical site entity. Promotion intent must explicitly ask the reader to visit, read, download, contact, or obtain content.
- Adjacent promotion and locator lines may form one evidence unit only when separated by at most one blank line and no story text. Keep their exact anchors separate.
- Family propagation is one hop from the original high-confidence seed. A promoted member never becomes a seed.
- Profile protection, missing anchors, truncated anchors, narrative context, or conflicting evidence always prevents an automatic delete draft.

## Normalization

- Normalize NFKC characters, whitespace, and dot obfuscation only for matching. Never rewrite the exact anchor text.
- The executable signal, site, intent, and label facts live only in `scripts/ad_rules.py`; do not copy their lists into instructions or review code.
- Public examples use reserved domains, such as `https://reader.example.com/update` and `notice@example.com`.

## Source-marker grammar

- `SOURCE_MARKER_RE` in `scripts/ad_rules.py` is the only executable definition of source evidence. It accepts an explicit production statement tied to a text object (`本书由…校对`), an explicit attribution ending in a concrete origin (`转自…网站`), or a bounded compound such as `手打版` / `校对组`.
- Source clauses are short and never bridge sentence punctuation. Ordinary words such as `网站`, `书库`, or `文本` do not turn a nearby bare verb into source evidence.
- `BARE_COPY_MARKER_RE` is diagnostic only. Hits such as `整理衣服`, `录入成绩`, `扫描庭院`, and `手打脚踢` may increment the suppressed-marker metric, but must not emit `watermark` or `copy_marker` signals.
- Do not widen the grammar with a reverse `裸动词…网站/文本` branch. Add every future source form with paired positive and narrative-negative regression cases.
- Scan provenance binds a sorted implementation pack of the scanner and every executable rule dependency. The pack uses repository-relative POSIX paths and raw file SHA-256 values; absolute paths and mtimes are excluded. Because this is an implementation identity, even a comment-only source change conservatively makes old scans stale.

## Hard negatives

- Ordinary prose can legitimately mention scanning, organizing, authors, websites, downloads, URLs, email addresses, or contact logs.
- Quoted notices, investigation clues, archived messages, old addresses, local device downloads, and negated invitations are narrative evidence, not automatic deletion targets.
- Author notes, repeated poems, system panels, chants, catchphrases, and deliberate refrains remain text unless independent external-promotion evidence proves otherwise.
- A protected character, place, faction, or story term in candidate-owned text requires review. Surrounding context alone does not create a protection hit.
- A very long line can contain both story text and a strong external signal. When the scanner
  marks `mutation_guard: "long_line_mixed_content"`, the complete line is never a safe delete
  unit; keep it or block for review instead of deleting surrounding prose.

## Review outcome

Use `delete` only when the complete candidate and every anchor are external to the story and satisfy the two-evidence gate. Use `keep` for clear narrative text and `uncertain` whenever deletion could remove authored content.
