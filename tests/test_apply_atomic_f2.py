from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import preprocess  # noqa: E402
from support_provenance import run_isolated_apply  # noqa: E402


class ApplyAtomicF2Tests(unittest.TestCase):
    def make_workspace(self, root: Path) -> tuple[Path, str]:
        source = root / "sample-a.txt"
        source.write_text("正文\n广告甲\n中段\n广告乙\n尾声\n", encoding="utf-8")
        workspace = preprocess.run(source)
        text = (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8")
        return workspace, text

    def anchor(self, text: str, original: str, anchor_id: str) -> dict:
        start = text.index(original)
        return {
            "anchor_id": anchor_id,
            "offset": start,
            "end": start + len(original),
            "original": original,
            "prefix": text[max(0, start - 3) : start],
            "suffix": text[start + len(original) : start + len(original) + 3],
            "splice_strategy": "exact",
        }

    def decision(self, text: str, candidate_id: str, fingerprint: str, anchors: list[dict]) -> dict:
        return {
            "scan_id": "a" * 64,
            "candidate_id": candidate_id,
            "candidate_fingerprint": fingerprint,
            "verdict": "delete",
            "confidence": 0.99,
            "reason": "测试广告",
            "anchors": anchors,
            "anchors_truncated": False,
        }

    def write_decisions(self, workspace: Path, decisions: list[dict]) -> None:
        common.write_jsonl(workspace / "decisions/ads_decisions.jsonl", decisions)

    def run_apply(self, workspace: Path, stage: str = "2_ads") -> dict:
        return run_isolated_apply(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "decisions/ads_decisions.jsonl",
            "versions/v2_ads_removed.txt",
            stage,
        )

    def assert_rejected_without_mutation(self, mutate) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, text = self.make_workspace(Path(directory))
            decisions = [
                self.decision(
                    text,
                    "AD-0001",
                    "b" * 64,
                    [self.anchor(text, "广告甲", "anchor-a")],
                )
            ]
            mutate(decisions, text)
            self.write_decisions(workspace, decisions)
            output = workspace / "versions/v2_ads_removed.txt"
            operations = workspace / "logs/operations.jsonl"
            anomalies = workspace / "logs/anomalies.jsonl"
            output.write_text("old output\n", encoding="utf-8")
            common.write_jsonl(operations, [{"run_id": "old", "anchor_id": "old"}])
            common.write_jsonl(anomalies, [{"run_id": "old", "message": "old"}])
            before = {
                output: output.read_bytes(),
                operations: operations.read_bytes(),
                anomalies: anomalies.read_bytes(),
                workspace / "manifest.json": (workspace / "manifest.json").read_bytes(),
            }

            with self.assertRaisesRegex(ValueError, "anchor|overlap|strategy|truncated|duplicate|stage"):
                self.run_apply(workspace)

            for path, expected in before.items():
                self.assertEqual(path.read_bytes(), expected)

    def test_any_invalid_anchor_rejects_the_whole_apply(self) -> None:
        def mutate(decisions: list[dict], text: str) -> None:
            decisions[0]["anchors"].append(
                {
                    **self.anchor(text, "广告乙", "anchor-b"),
                    "original": "不存在的广告",
                }
            )

        self.assert_rejected_without_mutation(mutate)

    def test_duplicate_anchor_id_is_rejected(self) -> None:
        def mutate(decisions: list[dict], text: str) -> None:
            decisions[0]["anchors"].append(self.anchor(text, "广告乙", "anchor-a"))

        self.assert_rejected_without_mutation(mutate)

    def test_cross_candidate_overlap_is_rejected(self) -> None:
        def mutate(decisions: list[dict], text: str) -> None:
            decisions.append(
                self.decision(
                    text,
                    "AD-0002",
                    "c" * 64,
                    [self.anchor(text, "广告甲", "anchor-b")],
                )
            )

        self.assert_rejected_without_mutation(mutate)

    def test_truncated_candidate_is_rejected(self) -> None:
        def mutate(decisions: list[dict], _text: str) -> None:
            decisions[0]["anchors_truncated"] = True

        self.assert_rejected_without_mutation(mutate)

    def test_invalid_strategy_and_offset_are_rejected(self) -> None:
        for case in ("strategy", "negative-offset", "bad-end"):
            with self.subTest(case=case):
                def mutate(decisions: list[dict], _text: str, case: str = case) -> None:
                    anchor = decisions[0]["anchors"][0]
                    if case == "strategy":
                        anchor["splice_strategy"] = "guess-and-delete"
                    elif case == "negative-offset":
                        anchor["offset"] = -1
                    else:
                        anchor["end"] += 1

                self.assert_rejected_without_mutation(mutate)

    def test_arbitrary_stage_cannot_bypass_the_module_state_machine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, text = self.make_workspace(Path(directory))
            self.write_decisions(
                workspace,
                [
                    self.decision(
                        text,
                        "AD-0001",
                        "b" * 64,
                        [self.anchor(text, "广告甲", "anchor-a")],
                    )
                ],
            )
            manifest_before = (workspace / "manifest.json").read_bytes()

            with self.assertRaisesRegex(ValueError, "stage"):
                self.run_apply(workspace, "rollback_ads")

            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            self.assertFalse((workspace / "versions/v2_ads_removed.txt").exists())

    def test_successful_run_is_exactly_replayable_and_auditable_by_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, text = self.make_workspace(Path(directory))
            decisions = [
                self.decision(
                    text,
                    "AD-0001",
                    "b" * 64,
                    [
                        self.anchor(text, "广告甲", "anchor-a"),
                        self.anchor(text, "广告乙", "anchor-b"),
                    ],
                )
            ]
            self.write_decisions(workspace, decisions)
            common.write_jsonl(
                workspace / "logs/operations.jsonl",
                [{"run_id": "historical", "anchor_id": "old"}],
            )

            summary = self.run_apply(workspace)
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            stage = manifest["stages"]["2_ads"]
            run_id = stage["run_id"]
            current = common.load_jsonl_for_run(workspace / "logs/operations.jsonl", run_id)

            self.assertEqual(stage["active_run_id"], run_id)
            self.assertEqual({item["anchor_id"] for item in current}, {"anchor-a", "anchor-b"})
            self.assertEqual(summary["expected_anchor_ids"], ["anchor-a", "anchor-b"])
            self.assertEqual(summary["operation_anchor_ids"], ["anchor-a", "anchor-b"])
            for item in current:
                self.assertEqual(item["candidate_fingerprint"], "b" * 64)
                self.assertEqual(item["input_sha256"], summary["input_sha256"])
                self.assertEqual(item["decision_sha256"], summary["decision_sha256"])
                self.assertEqual(item["output_sha256"], summary["output_sha256"])

            replayed = text
            for item in sorted(current, key=lambda value: value["start"], reverse=True):
                replayed = replayed[: item["start"]] + item["replacement"] + replayed[item["end"] :]
            self.assertEqual(
                replayed,
                (workspace / "versions/v2_ads_removed.txt").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
