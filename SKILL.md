---
name: cml-novel-purifier
description: Conservatively clean local Chinese plain-text (.txt) novels by preserving an immutable source snapshot, fully reviewing external advertisements or watermarks, compiling and atomically applying exact-anchor decisions, verifying the final layout, exporting traceable TXT, Markdown, and EPUB outputs, and optionally reporting suspicious chapter-title or masked-word candidates without changing them. Use when an Agent is asked to clean, purify, remove ads or watermarks from, inspect title or masking anomalies in, or safely export a Chinese TXT novel; do not use for general document editing, semantic rewriting, or ebook-library management.
---

# CML Novel Purifier

## Keep the safety contract

Use this Skill for local Chinese TXT novel cleanup. Do not turn it into a general editor.

- Preserve the source file and `<workspace>/versions/v0_original.txt` byte for byte.
- Treat `<workspace>/manifest.json` as the authority for workspace identity, `current_head`, artifact lineage, and stage status.
- Let scanners propose candidates; make the host Agent judge every current candidate.
- Let `finalize_ad_decisions.py` copy complete executable anchors only into mutating `delete` decisions; non-mutating decisions remain bound by candidate anchor IDs and hashes. Never handwrite, repair, or substitute executable anchors.
- Apply the complete formal decision file atomically. Any stale, missing, duplicate, overlapping, or invalid anchor stops the whole run without a partial text version.
- Prefer retained noise over deleted story text. Never infer missing or masked prose.
- Require a current final `passed` verification attestation before export. There is no manual override for a blocked or incomplete verification.
- Keep title and blocked-word findings strictly report-only. This release has no supported compiler/apply/verify path for mutating them, even when a user requests a repair.
- Keep all workspace and delivery paths inside their validated roots. Do not bypass path, identity, transaction, or recovery failures.
- Do not upload novel text, candidates, reports, or context to an external service unless the user explicitly authorizes that separate action.

The Python scripts do not call a model. The host Agent owns candidate judgment and must follow every stop condition below.

## Public licensing boundary

This repository is distributed under the [PolyForm Noncommercial License 1.0.0](LICENSE.txt).
It grants no commercial-use permission through this repository. Do not claim that a
commercial license is available, and do not invent a rightsholder or a contact method.

## Load only the needed guidance

- Read `references/text-input-contract.md` before preprocessing, repairing an
  input, or choosing any non-preserving layout behavior.
- Read `references/chapter-rules.md` before interpreting low-confidence structure.
- Read `references/ad-patterns.md` and `references/judgment-guide.md` before every formal advertisement review.
- Read `references/blocked-words.md` only when the user asks for blocked-word analysis.
- Read `references/performance.md` only for benchmark or baseline work.
- Read `assets/config-templates/default.json` before custom layout or export configuration.

Do not copy rule lists out of `scripts/ad_rules.py`; it is the executable fact source.

## Resolve paths and choose the workflow

Resolve `SKILL_ROOT` to the directory that contains this `SKILL.md`. Run every
`python scripts/...` command below with its working directory set to `SKILL_ROOT`;
never assume the invoking Agent's current directory. Use Python 3.11–3.14. Resolve
novel and workspace arguments to absolute paths. Also resolve every user-provided
external config file or output directory to an absolute path. Keep artifacts inside
the workspace relative to the workspace root, including documented artifact arguments
exactly as shown.

For a clear user-facing task root, optionally initialize one outside `SKILL_ROOT`:

```bash
python scripts/init_job_root.py "<absolute job-root>"
```

It creates `待清洗_Input/`, `小说清洗结果_Novel-Purifier/`, and the hidden
`.cml-novel-purifier/workspaces/` area; it never copies, scans, or deletes a novel.
Workspace resolution is fixed: explicit `--workspace`; a matching existing legacy
`<source>.cleanwork`; the initialized job root's hidden workspace area; otherwise
`<source.parent>/.cml-novel-purifier/workspaces/`. Never create a new source,
workspace, delivery, or user artifact inside `SKILL_ROOT`. Existing ignored legacy
workspaces may be read in place; do not move or delete them.

At acceptance, proactively tell the user the absolute input path, that source/v0 will
not be modified, the default `TXT + preserve` behavior, and the expected absolute result
root. Do not wait until the end to disclose where files will go.

When the request is only to inspect chapter titles and/or masked words, use the
report-only branch. First run only the required preprocessing and parsing:

```bash
python scripts/preprocess.py "<absolute novel.txt>"
python scripts/parse_structure.py "<absolute workspace>"
```

Honor the preprocessing and parsing stop checks in step 1, but do not build
`meta/book_profile.json`. Then run only the requested command(s), in this order when
both are requested:

```bash
python scripts/scan_titles.py "<absolute workspace>"
python scripts/scan_blocked.py "<absolute workspace>"
```

Read the requested candidate/report pair(s):

- `candidates/titles.jsonl`
- `report/titles_scan_report.json`
- `candidates/blocked.jsonl`
- `report/blocked_scan_report.json`

Do not enter cleaning steps 2 through 5. This branch must not scan or apply advertisement
decisions and must not mutate novel text. It still has a user-visible terminal state: call
`publish_result.py` as described in step 6. The publisher emits `report_only`, a review page,
and `result.json`, but no cleaned reading file.

For advertisement cleanup, watermark removal, or safe export, follow the complete
workflow below. Add the optional report-only scans at step 5 only when requested.

## Follow the cleaning and export workflow

### 1. Create and validate the workspace

Run:

```bash
python scripts/preprocess.py "<novel.txt>"
python scripts/parse_structure.py "<absolute workspace printed by preprocess>"
```

`preprocess.py` reuses a matching legacy sibling workspace when one exists; otherwise it
creates the hidden workspace selected by the frozen priority above, plus an immutable v0
snapshot. It exits nonzero when encoding detection is blocked. Also confirm these facts
before continuing:

- `manifest.stages.0_preprocess.status` is `done`.
- `report/preprocess_report.json` has `encoding_detection.blocked == false`.
- `versions/v1_preprocessed.txt` is the committed current artifact.

If decoding is blocked, preserve the report and ask for a supported explicit encoding; do
not guess and do not edit v0. If the user explicitly requests recovery from a mixed-encoding
injection and confirms the primary encoding, read the input-repair section of
`references/text-input-contract.md`. Run `input_repair.py inspect`, show the bounded
candidates, and require explicit confirmation for every eligible complete physical line
before writing the bound plan and running `apply-plan`. Then rerun `preprocess.py` with the
same `--encoding` and `--use-prepared-input`. Never use this path for a line containing story
text, never reconstruct characters, and never continue to parsing while preprocessing is
blocked. NUL, replacement characters, unsupported controls, ambiguous decoding, BOM
conflicts, and unsupported codecs otherwise remain stop conditions. Private-use characters
are preserved and reported; they are not deletion evidence.

`parse_structure.py` writes `meta/chapters.json`. Treat `chapters` as true chapters.
`fallback_chunks` and `locators` are positioning aids only; never use them as chapter
rollback targets or automatic title-repair evidence.

Build `meta/book_profile.json` from local opening, ending, representative middle excerpts, and the parsed chapter list. Use these exact keys when supported by evidence: `title`, `author`, `genre`, `narrative_style`, `main_characters`, `places`, `factions`, `terms`, `legitimate_structures`, `summary`, `evidence`, and `rename_verified`.
Store `title`, `author`, `genre`, `narrative_style`, and `summary` as strings; store `main_characters`, `places`, `factions`, `terms`, `legitimate_structures`, and `evidence` as arrays of strings; store `rename_verified` as a boolean.
Set it to `false` unless a user-confirmed or independently verified source also agrees with the local text. Profile creation never changes novel text.

### 2. Scan every advertisement candidate page

Read the advertisement references, then run:

```bash
python scripts/scan_ads.py "<workspace>"
python scripts/make_ad_decisions.py "<workspace>"
```

The scanner uses fast `boundary` near-repeat analysis by default while exact repeats and external-pattern checks still cover the full text. Use `--near-scan-scope all` only when a full near-repeat sweep is justified. The scan report records the chosen scope.

`candidates/ads.jsonl` is only the first review page. Use `report/ads_scan_report.json` and its page manifest to read every ordered file under `candidates/ads_pages/`. `make_ad_decisions.py` automatically consumes and validates the complete current page set; it has no `--all-pages` option.

Treat `decisions/ads_decisions.draft.jsonl` as evidence, never as execution authority.
If a delete candidate has `anchors_truncated: true`, stop. Increase `--max-anchors`, rescan,
reread every page, and regenerate all reviews; never execute a truncated anchor set.

### 3. Review every bounded Agent page

`make_ad_decisions.py` creates the complete machine ledger under `candidates/ads_pages/`
and a separate, non-executable Agent projection under `candidates/ads_review_pages/`.
Read `manifest.json`, then read every declared `page_*.json` in page-number order. Each
occurrence includes bounded source context, its chapter or locator, physical line, ordinal,
text hash, and context hash. Projection records intentionally contain no source offsets or
executable anchors.

For every projection page, write the matching
`decisions/ads_agent_reviews/pages/page_NNNN.jsonl`. Its first record must bind the page:

```json
{"record_type":"page_attestation","schema":"cml.ad-review-attestation.v1","page_number":1,"page_sha256":"<manifest page SHA-256>","manifest_sha256":"<draft report review_pages_manifest_sha256>","projection_set_sha256":"<manifest projection set SHA-256>","occurrence_coverage_sha256":"<manifest page coverage SHA-256>"}
```

Then add one `candidate_verdict` for each candidate whose verdict is assigned on that page:

```json
{"record_type":"candidate_verdict","review_group_id":"RG-...","candidate_id":"AD-0001","candidate_fingerprint":"<candidate SHA-256>","verdict":"keep","confidence":0.99,"reason":"剧情正文"}
```

If interrupted, keep completed page files and resume only the missing pages. Do not merge by
hand. `finalize_ad_decisions.py` rejects missing, extra, duplicate, reordered, stale, or
occurrence-incomplete pages, then deterministically writes the merged
`decisions/ads_agent_reviews.jsonl`. A zero-candidate run still has one empty projection page;
write its attestation and compile normally.

Apply these schema rules:

- Copy `candidate_id`, `candidate_fingerprint`, and `review_group_id` from the projection.
- Use a finite numeric `confidence` from 0 through 1 and a non-empty evidence-based reason.
- A `delete` record may add `action: "delete"`, `risk: "low|medium|high"`, and one supported
  `splice_strategy`: `remove_paragraph`, `exact`, `fallback_newline`, or the scanner-bound
  `exact_segment` described below.
- Never put anchors in the compact Agent review. The compiler owns them.
- A `keep` may carry a complete `cml.keep-basis.v1` object only as specified in
  `references/judgment-guide.md`. Use it for an audited rule false positive or a
  conflicting machine-delete draft; never invent occurrence IDs or hashes.
- Use `uncertain` when deletion could remove authored content, and add a non-empty
  `blocking_reasons` array. Do not add action or strategy.

For a mixed narrative/ad occurrence, use `exact_segment` only when the projection explicitly
provides a current `edit_plan_id`, bounded keep/delete/after previews, and declares segment
deletion allowed. Copy only that `edit_plan_id`; never supply offsets, ranges, parent bindings,
segments, or replacement text. The compiler copies the executable `cml.ad-edit-plan.v1` plan
from the current scanner ledger and rejects stale identity, incomplete occurrence coverage,
unsupported boundaries, overlaps, or any parent/context/hash drift. If the projected boundary is
wrong, keep or mark it `uncertain`; do not repair the range by hand.

`review_group_id` is independent of the scanner's ADF family. A group verdict must copy the
manifest's complete member ID list, fingerprint list, and coverage hash. Batch `delete` is
permitted only when `group_kind` is `delete_exact` and `delete_group_allowed` is true; every
member has already passed the independent locator, intent, occurrence, protection, boundary,
and splice-shape gates. A segment-delete group must also copy the manifest's complete
`member_edit_plan_ids` mapping; the compiler expands each member with its own current plan ID.
Otherwise submit individual verdicts. Never delete an ADF family or a similarity cluster as a
unit.

Use `delete` only when every saved occurrence is clearly outside the story, `keep` for
clear narrative content, and `uncertain` whenever deletion safety cannot be established.
Follow `references/judgment-guide.md` for all detailed verdict, `keep_basis`, profile-overlap,
mixed-content, and splice-strategy rules.

Do not ask the user to copy candidate IDs back to the same Agent. Reviewing candidates is the host Agent's normal work after the user invokes this Skill.

### 4. Compile and atomically apply formal decisions

Run:

```bash
python scripts/finalize_ad_decisions.py "<workspace>"
```

The compiler requires 100% current-candidate coverage and rejects stale rule/profile identities,
missing, extra, or duplicate reviews, invalid fields, unsafe delete candidates, incomplete
occurrence coverage, a conflicting `keep` without an explicit supported basis, and changed
pagination. A validated structured `keep` is the supported false-positive terminal state; it is
not a bypass.
Treat every compiler error as a stop until its stated contract violation is resolved under
`references/judgment-guide.md`; never weaken a judgment merely to pass compilation.
Inspect `report/ad_decision_formal_report.json`. If `by_verdict.uncertain` is nonzero, stop and report the blockers. Revise evidence and recompile only when a safe judgment becomes possible.

When no formal decision is uncertain, run:

```bash
python scripts/apply_decisions.py --workspace "<workspace>" --module ads --input versions/v1_preprocessed.txt --decisions decisions/ads_decisions.jsonl --output versions/v2_ads_removed.txt --stage 2_ads
```

Use only the compiler-produced `decisions/ads_decisions.jsonl`. The executor preflights the entire decision set, rejects overlap or identity drift, writes current-run audit logs, and commits the text, logs, report, lineage, and stage state as one transaction.
Before any write, it also re-hashes the current scan pages, compact reviews, rule draft, formal report, and formal decisions, then replays the compiler. A missing, edited, or handwritten provenance artifact stops the whole apply.

### 5. Preserve layout and verify the final head

For the complete cleaning workflow, run only the requested report-only title or
blocked-word scan(s) now, before layout, verification, and export. If both are
requested, run titles before blocked words:

```bash
python scripts/scan_titles.py "<workspace>"
python scripts/scan_blocked.py "<workspace>"
```

These scans never mutate text, but they update pipeline state. Adding either scan to an already
verified workspace invalidates downstream stages; rerun from the earliest `pending` stage shown
in the manifest before delivery.

Run the default preserve-layout stage, then final verification:

```bash
python scripts/normalize_layout.py "<workspace>"
python scripts/verify.py "<workspace>"
```

The default profile copies every input character unchanged; it does not add indentation,
collapse blank lines, trim trailing spaces, convert punctuation, or add a final newline.
Only set `layout.enabled` to `true` when the user explicitly requests uniform formatting.
The normalize profile protects ASCII syntax when `safe_chinese` is explicitly configured.
A custom config must match
`assets/config-templates/default.json`; the only extra root key is a non-empty `inherits`
string for a parent config, and the resolved chain is path-checked and cycle-checked.

`verify.py` binds the current apply run and current head, replays apply and layout, checks
chapter identity, layout idempotence, exact per-anchor accounting, current-run anomalies,
the complete formal-decision provenance, the 8% deletion limit, and a complete
strong-residual scan. It exits nonzero unless status is `passed`. Never use
`--skip-residual-scan` for final delivery; that status is `incomplete`.

Any formal `uncertain` decision blocks attestation even when no residual scanner rule fires.

### 6. Publish every terminal state

任何终态都不得从 raw preprocess, scan, finalize, apply, verify, export, or review
stdout 直接结束。
For `completed`, `needs_review`, `blocked`, `incomplete`, and `report_only`, always call the
single user-facing publisher:

```bash
python scripts/publish_result.py "<workspace>" --delivery-root "<absolute 小说清洗结果_Novel-Purifier>"
```

Without format flags, a completed run publishes only a byte-exact UTF-8 TXT. Use repeatable
`--format txt|markdown|epub` for selected formats, or `--all-formats` only when the user
explicitly requests every format. If the user asks for conversion without naming a format,
ask which format. Do not generate extra formats speculatively. All requested formats, review,
and `result.json` publish as one atomic delivery; one requested-format or latest-index failure
publishes none of the new attempt. The publisher independently requires the current final
`passed` attestation and exact current-head SHA, lineage, rule version, checks, apply run,
layout run, and current runtime identity before it generates any reading file.

Interpret format requests by their meaning, not by a tempting shortest command: when the user
says “增加 Markdown 或 EPUB”, preserve the default TXT and add the requested format, for
example `--format txt --format markdown`. Use a one-format command only for an explicit
exclusive request such as “只要 Markdown” or “只转换为 EPUB”. A report-only or any
non-completed terminal delivery has no produced reading format (`[]`), regardless of an earlier
format wish.

增加 Markdown 或 EPUB 时仍传 `--format txt`；“只要 Markdown”才可以省略 TXT。
增加 Markdown 或 EPUB 时仍传 --format txt；只要 Markdown 才可以省略 TXT。

Every attempt creates a new delivery directory. `00_从这里开始_Start-Here.html` and
`latest.json` update only after the bundle is complete and separately retain the latest
attempt and latest success. A non-completed status still publishes
`01_查看结果_Review.html` and `03_处理摘要_Result.json`, never a reading file. The review's
advanced audit points to internal logs; do not copy the workspace `report/` tree into the
user delivery.

For a validated batch, call the publisher once per workspace into one shared external result
root and report each status separately. Do not use raw batch-export stdout as the terminal
receipt.

The publisher exit codes are fixed: `0=completed`, `2=a reliable non-completed bundle was
published`, `1=publisher failure with no reliable new bundle`. Its stdout is a small terminal
JSON receipt. Proactively show its absolute paths; never wait for the user to ask for the
webpage. Use this exact final-answer shape:

```text
状态：completed / needs_review / blocked / incomplete / report_only
复核页：<absolute review path>
清洗后文件：<absolute primary path 或“未生成”>
结果目录：<absolute delivery path>
实际格式：<txt / markdown / epub / 无>
原文与 v0：<unchanged / mismatch>
摘要：删除 X 处，保留 Y 处，未决 Z 处；原文未修改
下一步：<无 / 一条明确动作>
```

When any encoding, review, or verification blocker first appears, publish the non-completed
bundle immediately and notify the user with the review path, exact reason, and one next action.
The page can prepare an exception-review request or rollback command, but it never modifies
the workspace. Reload current candidate records before revising a formal review.

## Stop safely

Stop the current workflow and preserve existing committed artifacts when any of these occurs:

- Encoding is blocked, preprocessing is not `done`, or source/v0 identity changes.
- Manifest schema, current head, parent lineage, workspace role, path boundary, lock,
  transaction recovery, or atomic publish validation fails.
- Current scan pages are missing, duplicated, reordered, changed, or inconsistent with their
  scan ID, candidate-set hash, structure hash, or input hash.
- Agent reviews are incomplete, duplicated, stale, malformed, or contain unknown candidates.
- Any formal decision remains `uncertain`, or any mutating `delete` lacks complete executable anchors or reports truncated anchors.
- Any anchor is missing, ambiguous, stale, duplicated, overlapping, or uses an unsupported
  strategy. Never skip only the failing anchor.
- Structure confidence is low and a request depends on fallback chunks as real chapters.
- Apply/layout replay, run binding, chapter identity, deletion risk, residual scan, anomaly,
  or final current-head validation fails.
- Verification is `blocked` or `incomplete`, or any warning/check is unresolved.
- Export rejects its attestation. Never edit the manifest or verification report to bypass it.
- A batch is partial. Never describe the whole batch as complete.

Explain the exact report/check that blocked progress and the safe next action. Do not downgrade
a code-enforced stop because the user is willing to accept risk.

## Keep titles and blocked words high risk

Within a complete cleaning workflow, run only requested title or blocked-word scans at
step 5. For a pure title/blocked report-only request, use the earlier report-only branch and
stop there. When all three scans are intentionally current, inspect their combined readiness with:

```bash
python scripts/dry_run.py "<workspace>"
```

Treat duplicate, missing, non-monotonic, pseudo, or low-confidence title findings as reports.
Do not rename, reorder, add, or delete chapters. Do not restore blocked text by semantics,
moral preference, or guesswork. Names, places, factions, abilities, codes, and plot keys remain
unmodified. `dry_run.py` never edits text and exits nonzero if any of ads, titles, or blocked
artifacts is pending.

Title and blocked-word findings are strictly report-only in this release. If the user requests
either mutation, stop and report that no compiler/apply/verify path exists. Never hand-edit the
text, handwrite executable decisions, or reuse the ads executor for those modules.

## Roll back through the script

Use the narrowest requested scope:

```bash
python scripts/rollback.py "<workspace>" --level all
python scripts/rollback.py "<workspace>" --level module --module ads --overwrite
python scripts/rollback.py "<workspace>" --level chapter --module ads --chapter 12
python scripts/rollback.py "<workspace>" --level point --module ads --candidate-id AD-0042
```

Chapter rollback accepts true chapter indexes only. Point rollback must match exactly one
mutating formal decision. Module, chapter, and point rollbacks validate the active apply baseline; `all` validates immutable v0 and workspace identity. Every rollback commits a new
current head atomically, and marks affected stages pending. Read `rollback_report.invalidated_stages` and the manifest, then rerun in order from the earliest pending stage. For `all`, restart at preprocess and parse. For ads module, chapter, or point rollback, run scan, regenerate the rule draft, reread every new page, write a new complete review set that encodes the desired retained state, then run finalize, apply, layout, verify, export, and review. Never reuse old pages, formal decisions, or attestations.
Optional title/blocked stages never requested by the user may stay pending; rerun either one before layout only if it was requested and then invalidated.

## Respect current limits

- Input is a local text novel, not PDF, DOCX, OCR, DRM, web scraping, or library management.
- The workflow removes external advertisements and watermarks; it does not rewrite plot,
  polish style, translate, summarize the book for publication, or continue the story.
- Candidate anchors describe complete scanner blocks. Safe arbitrary inline editing is not
  supported. The sole sub-block exception is a scanner-generated, compiler-validated
  `exact_segment` plan; the Agent may reference its `edit_plan_id` but may not choose or alter
  any range.
- EPUB output is intentionally basic; it is validated for package structure and semantic text,
  not rich book design.
- Traditional/simplified conversion is off by default and depends on optional OpenCC when
  explicitly configured.
- Use the declared performance support and benchmark commands in
  `references/performance.md`; do not generalize one machine's timings.
