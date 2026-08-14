from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_blocked  # noqa: E402
import scan_titles  # noqa: E402


class ReportOnlyF4Tests(unittest.TestCase):
    def test_default_title_and_blocked_scans_publish_no_executable_suggestion(self) -> None:
        title_candidates, title_summary = scan_titles.scan_text("第一章\n短标题\n这是正文。\n")
        blocked_candidates, _ = scan_blocked.scan_text("这是被屏*蔽的正文。\n")

        self.assertTrue(title_candidates)
        self.assertTrue(blocked_candidates)
        self.assertFalse(title_summary["execution_suggestions_enabled"])
        title_messages = " ".join(str(candidate["message"]) for candidate in title_candidates).casefold()
        self.assertNotIn("user confirm", title_messages)
        self.assertNotIn("unless user", title_messages)
        for candidate in [*title_candidates, *blocked_candidates]:
            self.assertTrue(candidate["report_only"])
            suggestion = candidate["suggested_decision"]
            self.assertNotIn("action", suggestion)
            self.assertNotIn("replacement", suggestion)
            self.assertNotIn("anchors", suggestion)
            reason = suggestion["reason"].casefold()
            self.assertIn("report-only in this release", reason)
            self.assertIn("no supported", reason)
            for misleading in ("approval", "repair", "restore"):
                self.assertNotIn(misleading, reason)

    def test_scanning_workspace_does_not_change_the_current_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "anonymous.txt"
            source.write_text(
                "第一章\n短标题\n这是被屏*蔽的正文。\n",
                encoding="utf-8",
            )
            workspace = preprocess.run(source)
            parse_structure.run(workspace)
            current = common.resolve_current_head(workspace)
            before = current.read_bytes()

            scan_titles.run(workspace, "auto", "candidates/titles.jsonl")
            scan_blocked.run(workspace, "auto", "candidates/blocked.jsonl", 20)

            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(current.read_bytes(), before)
            self.assertEqual(
                manifest["artifacts"][manifest["current_head"]]["sha256"],
                common.sha256_file(current),
            )
            self.assertFalse(any((workspace / "logs").glob("*operations*.jsonl")))


if __name__ == "__main__":
    unittest.main()
