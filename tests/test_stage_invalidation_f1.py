from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import preprocess  # noqa: E402


PIPELINE_ORDER = (
    "0_preprocess",
    "1_parse_structure",
    "2_ads",
    "3_titles",
    "4_blocked_words",
    "5_layout",
    "6_verify",
    "7_export",
    "dry_run",
    "review",
)

EXPECTED = {
    "0_preprocess": PIPELINE_ORDER[1:],
    "1_parse_structure": PIPELINE_ORDER[2:],
    "2_ads": PIPELINE_ORDER[3:],
    "3_titles": PIPELINE_ORDER[4:],
    "4_blocked_words": PIPELINE_ORDER[5:],
    "5_layout": ("6_verify", "7_export", "review"),
    "6_verify": ("7_export", "review"),
    "7_export": ("review",),
    "rollback_all": PIPELINE_ORDER,
    "rollback_ads": PIPELINE_ORDER[2:],
    "rollback_ads_chapter_7": PIPELINE_ORDER[2:],
}


class StageInvalidationF1Tests(unittest.TestCase):
    def make_workspace(self, root: Path) -> Path:
        source = root / "sample-a.txt"
        source.write_text("第一章 起点\n匿名正文。\n", encoding="utf-8")
        return preprocess.run(source)

    def activate(self, workspace: Path, stage: str, content: str = "active") -> None:
        target = workspace / "report" / f"invalidation-{stage}.json"
        with common.WorkspaceTransaction(workspace) as transaction:
            common.write_json(
                transaction.stage_path(target),
                {"stage": stage, "content": content},
            )
            transaction.commit({stage: ("done", {"report": target.relative_to(workspace).as_posix()})})

    def trigger(self, workspace: Path, stage: str, status: str = "done", content: str = "changed") -> str:
        safe_stage = stage.replace("/", "-")
        target = workspace / "report" / f"trigger-{safe_stage}.json"
        with common.WorkspaceTransaction(workspace) as transaction:
            common.write_json(
                transaction.stage_path(target),
                {"stage": stage, "content": content},
            )
            run_id = transaction.run_id
            transaction.commit({stage: (status, {"report": target.relative_to(workspace).as_posix()})})
        return run_id

    def read_manifest(self, workspace: Path) -> dict:
        return json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))

    def test_matrix_covers_every_planned_upstream_change(self) -> None:
        for stage, expected in EXPECTED.items():
            with self.subTest(stage=stage):
                self.assertEqual(common.stage_invalidation_targets(stage), expected)

    def test_every_changed_upstream_artifact_invalidates_its_active_targets(self) -> None:
        cases = (
            ("0_preprocess", "done"),
            ("1_parse_structure", "done"),
            ("2_ads", "candidates_ready"),
            ("2_ads", "formal_decisions_ready"),
            ("2_ads", "done"),
            ("3_titles", "candidates_ready"),
            ("4_blocked_words", "candidates_ready"),
            ("5_layout", "done"),
            ("6_verify", "passed"),
            ("7_export", "done"),
            ("rollback_all", "done"),
            ("rollback_ads", "done"),
            ("rollback_ads_chapter_7", "done"),
        )
        for trigger_stage, trigger_status in cases:
            with self.subTest(stage=trigger_stage, status=trigger_status):
                with tempfile.TemporaryDirectory() as directory:
                    workspace = self.make_workspace(Path(directory))
                    targets = EXPECTED[trigger_stage]
                    for stage in PIPELINE_ORDER:
                        if stage in targets and stage != "0_preprocess":
                            self.activate(workspace, stage)

                    run_id = self.trigger(workspace, trigger_stage, trigger_status)
                    stages = self.read_manifest(workspace)["stages"]

                    for stage in targets:
                        self.assertEqual(stages[stage]["status"], "pending")
                        self.assertEqual(stages[stage]["invalidated_by"], trigger_stage)
                        self.assertEqual(stages[stage]["invalidated_by_run_id"], run_id)
                        self.assertNotIn("artifacts", stages[stage])
                        self.assertNotIn("attestation", stages[stage])
                        self.assertNotIn("run_id", stages[stage])

    def test_identical_upstream_artifact_does_not_invalidate_current_attestations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_workspace(Path(directory))
            self.trigger(workspace, "2_ads", "candidates_ready", "same")
            for stage in ("5_layout", "6_verify", "7_export", "review"):
                self.activate(workspace, stage)

            self.trigger(workspace, "2_ads", "candidates_ready", "same")
            stages = self.read_manifest(workspace)["stages"]

            for stage in ("5_layout", "6_verify", "7_export", "review"):
                self.assertEqual(stages[stage]["status"], "done")


if __name__ == "__main__":
    unittest.main()
