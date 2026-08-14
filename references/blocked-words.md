# Blocked Words

Use this reference for stage 4 blocked-word candidate review. The scanner locates masking forms only; it does not restore words.

## Masking Forms

- Repeated mask characters: `**`, `***`, `××`, `□□`, `口口`, `■■`.
- Single mask inside a Chinese word or phrase: `他*了`, `做×事`.
- Separators inserted inside a word: `做.爱`, `杀·人`, `亲  吻`.
- Fullwidth variants: `＊`, `Ｘ`, `．`.
- The current scanner does not infer pinyin or homophone replacements; record them only as review notes and leave the source unchanged.

## Review Rules

- Keep every finding report-only in this release, regardless of apparent confidence.
- Preserve the original text and list alternatives only as review notes.
- Names, places, factions, skills, items, passwords, system codes, and other plot keys remain unchanged.
- Do not sanitize, moralize, infer, or restore content.

## Repair Boundary

This Skill only reports `candidates/blocked.jsonl`; it has no blocked-word compiler, apply path,
or verification path. If the user requests a mutation, stop and report that boundary. Never
hand-edit the text, handwrite executable decisions, or reuse the advertisement executor.
