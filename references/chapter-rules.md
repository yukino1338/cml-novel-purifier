# Chapter Rules

Use this reference when parsing or reviewing chapter structure.

## Recognized Chapter Lines

Match only likely standalone chapter headings. Keep false positives conservative.

- Chinese numeric headings: `第1章`, `第一章`, `第十章`, `第001回`, `第3卷`, `第六部`, `第 12 话`, `第兩百話`, `第 12 節`.
- Source-prefixed headings: `正文 第001章 标题`.
- Volume headings: `卷一`, `卷 2`, `卷三 风起`.
- Common front/back matter: `序`, `序章`, `序言`, `序文`, `序幕`, `楔子`, `引子`, `前言`, `后记`, `後記`, `尾声`, `尾聲`, `终章`, `終章`, `番外`, `番外 1`.
- English headings: `Chapter 1`, `CHAPTER 12`, `Chapter-001`, numbered `Part`/`Volume`/`Book`, and standalone `Prologue`, `Epilogue`, `Foreword`, `Afterword`, or `Introduction`.
- Numbered title headings such as `1、标题` are allowed only when the whole line is short.
- Plain numeric headings such as `001 标题` are allowed only when the line is short and does not look like prose.

## Conservative Filters

- Treat very long lines as正文, not headings.
- Treat headings embedded inside dialogue or narration as疑似伪标题 unless they occupy the whole line.
- Reject catalog/navigation/ad fragments such as `目录`, `章节列表`, `最新章节`, `上一章`, `下一章`, `无弹窗`, `更新最快`.
- Reject heading candidates whose title tail looks like body prose, especially when it contains `。！？!?；;`.
- Report missing, duplicate, or out-of-order chapter numbers. Do not repair them in this release.
- Do not rename, reorder, add, or delete chapters.
- All scanners and layout logic must call the shared `match_chapter` matcher in
  `scripts/parse_structure.py`; they must not carry a narrower private heading regex.

## Structure Confidence And Fallback

- `structure_confidence.level` is `high`, `medium`, or `low`.
- Low confidence on large texts enables `fallback_chunks`. These are locator blocks only, never real chapters.
- `meta/chapters.json` keeps real chapters in `chapters`; fallback locator blocks live in `fallback_chunks` and `locators`.
- All title findings remain report-only; at low confidence, fallback chunks must not be used as repair evidence.

## Report Expectations

`meta/chapters.json` should include each chapter's label/title, line number, start/end offsets, word count, and health flags.

`report/structure_report.json` should summarize chapter count, duplicate labels, non-monotonic numbering, suspicious chapter lengths, structure confidence, and fallback chunking status.
