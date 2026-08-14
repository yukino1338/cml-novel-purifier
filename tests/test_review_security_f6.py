from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_review_html as review  # noqa: E402
from tests.test_build_review_html_f6 import workspace_item  # noqa: E402


def executable_projection_keys(value: object) -> set[str]:
    forbidden = {
        "offset",
        "end",
        "start",
        "range",
        "ranges",
        "segments",
        "spans",
        "edit_plan",
        "relative_start",
        "relative_end",
        "parent_start",
        "parent_end",
        "free_offset",
    }
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in forbidden or normalized.endswith(("_offset", "_start", "_end")):
                found.add(str(key))
            found.update(executable_projection_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(executable_projection_keys(child))
    return found


def review_item(candidate_id: str, original: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "module": "ads",
        "risk": "high",
        "draft_verdict": "uncertain",
        "formal_decision": "uncertain",
        "chapter": {"index": 1, "title": "第一章"},
        "anchors_count": 1,
        "anchors": [{"offset": 1, "prefix": "前", "original": original, "suffix": "后"}],
        "before": "前文",
        "original": original,
        "after": "后文",
        "needs_review": True,
        "review_reasons": ["Agent 正式结论仍为未决"],
        "rollback": {},
    }


class ReviewSecurityF6Tests(unittest.TestCase):
    def test_hostile_json_cannot_close_the_data_script_and_round_trips(self) -> None:
        hostile = "</ScRiPt><img src=x onerror='globalThis.pwned=1'>\"'`$();\n下一行"
        item = workspace_item("敌意样本", "needs-review", formal_uncertain=2)
        items = [review_item("a/b", hostile), review_item("a?b", hostile)]
        items[0]["evidence"] = {
            "relative_start": 4,
            "relative_end": 9,
            "nested": {"free_offset": 11},
        }
        items[0]["custom_metadata"] = {"segment_start": 3, "segment_end": 8}
        item["modules"]["ads"]["review_items"] = items

        page = review.render_html(
            {"mode": "single", "workspaces": [item], "summary": item["summary"]},
            hostile,
        )

        payload_match = re.search(
            r"<script type='application/json' class='review-payload'>(.*?)</script>",
            page,
            re.S,
        )
        self.assertIsNotNone(payload_match)
        payload_text = payload_match.group(1)  # type: ignore[union-attr]
        payload = json.loads(payload_text)
        self.assertEqual(payload["modules"]["ads"]["items"][0]["original"], hostile)
        self.assertEqual(executable_projection_keys(payload), set())
        self.assertNotIn("<", payload_text)
        shell = page.split("<script type='application/json'", 1)[0]
        self.assertNotIn("<img", shell.lower())
        self.assertIn("&lt;img", shell.lower())
        self.assertEqual(page.lower().count("</script>"), 2)
        self.assertIn("review_state_id", page)
        self.assertIn("CSS.escape(candidateId)", page)
        self.assertNotIn("candidate_id || \"candidate\").replace", page)

    def test_review_candidate_embeds_only_bounded_marked_excerpts(self) -> None:
        hidden = "PRIVATE-MIDDLE-MARKER"
        long_original = "头" * 3_000 + hidden + "尾" * 3_000
        long_before = "远" * 700 + "近" * 700
        long_after = "近" * 700 + "远" * 700
        anchors = [
            {
                "offset": index,
                "prefix": "前" * 300,
                "original": long_original,
                "suffix": "后" * 300,
            }
            for index in range(25)
        ]
        candidate = {
            "candidate_id": "AD-BOUND",
            "layer": "L3",
            "signals": ["url"],
            "contexts": [{"before": long_before, "original": long_original, "after": long_after}],
            "anchors": anchors,
            "evidence": ["证据" * 2_000 for _ in range(25)],
        }
        bounded = review.review_candidate(
            Path("anonymous.cleanwork"),
            "ads",
            candidate,
            {"verdict": "uncertain", "reason": "草稿" * 2_000},
            {"verdict": "uncertain", "reason": "正式" * 2_000, "blockers": ["阻止" * 2_000] * 25},
            None,
        )

        self.assertEqual(len(bounded["before"]), review.MAX_REVIEW_CONTEXT_CHARS)
        self.assertTrue(bounded["before"].startswith("…"))
        self.assertEqual(len(bounded["after"]), review.MAX_REVIEW_CONTEXT_CHARS)
        self.assertTrue(bounded["after"].endswith("…"))
        self.assertEqual(len(bounded["original"]), review.MAX_REVIEW_ORIGINAL_CHARS)
        self.assertNotIn(hidden, bounded["original"])
        self.assertTrue(bounded["excerpt_truncated"])
        self.assertTrue(bounded["anchors_truncated"])
        self.assertTrue(bounded["metadata_truncated"])
        self.assertEqual(len(bounded["anchors"]), review.MAX_REVIEW_ANCHORS)
        self.assertTrue(all(len(anchor["original"]) <= review.MAX_REVIEW_ANCHOR_ORIGINAL_CHARS for anchor in bounded["anchors"]))
        self.assertEqual(executable_projection_keys(bounded), set())
        self.assertLessEqual(len(bounded["evidence"]), review.MAX_REVIEW_METADATA_ITEMS)

        item = workspace_item("有界摘录", "needs-review", formal_uncertain=1)
        item["modules"]["ads"]["review_items"] = [bounded]
        page = review.render_html(
            {"mode": "single", "workspaces": [item], "summary": item["summary"]},
            "测试结果",
        )
        self.assertNotIn(hidden, page)
        self.assertIn("网页仅展示有界摘录", page)

    def test_posix_and_powershell_commands_preserve_hostile_arguments(self) -> None:
        workspace = Path("C:/books/含'引号 ` $() ;\n换行.txt.cleanwork")
        candidate_id = "AD-' ` $() ;\n下一行"
        expected = [
            "python",
            "scripts/rollback.py",
            str(workspace),
            "--level",
            "point",
            "--module",
            "ads",
            "--candidate-id",
            candidate_id,
        ]
        commands = review.rollback_commands(workspace, "ads", "point", candidate_id)
        self.assertEqual(shlex.split(commands["posix"], posix=True), expected)
        with self.assertRaises(ValueError):
            review.shell_quote(candidate_id, "unknown")

        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell parser is unavailable")
        parser_script = r"""
param([string]$InputPath)
$source = Get-Content -Raw -Encoding UTF8 -LiteralPath $InputPath
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
$commands = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true))
if ($commands.Count -ne 1) { throw "expected exactly one command" }
@($commands[0].CommandElements | ForEach-Object { $_.SafeGetValue() }) | ConvertTo-Json -Compress
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command_path = root / "command.txt"
            parser_path = root / "parse.ps1"
            command_path.write_bytes(commands["powershell"].encode("utf-8"))
            parser_path.write_text(parser_script, encoding="utf-8-sig")
            completed = subprocess.run(
                [powershell, "-NoProfile", "-File", str(parser_path), str(command_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), expected)


if __name__ == "__main__":
    unittest.main()
