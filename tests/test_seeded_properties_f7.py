from __future__ import annotations

import copy
import json
import random
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_decisions  # noqa: E402
import common  # noqa: E402
import normalize_layout  # noqa: E402


APPLY_SEED = 2026071501
LAYOUT_SEED = 2026071502
MALFORMED_SEED = 2026071503
APPLY_CASES = 4_000
LAYOUT_CASES = 4_000
MALFORMED_CASES = 2_400
RELEASE_CASES = APPLY_CASES + LAYOUT_CASES + MALFORMED_CASES


def case_label(seed: int, index: int, domain: str) -> str:
    return f"domain={domain} seed={seed} case_index={index}"


def outside_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class SeededPropertiesF7Tests(unittest.TestCase):
    def assert_expected_rejection(
        self,
        call: Callable[[], Any],
        expected: type[Exception] | tuple[type[Exception], ...],
        label: str,
    ) -> None:
        try:
            call()
        except expected:
            return
        except Exception as exc:  # pragma: no cover - failure classification
            self.fail(f"{label}: unclassified {type(exc).__name__}: {exc}")
        self.fail(f"{label}: malformed input was accepted")

    def test_release_seed_budget_is_at_least_ten_thousand(self) -> None:
        self.assertGreaterEqual(RELEASE_CASES, 10_000)

    def test_non_overlapping_operations_are_input_order_independent(self) -> None:
        rng = random.Random(APPLY_SEED)
        for index in range(APPLY_CASES):
            label = case_label(APPLY_SEED, index, "operations")
            repeated = rng.choice(("重复段落。", "same paragraph!", "标点，。！？"))
            paragraphs = [
                f"中文正文甲{rng.randrange(10_000)}。",
                f"ASCII prose {rng.randrange(10_000)} v1.2.3!",
                f"标点测试，。！？；：{rng.randrange(10_000)}",
                repeated,
                repeated,
            ]
            rng.shuffle(paragraphs)
            text = "\n".join(paragraphs) + "\n"

            offsets: list[tuple[int, int, str]] = []
            cursor = 0
            for paragraph in paragraphs:
                start = cursor
                end = start + len(paragraph)
                offsets.append((start, end, paragraph))
                cursor = end + 1

            selected = sorted(rng.sample(range(len(offsets)), rng.choice((2, 3))))
            decisions: list[dict[str, Any]] = []
            expected_splices: list[tuple[int, int, str]] = []
            for slot, paragraph_index in enumerate(selected):
                start, end, original = offsets[paragraph_index]
                strategy = rng.choice(("exact", "fallback_newline", "remove_paragraph"))
                replacement = "\n" if strategy == "fallback_newline" else ""
                anchor: dict[str, Any] = {
                    "anchor_id": f"anchor-{index}-{slot}",
                    "offset": start,
                    "end": end,
                    "original": original,
                    "prefix": text[max(0, start - 2) : start],
                    "suffix": text[end : end + 2],
                    "splice_strategy": strategy,
                }
                decision: dict[str, Any] = {
                    "candidate_id": f"candidate-{index}-{slot}",
                    "candidate_fingerprint": f"fingerprint-{index}-{slot}",
                    "scan_id": f"scan-{index}",
                    "anchors_truncated": False,
                    "anchors": [anchor],
                }
                if rng.choice((True, False)):
                    decision["verdict"] = "delete"
                else:
                    decision["action"] = "delete"
                decisions.append(decision)
                splice_end = end + 1 if strategy == "remove_paragraph" else end
                expected_splices.append((start, splice_end, replacement))

            first_order = list(decisions)
            second_order = list(decisions)
            rng.shuffle(first_order)
            rng.shuffle(second_order)
            try:
                first_operations = apply_decisions.collect_operations(
                    text,
                    first_order,
                    Path("unused-anomalies.jsonl"),
                    "ads",
                )
                second_operations = apply_decisions.collect_operations(
                    text,
                    second_order,
                    Path("unused-anomalies.jsonl"),
                    "ads",
                )
                first_result = apply_decisions.apply_operations(text, first_operations)
                second_result = apply_decisions.apply_operations(
                    text,
                    list(reversed(second_operations)),
                )
            except Exception as exc:  # pragma: no cover - property failure detail
                self.fail(f"{label}: unclassified {type(exc).__name__}: {exc}")

            expected = text
            for start, end, replacement in sorted(expected_splices, reverse=True):
                expected = expected[:start] + replacement + expected[end:]
            self.assertEqual(first_result, expected, label)
            self.assertEqual(second_result, expected, label)

    def test_safe_layout_is_idempotent_for_seeded_mixed_text(self) -> None:
        rng = random.Random(LAYOUT_SEED)
        for index in range(LAYOUT_CASES):
            label = case_label(LAYOUT_SEED, index, "layout")
            repeated = rng.choice(("重复段落。", "Repeated paragraph!", "标点,继续?"))
            lines = [
                "第一章 起点",
                f"中文正文{rng.randrange(10_000)},继续!真的可以?",
                f"ASCII token {rng.randrange(10_000)} v1.2.3 name@example.com",
                f"标点，。！？；：{rng.randrange(10_000)}",
                repeated,
                repeated,
            ]
            rng.shuffle(lines)
            expanded: list[str] = []
            for line in lines:
                expanded.append(rng.choice(("", " ", "\t", "　")) + line)
                if rng.randrange(3) == 0:
                    expanded.extend([""] * rng.randrange(1, 4))
            newline = rng.choice(("\n", "\r\n", "\r"))
            text = newline.join(expanded)
            if rng.choice((True, False)):
                text += newline

            config = copy.deepcopy(normalize_layout.DEFAULT_CONFIG)
            config["layout"].update(
                {
                    "enabled": True,
                    "indent": rng.choice(("none", "two_fullwidth_spaces")),
                    "collapse_blank_lines": rng.choice((True, False)),
                    "max_blank_lines": rng.randrange(0, 4),
                    "punctuation_mode": rng.choice(("none", "safe_chinese")),
                    "trim_trailing_space": rng.choice((True, False)),
                    "normalize_ascii_space": rng.choice((True, False)),
                }
            )
            try:
                once, _ = normalize_layout.normalize_text(text, config)
                twice, _ = normalize_layout.normalize_text(once, config)
            except Exception as exc:  # pragma: no cover - property failure detail
                self.fail(f"{label}: unclassified {type(exc).__name__}: {exc}")
            self.assertEqual(twice, once, label)

    def test_malformed_jsonl_config_and_paths_fail_safely(self) -> None:
        rng = random.Random(MALFORMED_SEED)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "anonymous.cleanwork"
            workspace.mkdir()
            fuzz_inputs = root / "fuzz-inputs"
            fuzz_inputs.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "sentinel.bin").write_bytes(b"outside-sentinel-v1")
            before = outside_snapshot(outside)
            jsonl_path = fuzz_inputs / "malformed.jsonl"
            config_path = fuzz_inputs / "malformed-config.json"

            path_patterns = (
                "../outside/empty-{token}.txt",
                "../outside/escape-{token}.txt",
                "versions/../outside/escape-{token}.txt",
                "/absolute/escape-{token}.txt",
                r"C:\absolute\escape-{token}.txt",
                r"C:drive-relative-{token}.txt",
                "report/value-{token}.json:stream",
                "report/trailing-{token}.",
                "report/trailing-{token} ",
                "report/has-{token}\x00nul.json",
                r"\\server\share\escape-{token}.txt",
                ".runs/forbidden-{token}.json",
            )
            seen_cases: set[tuple[str, str]] = set()

            for index in range(MALFORMED_CASES):
                domain = ("jsonl", "config", "path")[index % 3]
                label = case_label(MALFORMED_SEED, index, domain)
                token = f"{index:04d}-{rng.getrandbits(64):016x}"
                if domain == "jsonl":
                    variant = rng.randrange(8)
                    if variant == 0:
                        payload = f'{{"case_{token}":'
                    elif variant == 1:
                        payload = json.dumps([{"token": token}]) + "\n"
                    elif variant == 2:
                        payload = json.dumps(f"scalar-{token}") + "\n"
                    elif variant == 3:
                        payload = (
                            json.dumps({"ok": 1, "token": token})
                            + f"\nnot-json-{token}\n"
                        )
                    elif variant == 4:
                        payload = '{"bad":"\\q","token":"' + token + '"}\n'
                    elif variant == 5:
                        payload = f"{{]{token}\n"
                    elif variant == 6:
                        payload = f"null {token}\n"
                    else:
                        payload = f"{index + 1}\n"
                    case_key = (domain, payload)
                    self.assertNotIn(case_key, seen_cases, label)
                    seen_cases.add(case_key)
                    jsonl_path.write_text(payload, encoding="utf-8")
                    self.assert_expected_rejection(
                        lambda: common.load_jsonl(jsonl_path),
                        ValueError,
                        label,
                    )
                    continue

                if domain == "config":
                    variant = rng.randrange(12)
                    if variant == 0:
                        config_path.write_text(
                            f'{{"case":"{token}"',
                            encoding="utf-8",
                        )
                    elif variant == 1:
                        config_path.write_text(
                            json.dumps([token]),
                            encoding="utf-8",
                        )
                    else:
                        config = copy.deepcopy(normalize_layout.DEFAULT_CONFIG)
                        config["export"]["title"] = f"case-{token}"
                        mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
                            lambda value: value.__setitem__("unsupported", True),
                            lambda value: value.__setitem__("modules", []),
                            lambda value: value["layout"].__setitem__("enabled", 1),
                            lambda value: value["layout"].__setitem__("indent", "guess"),
                            lambda value: value["layout"].__setitem__("max_blank_lines", -1),
                            lambda value: value["layout"].__setitem__(
                                "punctuation_mode", "all"
                            ),
                            lambda value: value["conversion"].__setitem__("mode", "auto"),
                            lambda value: value["conversion"].__setitem__("engine", "unknown"),
                            lambda value: value["export"].__setitem__("language", " "),
                            lambda value: value["export"].__setitem__("txt", "yes"),
                        )
                        mutations[variant - 2](config)
                        config_path.write_text(
                            json.dumps(config, ensure_ascii=False),
                            encoding="utf-8",
                        )
                    config_payload = config_path.read_text(encoding="utf-8")
                    case_key = (domain, config_payload)
                    self.assertNotIn(case_key, seen_cases, label)
                    seen_cases.add(case_key)
                    self.assert_expected_rejection(
                        lambda: normalize_layout.load_config(config_path),
                        ValueError,
                        label,
                    )
                    continue

                value = rng.choice(path_patterns).format(token=token)
                case_key = (domain, value)
                self.assertNotIn(case_key, seen_cases, label)
                seen_cases.add(case_key)
                self.assert_expected_rejection(
                    lambda value=value: common.resolve_workspace_paths(
                        workspace,
                        writes={"output": value},
                        allow_missing_workspace=True,
                    ),
                    common.WorkspacePathError,
                    label,
                )

            self.assertEqual(len(seen_cases), MALFORMED_CASES)
            self.assertEqual(
                outside_snapshot(outside),
                before,
                case_label(MALFORMED_SEED, MALFORMED_CASES - 1, "outside-sentinel"),
            )


if __name__ == "__main__":
    unittest.main()
