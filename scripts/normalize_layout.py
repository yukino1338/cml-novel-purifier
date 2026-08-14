from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from common import (
    WorkspaceTransaction,
    read_utf8,
    resolve_current_head,
    resolve_workspace_paths,
    sha256_file,
    workspace_transaction_lock,
    write_json,
    write_utf8,
)
from parse_structure import match_chapter


DEFAULT_CONFIG: dict[str, Any] = {
    "layout": {
        "enabled": False,
        "indent": "two_fullwidth_spaces",
        "collapse_blank_lines": True,
        "max_blank_lines": 1,
        "punctuation_mode": "none",
        "trim_trailing_space": True,
        "normalize_ascii_space": False,
    },
    "conversion": {
        "mode": "none",
    },
    "export": {
        "title": "",
        "author": "",
        "language": "zh-CN",
    },
}

CONFIG_KEYS = frozenset(DEFAULT_CONFIG)
LAYOUT_KEYS = frozenset(DEFAULT_CONFIG["layout"])
CONVERSION_KEYS = frozenset(DEFAULT_CONFIG["conversion"])
EXPORT_KEYS = frozenset(DEFAULT_CONFIG["export"])
PROTECTED_SPAN_RE = re.compile(
    r"(`[^`\n]*`|https?://[^\s]+|www\.[^\s]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"[A-Za-z]:\\[^\s]+)",
    re.IGNORECASE,
)
SAFE_PUNCTUATION = {",": "，", ".": "。", "?": "？", "!": "！", ":": "：", ";": "；"}
CLOSING_QUOTES = frozenset('"\'”’」』）】》')


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _require_object(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"config {key} must be an object")
    return value


def _reject_unknown(value: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"config {label} has unsupported option(s): {', '.join(unknown)}")


def _require_bool(value: dict[str, Any], key: str, label: str) -> None:
    if not isinstance(value.get(key), bool):
        raise ValueError(f"config {label}.{key} must be boolean")


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("config root must be an object")
    _reject_unknown(config, CONFIG_KEYS, "root")
    layout = _require_object(config, "layout")
    conversion = _require_object(config, "conversion")
    export = _require_object(config, "export")
    _reject_unknown(layout, LAYOUT_KEYS, "layout")
    _reject_unknown(conversion, CONVERSION_KEYS, "conversion")
    _reject_unknown(export, EXPORT_KEYS, "export")
    for key in (
        "enabled",
        "collapse_blank_lines",
        "trim_trailing_space",
        "normalize_ascii_space",
    ):
        _require_bool(layout, key, "layout")
    if layout.get("indent") not in {"none", "two_fullwidth_spaces"}:
        raise ValueError("config layout.indent is unsupported")
    maximum = layout.get("max_blank_lines")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 0 <= maximum <= 10:
        raise ValueError("config layout.max_blank_lines must be an integer from 0 to 10")
    if layout.get("punctuation_mode") not in {"none", "safe_chinese"}:
        raise ValueError("config layout.punctuation_mode is unsupported")

    if conversion.get("mode") not in {"none", "traditional", "simplified"}:
        raise ValueError("config conversion.mode is unsupported")
    for key in ("title", "author", "language"):
        if not isinstance(export.get(key), str):
            raise ValueError(f"config export.{key} must be a string")
    if not export["language"].strip():
        raise ValueError("config export.language cannot be empty")
    return config


def _load_config_file(
    path: Path,
    input_paths: set[Path] | None,
    seen: set[Path],
) -> dict[str, Any]:
    path = path.resolve(strict=False)
    if path in seen:
        raise ValueError("config inheritance cycle detected")
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    seen.add(path)
    if input_paths is not None:
        input_paths.add(path)
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("config file root must be an object")
    parent = raw.pop("inherits", None)
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise ValueError("config inherits must be a non-empty string")
    base = (
        _load_config_file(path.parent / parent, input_paths, seen)
        if isinstance(parent, str)
        else DEFAULT_CONFIG
    )
    merged = deep_merge(base, raw)
    seen.remove(path)
    return merged


def load_config(path: Path | None, input_paths: set[Path] | None = None) -> dict[str, Any]:
    selected = path if path is not None else skill_root() / "assets/config-templates/default.json"
    return validate_config(_load_config_file(selected, input_paths, set()))


def choose_input(workspace: Path, value: str) -> str:
    if value != "auto":
        return value
    return resolve_current_head(workspace).relative_to(Path(workspace).resolve()).as_posix()


def _is_cjk(char: str) -> bool:
    return bool(char) and ("\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff")


def normalize_punctuation(line: str) -> str:
    output: list[str] = []
    for index, character in enumerate(line):
        replacement = SAFE_PUNCTUATION.get(character)
        if replacement is None:
            output.append(character)
            continue
        previous = line[index - 1] if index else ""
        following = line[index + 1] if index + 1 < len(line) else ""
        safe_following = not following or following.isspace() or _is_cjk(following) or following in CLOSING_QUOTES
        output.append(replacement if _is_cjk(previous) and safe_following else character)
    return "".join(output)


def normalize_ascii_spaces(line: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in PROTECTED_SPAN_RE.finditer(line):
        parts.append(re.sub(r"[ \t]+", " ", line[cursor : match.start()]))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(re.sub(r"[ \t]+", " ", line[cursor:]))
    return "".join(parts)


def normalize_line(line: str, config: dict[str, Any], metrics: dict[str, int]) -> str:
    original = line
    layout = config["layout"]
    if layout["trim_trailing_space"]:
        line = line.rstrip(" \t\u3000")
    if layout["normalize_ascii_space"]:
        line = normalize_ascii_spaces(line)
    if layout["punctuation_mode"] == "safe_chinese":
        line = normalize_punctuation(line)
    if line.strip() and match_chapter(line) is None:
        stripped = line.lstrip(" \t\u3000")
        line = "　　" + stripped if layout["indent"] == "two_fullwidth_spaces" else stripped
    if line != original:
        metrics["changed_lines"] += 1
    return line


def collapse_blank_lines(lines: list[str], max_blank_lines: int) -> tuple[list[str], int]:
    result: list[str] = []
    consecutive = 0
    removed = 0
    for line in lines:
        if not line.strip():
            consecutive += 1
            if consecutive <= max_blank_lines:
                result.append("")
            else:
                removed += 1
        else:
            consecutive = 0
            result.append(line)
    return result, removed


def convert_script(text: str, config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    mode = config["conversion"]["mode"]
    if mode == "none":
        return text, {"mode": "none", "engine": None, "warning": None}
    try:
        from opencc import OpenCC  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "OpenCC is required for script conversion; no partial fallback is used"
        ) from exc
    converted = OpenCC("s2t" if mode == "traditional" else "t2s").convert(text)
    return converted, {"mode": mode, "engine": "opencc", "warning": None}


def normalize_text(text: str, config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    config = validate_config(config)
    if not config["layout"]["enabled"]:
        converted, conversion_report = convert_script(text, config)
        return converted, {
            "layout_profile": "preserve",
            "layout_enabled": False,
            "changed_lines": 0,
            "blank_lines_removed": 0,
            "conversion": conversion_report,
        }

    metrics = {"changed_lines": 0}
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [normalize_line(line, config, metrics) for line in lines]
    removed = 0
    if config["layout"]["collapse_blank_lines"]:
        lines, removed = collapse_blank_lines(lines, config["layout"]["max_blank_lines"])
    normalized = "\n".join(lines)
    if not normalized.endswith("\n"):
        normalized += "\n"
    converted, conversion_report = convert_script(normalized, config)
    return converted, {
        "layout_profile": "normalize",
        "layout_enabled": True,
        "changed_lines": metrics["changed_lines"],
        "blank_lines_removed": removed,
        "conversion": conversion_report,
    }


def run(
    workspace: Path,
    input_value: str,
    output_value: str,
    config_path: Path | None,
) -> dict[str, Any]:
    with workspace_transaction_lock(workspace):
        return _run_locked(workspace, input_value, output_value, config_path)


def _run_locked(
    workspace: Path,
    input_value: str,
    output_value: str,
    config_path: Path | None,
) -> dict[str, Any]:
    config_inputs: set[Path] = set()
    config = load_config(config_path, config_inputs)
    selected_input = choose_input(workspace, input_value)
    workspace, reads, writes = resolve_workspace_paths(
        workspace,
        reads={"input": selected_input},
        writes={"output": output_value, "report": "report/layout_report.json"},
        protected_paths=config_inputs,
    )
    input_path = reads["input"]
    output_path = writes["output"]
    normalized, metrics = normalize_text(read_utf8(input_path), config)
    input_sha256 = sha256_file(input_path)
    config_sha256 = hashlib.sha256(
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with WorkspaceTransaction(workspace) as transaction:
        staged_output = transaction.stage_path(output_path)
        write_utf8(staged_output, normalized)
        report = {
            "input": input_path.relative_to(workspace).as_posix(),
            "output": output_path.relative_to(workspace).as_posix(),
            "input_sha256": input_sha256,
            "output_sha256": sha256_file(staged_output),
            "metrics": metrics,
            "config": config,
            "config_sha256": config_sha256,
            "active_run_id": transaction.run_id,
        }
        write_json(transaction.stage_path(writes["report"]), report)
        transaction.commit(
            {
                "5_layout": (
                    "done",
                    {
                        "input": report["input"],
                        "output": report["output"],
                        "report": "report/layout_report.json",
                        "input_sha256": report["input_sha256"],
                        "output_sha256": report["output_sha256"],
                        "config_sha256": config_sha256,
                        "active_run_id": transaction.run_id,
                    },
                )
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize low-risk novel layout into v5.")
    parser.add_argument("workspace", help="Path to the .cleanwork directory.")
    parser.add_argument("--input", default="auto")
    parser.add_argument("--output", default="versions/v5_layout_final.txt")
    parser.add_argument("--config", help="Path to JSON config template.")
    args = parser.parse_args()
    report = run(
        Path(args.workspace).resolve(),
        args.input,
        args.output,
        Path(args.config).resolve() if args.config else None,
    )
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
