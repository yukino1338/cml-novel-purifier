# Text Input Contract

Use this reference before preprocessing a novel or choosing a layout profile. The
workflow is deliberately narrower than a general document converter.

## Accepted input

- Input is one local plain-text `.txt` file. PDF, DOCX, OCR, DRM-protected files,
  website downloads, and ebook-library imports are outside this workflow.
- Automatic detection tries strict UTF-8, GB18030, and Big5. It stops when no
  healthy decoding is unique enough to trust.
- An explicit override accepts only `ascii`, `big5`, `gb18030`, `utf-8`,
  `utf-8-sig`, `utf-16`, `utf-16-le`, or `utf-16-be`. UTF-32, unknown codecs, and an override that
  conflicts with a BOM are rejected.
- The guarantee is strict handling of a single primary encoding. A byte run from
  another encoding can sometimes be legal under the primary decoder and produce
  the wrong characters; automatic detection cannot promise to identify every such
  mixed-encoding injection.

## Immutable source and UTF-8 working text

- The source file and `versions/v0_original.txt` remain byte-for-byte unchanged.
  The preprocessing report binds their byte size and SHA-256 identity.
- Successful preprocessing writes `versions/v1_preprocessed.txt` as UTF-8 without
  a BOM and records its byte size and SHA-256 separately from the source identity.
- Repairs never overwrite source/v0. The supported repair flow may create only
  `versions/v0_prepared_input.txt`, with its own byte identity and plan provenance.

## Explicit mixed-encoding repair

Input repair is available only after preprocessing has safely stopped on the immutable
original input. It is not an automatic decoder or a general text editor:

1. Run `python scripts/input_repair.py inspect <workspace> --primary-encoding <encoding>`.
   The report lists strict alternate decodes, bounded previews, exact physical-line byte
   ranges and the known limitation that foreign bytes legal under the primary decoder can
   evade detection.
2. Review the full source context and create `input_repair/repair_plan.json`. The only
   supported action is `drop_full_physical_line`; it binds the source/report hashes, byte
   range, complete newline, decoded text hash and the literal confirmation
   `DROP_FULL_PHYSICAL_LINE`.
3. Run `python scripts/input_repair.py apply-plan <workspace>`, then rerun preprocessing
   with the same explicit encoding and `--use-prepared-input`.

The tool permits a drop only when the whole physical line has both an external locator and
promotion intent, has no narrative framing, and strictly decodes under the reviewed
alternate encoding. It recomputes the candidate report before execution and copies retained
raw byte ranges; it never reconstructs characters, performs partial transcoding, uses
`errors=replace`, or changes source/v0. Mixed story-and-ad lines remain a content-review
problem and cannot be repaired by this byte-line workflow.

## Allowed preprocessing changes

- CRLF, lone CR, U+2028, and U+2029 line endings become LF. Each source form is
  counted separately.
- Only the declared `ZERO_WIDTH` set in `scripts/preprocess.py` is removed, with
  per-code-point counts.
- Whether the source ended in a line terminator is preserved: preprocessing does
  not add or remove the final newline.
- NUL, replacement characters, and non-whitelisted C0 controls block the stage;
  they are never silently discarded.

## Text that remains unchanged

- Simplified and Traditional Chinese, mixed Chinese/English text, digits, foreign
  names, emoji, code-like text, and full-width punctuation are preserved.
- Private-use characters are preserved and counted with a warning; they are not
  treated as proof that prose is corrupt.
- Unicode folding is permitted only for comparison keys. It must not rewrite the
  novel body with global NFC or NFKC normalization.
- Ordinary long prose remains intact. A line that mixes story text and an external
  signal requires bounded segment review; it must not be deleted as one whole line.

## Structure and layout

- Chapter detection uses the shared matcher in `scripts/parse_structure.py` for
  Chinese and Traditional headings, Arabic or Chinese numbers, volumes, chapters,
  sections, parts, episodes, prefaces, prologues, epilogues, afterwords, extras,
  and supported English headings.
- Low-confidence fallback chunks are locator blocks, not chapters and not rollback
  targets.
- The default layout profile is `preserve`: no indentation, blank-line collapse,
  punctuation conversion, trailing-space trimming, or script conversion. A
  normalize profile is allowed only after the user explicitly requests uniform
  formatting.

## Default output

The default reading output is one UTF-8-without-BOM `.txt` file, matching the
source format. Markdown, EPUB, script conversion, and normalized layout are opt-in
outputs or transformations.
