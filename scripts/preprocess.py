from __future__ import annotations

import argparse
import codecs
import hashlib
import json
from pathlib import Path
import sys
from typing import Any
import unicodedata

from common import (
    WorkspaceTransaction,
    init_workspace_from_source,
    load_manifest,
    read_utf8,
    resolve_workspace_paths,
    stage_invalidation_targets,
    workspace_transaction_lock,
    workspace_for_source,
    write_json,
    write_utf8,
)


ZERO_WIDTH = {
    "\ufeff",
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
}

AUTO_ENCODINGS = ("utf-8", "gb18030", "big5")
EXPLICIT_ENCODINGS = (
    "ascii",
    "big5",
    "gb18030",
    "utf-8",
    "utf-8-sig",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
)
SUPPORTED_EXPLICIT_ENCODINGS = frozenset(EXPLICIT_ENCODINGS)
BOMS = (
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
MIN_AUTO_SCORE = 70.0
MIN_SCORE_GAP = 12.0
MIN_NON_ASCII_EVIDENCE_BYTES = 8


def _canonical_encoding(value: str) -> str | None:
    try:
        canonical = codecs.lookup(value).name
    except (LookupError, TypeError):
        return None
    aliases = {
        "iso8859-1": "latin-1",
        "utf-16le": "utf-16-le",
        "utf-16be": "utf-16-be",
    }
    return aliases.get(canonical, canonical)


def _bom_encoding(raw: bytes) -> str | None:
    for marker, encoding in BOMS:
        if raw.startswith(marker):
            return encoding
    return None


def _bom_is_compatible(bom: str, requested: str) -> bool:
    return requested == bom or (
        (bom == "utf-8-sig" and requested == "utf-8")
        or (bom in {"utf-16-le", "utf-16-be"} and requested == "utf-16")
    )


def _decode_strict(raw: bytes, encoding: str) -> str:
    if encoding == "utf-16-le" and raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="strict")
    if encoding == "utf-16-be" and raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="strict")
    return raw.decode(encoding, errors="strict")


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x323AF
    )


def _is_unexpected_east_asian_script(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF or 0x3100 <= code <= 0x312F or 0x31A0 <= code <= 0x31BF
    )


def _is_abnormal_punctuation(char: str) -> bool:
    code = ord(char)
    return 0xFE10 <= code <= 0xFE6F or char in {"\u00a7", "\u00ad"}


def text_quality(text: str) -> tuple[float, dict[str, int | float]]:
    length = len(text)
    denominator = max(1, length)
    replacement_count = text.count("\ufffd")
    control_count = sum(
        unicodedata.category(char) == "Cc" and char not in {"\n", "\r", "\t"}
        for char in text
    )
    invalid_unicode_count = sum(
        unicodedata.category(char) in {"Cn", "Cs"} for char in text
    )
    private_use_count = sum(unicodedata.category(char) == "Co" for char in text)
    abnormal_punctuation_count = sum(_is_abnormal_punctuation(char) for char in text)
    punctuation_count = sum(unicodedata.category(char).startswith("P") for char in text)
    cjk_count = sum(_is_cjk(char) for char in text)
    unexpected_script_count = sum(
        _is_unexpected_east_asian_script(char) for char in text
    )
    text_char_count = sum(unicodedata.category(char)[0] in {"L", "N"} for char in text)
    non_whitespace_count = sum(not char.isspace() for char in text)
    mojibake_marker_count = sum(
        text.count(marker) for marker in ("锟斤拷", "烫烫烫", "鈥", "銆")
    )

    punctuation_density = punctuation_count / denominator
    score = 100.0
    score -= min(100.0, replacement_count * 100.0)
    score -= min(100.0, control_count * 100.0)
    score -= min(100.0, invalid_unicode_count / denominator * 800.0)
    score -= min(60.0, abnormal_punctuation_count / denominator * 300.0)
    score -= min(45.0, unexpected_script_count / denominator * 180.0)
    score -= min(60.0, mojibake_marker_count / denominator * 500.0)
    score -= max(0.0, punctuation_density - 0.35) * 200.0
    if non_whitespace_count == 0 or text_char_count == 0:
        score = 0.0

    metrics: dict[str, int | float] = {
        "char_count": length,
        "non_whitespace_char_count": non_whitespace_count,
        "text_char_count": text_char_count,
        "replacement_char_count": replacement_count,
        "replacement_char_density": replacement_count / denominator,
        "control_char_count": control_count,
        "control_char_density": control_count / denominator,
        "invalid_unicode_count": invalid_unicode_count,
        "invalid_unicode_density": invalid_unicode_count / denominator,
        "private_use_char_count": private_use_count,
        "punctuation_count": punctuation_count,
        "punctuation_density": punctuation_density,
        "abnormal_punctuation_count": abnormal_punctuation_count,
        "abnormal_punctuation_density": abnormal_punctuation_count / denominator,
        "unexpected_script_count": unexpected_script_count,
        "unexpected_script_density": unexpected_script_count / denominator,
        "mojibake_marker_count": mojibake_marker_count,
        "cjk_char_count": cjk_count,
        "cjk_char_ratio": cjk_count / denominator,
    }
    return round(max(0.0, min(100.0, score)), 3), metrics


def _candidate(raw: bytes, encoding: str) -> tuple[str | None, dict[str, Any]]:
    try:
        text = _decode_strict(raw, encoding)
    except (UnicodeDecodeError, UnicodeError):
        return None, {
            "encoding": encoding,
            "strict_decode": False,
            "score": None,
            "metrics": {},
            "rejection_reason": "strict_decode_failed",
        }
    score, metrics = text_quality(text)
    return text, {
        "encoding": encoding,
        "strict_decode": True,
        "score": score,
        "metrics": metrics,
        "rejection_reason": None,
    }


def _healthy_candidate(candidate: dict[str, Any]) -> bool:
    metrics = candidate["metrics"]
    return bool(
        candidate["strict_decode"]
        and candidate["score"] >= MIN_AUTO_SCORE
        and metrics["replacement_char_count"] == 0
        and metrics["control_char_count"] == 0
        and metrics["invalid_unicode_count"] == 0
        and metrics["non_whitespace_char_count"] > 0
        and metrics["text_char_count"] > 0
    )


def _decoded_blocker(text: str) -> str | None:
    if "\ufffd" in text:
        return "replacement_character"
    if any(
        unicodedata.category(char) == "Cc" and char not in {"\n", "\r", "\t"}
        for char in text
    ):
        return "disallowed_control_character"
    return None


def detect_and_decode(
    raw: bytes,
    explicit_encoding: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    requested = _canonical_encoding(explicit_encoding) if explicit_encoding else None
    detection: dict[str, Any] = {
        "mode": "explicit" if explicit_encoding else "auto",
        "requested_encoding": explicit_encoding,
        "selected_encoding": None,
        "selection_reason": None,
        "confidence": 0.0,
        "blocked": True,
        "blocked_reason": None,
        "candidates": [],
    }
    if explicit_encoding and (
        requested is None or requested not in SUPPORTED_EXPLICIT_ENCODINGS
    ):
        detection["blocked_reason"] = "unsupported_explicit_encoding"
        return None, detection

    bom = _bom_encoding(raw)
    if bom in {"utf-32-le", "utf-32-be"}:
        detection["blocked_reason"] = "unsupported_bom"
        return None, detection
    if bom is not None:
        if requested is not None and not _bom_is_compatible(bom, requested):
            detection["blocked_reason"] = "explicit_encoding_conflicts_with_bom"
            return None, detection
        text, candidate = _candidate(raw, bom)
        detection["candidates"] = [candidate]
        if text is None:
            detection["blocked_reason"] = "no_strict_decoder"
            return None, detection
        blocker = _decoded_blocker(text)
        if blocker is not None:
            detection["blocked_reason"] = blocker
            return None, detection
        if not _healthy_candidate(candidate):
            detection["blocked_reason"] = "low_text_quality"
            return None, detection
        detection.update(
            {
                "selected_encoding": bom,
                "selection_reason": "bom",
                "confidence": 1.0,
                "blocked": False,
                "blocked_reason": None,
            }
        )
        return text, detection

    if requested is not None:
        text, candidate = _candidate(raw, requested)
        detection["candidates"] = [candidate]
        if text is None:
            detection["blocked_reason"] = "no_strict_decoder"
            return None, detection
        blocker = _decoded_blocker(text)
        if blocker is not None:
            detection["blocked_reason"] = blocker
            return None, detection
        if not _healthy_candidate(candidate):
            detection["blocked_reason"] = "low_text_quality"
            return None, detection
        detection.update(
            {
                "selected_encoding": requested,
                "selection_reason": "explicit_override",
                "confidence": 1.0,
                "blocked": False,
                "blocked_reason": None,
            }
        )
        return text, detection

    decoded: list[tuple[str, str, dict[str, Any]]] = []
    for encoding in AUTO_ENCODINGS:
        text, candidate = _candidate(raw, encoding)
        detection["candidates"].append(candidate)
        if text is not None:
            decoded.append((encoding, text, candidate))

    if not decoded:
        detection["blocked_reason"] = "no_strict_decoder"
        return None, detection

    utf8 = next((item for item in decoded if item[0] == "utf-8"), None)
    if utf8 is not None:
        blocker = _decoded_blocker(utf8[1])
        if blocker is not None:
            detection["blocked_reason"] = blocker
            return None, detection
        if not _healthy_candidate(utf8[2]):
            detection["blocked_reason"] = "low_text_quality"
            return None, detection
        detection.update(
            {
                "selected_encoding": "utf-8",
                "selection_reason": "strict_utf8",
                "confidence": round(utf8[2]["score"] / 100.0, 3),
                "blocked": False,
                "blocked_reason": None,
            }
        )
        return utf8[1], detection

    distinct_texts = {text for _, text, _ in decoded}
    decoded_blockers = {_decoded_blocker(text) for _, text, _ in decoded}
    if len(decoded_blockers) == 1 and None not in decoded_blockers:
        detection["blocked_reason"] = decoded_blockers.pop()
        return None, detection
    healthy = [item for item in decoded if _healthy_candidate(item[2])]
    if not healthy:
        detection["blocked_reason"] = "low_text_quality"
        return None, detection
    healthy.sort(
        key=lambda item: (
            -item[2]["score"],
            AUTO_ENCODINGS.index(item[0]),
        )
    )
    selected = healthy[0]
    if len(raw) < MIN_NON_ASCII_EVIDENCE_BYTES and len(distinct_texts) > 1:
        detection["blocked_reason"] = "ambiguous_strict_decoding"
        return None, detection
    competing = [item for item in healthy[1:] if item[1] != selected[1]]
    score_gap = selected[2]["score"] - competing[0][2]["score"] if competing else 100.0
    if competing and score_gap < MIN_SCORE_GAP:
        detection["blocked_reason"] = "ambiguous_strict_decoding"
        return None, detection
    reason = "quality_score"
    confidence = min(selected[2]["score"] / 100.0, max(0.0, score_gap / 25.0))

    if confidence < 0.7:
        detection["blocked_reason"] = "low_text_quality"
        return None, detection
    detection.update(
        {
            "selected_encoding": selected[0],
            "selection_reason": reason,
            "confidence": round(confidence, 3),
            "blocked": False,
            "blocked_reason": None,
        }
    )
    return selected[1], detection


def decode_bytes(raw: bytes, encoding: str | None = None) -> tuple[str, str, int]:
    text, detection = detect_and_decode(raw, encoding)
    if text is None:
        raise UnicodeError(f"encoding detection blocked: {detection['blocked_reason']}")
    return text, detection["selected_encoding"], 0


def normalize_text(text: str) -> tuple[str, dict[str, Any]]:
    blocker = _decoded_blocker(text)
    if blocker is not None:
        raise ValueError(f"preprocessing blocked: {blocker}")
    zero_width_by_codepoint = {
        f"U+{ord(char):04X}": text.count(char)
        for char in sorted(ZERO_WIDTH)
        if text.count(char)
    }
    metrics: dict[str, Any] = {
        "crlf_count": text.count("\r\n"),
        "cr_count": text.count("\r") - text.count("\r\n"),
        "unicode_line_separator_count": text.count("\u2028"),
        "unicode_paragraph_separator_count": text.count("\u2029"),
        "zero_width_removed": sum(zero_width_by_codepoint.values()),
        "zero_width_removed_by_codepoint": zero_width_by_codepoint,
        "source_had_final_line_terminator": text.endswith(
            ("\r", "\n", "\u2028", "\u2029")
        ),
    }
    text = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n")
    )

    normalized = "".join(char for char in text if char not in ZERO_WIDTH)
    metrics["line_count"] = normalized.count("\n") + (1 if normalized else 0)
    metrics["char_count"] = len(normalized)
    metrics["output_has_final_lf"] = normalized.endswith("\n")
    metrics["replacement_char_count"] = normalized.count("\ufffd")
    metrics["replacement_char_density"] = metrics["replacement_char_count"] / max(
        1, metrics["char_count"]
    )
    metrics["private_use_char_count"] = sum(
        unicodedata.category(char) == "Co" for char in normalized
    )
    return normalized, metrics


def committed_preprocess_result_matches(
    workspace: Path,
    v1_path: Path,
    report_path: Path,
    normalized: str,
    report: dict[str, Any],
    input_relative: str = "versions/v0_original.txt",
) -> bool:
    manifest = load_manifest(workspace)
    stages = manifest.get("stages")
    stage = stages.get("0_preprocess") if isinstance(stages, dict) else None
    artifacts = manifest.get("artifacts")
    if not isinstance(stage, dict) or not isinstance(artifacts, dict):
        return False

    v1_relative = v1_path.relative_to(workspace).as_posix()
    report_relative = report_path.relative_to(workspace).as_posix()
    expected_paths = {v1_relative, report_relative}
    run_id = stage.get("run_id")
    if (
        stage.get("status") != "done"
        or stage.get("input") != input_relative
        or stage.get("output") != v1_relative
        or stage.get("report") != report_relative
        or not isinstance(run_id, str)
        or set(stage.get("artifacts", [])) != expected_paths
    ):
        return False

    expected_bytes = {
        v1_relative: normalized.encode("utf-8"),
        report_relative: (
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8"),
    }
    for relative, path in (
        (v1_relative, v1_path),
        (report_relative, report_path),
    ):
        record = artifacts.get(relative)
        content = expected_bytes[relative]
        if (
            not isinstance(record, dict)
            or record.get("path") != relative
            or record.get("stage") != "0_preprocess"
            or record.get("run_id") != run_id
            or record.get("sha256") != hashlib.sha256(content).hexdigest()
            or record.get("size_bytes") != len(content)
            or not path.is_file()
            or path.read_bytes() != content
        ):
            return False
    return True


def run(
    source: Path,
    workspace_arg: str | None = None,
    encoding: str | None = None,
    use_prepared_input: bool = False,
) -> Path:
    source = source.resolve()
    workspace = workspace_for_source(source, workspace_arg)
    input_relative = (
        "versions/v0_prepared_input.txt"
        if use_prepared_input
        else "versions/v0_original.txt"
    )
    workspace, read_paths, write_paths = resolve_workspace_paths(
        workspace,
        reads={"input": input_relative},
        writes={
            "v1": "versions/v1_preprocessed.txt",
            "report": "report/preprocess_report.json",
        },
        protected_paths=(source,),
        allow_missing_workspace=True,
    )
    init_workspace_from_source(source, workspace)

    selected_input = read_paths["input"]
    raw = selected_input.read_bytes()
    v0_raw = (workspace / "versions/v0_original.txt").read_bytes()
    source_identity = {
        "path": "versions/v0_original.txt",
        "size_bytes": len(v0_raw),
        "sha256": hashlib.sha256(v0_raw).hexdigest(),
    }
    decoded, detection = detect_and_decode(raw, encoding)
    if decoded is None:
        metrics: dict[str, Any] = {
            "raw_size_bytes": len(raw),
            "detected_encoding": None,
            "decode_replacement_count": 0,
            "char_count": 0,
            "line_count": 0,
            "replacement_char_count": 0,
            "replacement_char_density": 0.0,
        }
    else:
        normalized, metrics = normalize_text(decoded)
        metrics["decode_replacement_count"] = 0
        metrics["detected_encoding"] = detection["selected_encoding"]
        metrics["raw_size_bytes"] = len(raw)
    working_text_identity = None
    if decoded is not None:
        working_bytes = normalized.encode("utf-8")
        working_text_identity = {
            "path": "versions/v1_preprocessed.txt",
            "encoding": "utf-8",
            "bom": False,
            "size_bytes": len(working_bytes),
            "sha256": hashlib.sha256(working_bytes).hexdigest(),
        }

    report = {
        "source_version": "versions/v0_original.txt",
        "preprocess_input": {
            "path": input_relative,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "prepared": use_prepared_input,
        },
        "output_version": (
            "versions/v1_preprocessed.txt" if decoded is not None else None
        ),
        "source_identity": source_identity,
        "working_text_identity": working_text_identity,
        "encoding_detection": detection,
        "metrics": metrics,
        "warnings": [],
    }
    if decoded is not None and metrics["private_use_char_count"]:
        report["warnings"].append("private_use_characters_preserved")
    with workspace_transaction_lock(workspace):
        if decoded is None:
            report["warnings"].append(
                f"preprocessing blocked: {detection['blocked_reason']}"
            )
            updates: dict[str, tuple[str, dict[str, Any]]] = {
                "0_preprocess": (
                    "blocked",
                    {
                        "input": input_relative,
                        "report": "report/preprocess_report.json",
                        "blocked_reason": detection["blocked_reason"],
                        "_current_head": input_relative,
                    },
                )
            }
            for stage in stage_invalidation_targets("0_preprocess"):
                updates[stage] = (
                    "pending",
                    {"invalidated_by": "0_preprocess"},
                )
            with WorkspaceTransaction(workspace) as transaction:
                write_json(transaction.stage_path(write_paths["report"]), report)
                transaction.commit(updates)
            return workspace

        if committed_preprocess_result_matches(
            workspace,
            write_paths["v1"],
            write_paths["report"],
            normalized,
            report,
            input_relative,
        ):
            return workspace

        with WorkspaceTransaction(workspace) as transaction:
            write_utf8(transaction.stage_path(write_paths["v1"]), normalized)
            write_json(transaction.stage_path(write_paths["report"]), report)
            transaction.commit(
                {
                    "0_preprocess": (
                        "done",
                        {
                            "input": input_relative,
                            "output": "versions/v1_preprocessed.txt",
                            "report": "report/preprocess_report.json",
                        },
                    )
                }
            )
    return workspace


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create workspace and preprocess novel text."
    )
    parser.add_argument("source", help="Path to the original novel text file.")
    parser.add_argument("--workspace", help="Optional explicit cleanwork directory.")
    parser.add_argument(
        "--encoding",
        help=(
            "Explicit source encoding. Supported values: "
            + ", ".join(EXPLICIT_ENCODINGS)
            + "."
        ),
    )
    parser.add_argument(
        "--use-prepared-input",
        action="store_true",
        help=(
            "Read versions/v0_prepared_input.txt created by the explicit input "
            "repair workflow; never enabled automatically."
        ),
    )
    args = parser.parse_args()

    workspace = run(
        Path(args.source),
        args.workspace,
        args.encoding,
        args.use_prepared_input,
    )
    report = read_utf8(workspace / "report" / "preprocess_report.json")
    print(f"workspace: {workspace}")
    print(report)
    if json.loads(report)["encoding_detection"]["blocked"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
