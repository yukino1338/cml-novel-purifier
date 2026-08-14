from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_decisions  # noqa: E402
import common  # noqa: E402
import export_outputs  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import rollback  # noqa: E402
from tests.support_formal_ads import formalize_ads  # noqa: E402


class PreciseRollbackF3Tests(unittest.TestCase):
    shared_id = "AD/../CON:shared"

    def anchor(
        self,
        text: str,
        original: str,
        start: int,
        anchor_id: str,
        chapter: int | None,
    ) -> dict[str, object]:
        end = start + len(original)
        result: dict[str, object] = {
            "anchor_id": anchor_id,
            "offset": start,
            "end": end,
            "original": original,
            "prefix": text[max(0, start - 8) : start],
            "suffix": text[end : end + 8],
            "splice_strategy": "exact",
        }
        if chapter is None:
            result["locator"] = {"kind": "fallback_chunk", "index": 1, "title": ""}
        else:
            result["chapter"] = {"index": chapter, "title": f"第{chapter}章"}
        return result

    def make_workspace(
        self,
        root: Path,
        name: str = "rollback",
        *,
        true_chapters: bool = True,
    ) -> tuple[Path, str, list[dict[str, object]], str]:
        shared = "站外提示：https://reader.example.com/shared"
        unique = "下载提示：https://reader.example.com/unique"
        source_text = (
            "第一章 起点\n"
            "正文甲。\n"
            f"{shared}\n"
            "第二章 继续\n"
            "正文乙。\n"
            f"{shared}\n"
            f"{unique}\n"
        )
        source = root / f"{name}.txt"
        source.write_text(source_text, encoding="utf-8")
        workspace = preprocess.run(source, encoding="utf-8")
        text = (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8")
        first = text.index(shared)
        second = text.index(shared, first + 1)
        unique_start = text.index(unique)
        chapters = (1, 2, 2) if true_chapters else (None, None, None)
        candidate_specs: list[dict[str, object]] = [
            {
                "candidate_id": self.shared_id,
                "anchors": [
                    self.anchor(text, shared, first, "shared-1", chapters[0]),
                    self.anchor(text, shared, second, "shared-2", chapters[1]),
                ],
            },
            {
                "candidate_id": "AD-unique",
                "anchors": [
                    self.anchor(text, unique, unique_start, "unique-1", chapters[2]),
                ],
            },
        ]
        formalize_ads(
            workspace,
            candidate_specs,
            verdict="delete",
            action="delete",
        )
        decisions = common.load_jsonl(workspace / "decisions/ads_decisions.jsonl")
        apply_decisions.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "decisions/ads_decisions.jsonl",
            "versions/v2_ads_removed.txt",
            "2_ads",
        )
        clean_sha256 = common.sha256_file(workspace / "versions/v2_ads_removed.txt")
        return workspace, text, decisions, clean_sha256

    def text_after_deleting(self, text: str, anchors: list[dict[str, object]]) -> str:
        result = text
        for anchor in sorted(anchors, key=lambda item: int(item["offset"]), reverse=True):
            result = result[: int(anchor["offset"])] + result[int(anchor["end"]) :]
        return result

    def test_full_rollback_blocks_reuse_of_invalidated_formal_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, _text, _decisions, _clean_sha256 = self.make_workspace(
                Path(directory), "full-reapply"
            )
            source_before = Path(json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))["source"]["path"]).read_bytes()
            v0_before = (workspace / "versions/v0_original.txt").read_bytes()

            rollback.rollback_all(workspace, None, True)
            manifest_before = (workspace / "manifest.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "formal decision provenance"):
                apply_decisions.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "decisions/ads_decisions.jsonl",
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                )

            manifest = json.loads(manifest_before.decode("utf-8"))
            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            for stage in ("0_preprocess", "1_parse_structure", "2_ads"):
                self.assertEqual(manifest["stages"][stage]["status"], "pending")
            self.assertEqual(
                Path(manifest["source"]["path"]).read_bytes(), source_before
            )
            self.assertEqual(
                (workspace / "versions/v0_original.txt").read_bytes(), v0_before
            )

            source = Path(manifest["source"]["path"])
            preprocess.run(source, str(workspace), encoding="utf-8")
            parse_structure.run(workspace)
            manifest_after_prerequisites = (workspace / "manifest.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "formal decision provenance"):
                apply_decisions.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "decisions/ads_decisions.jsonl",
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                )
            self.assertEqual(
                (workspace / "manifest.json").read_bytes(),
                manifest_after_prerequisites,
            )

    def snapshot(self, workspace: Path) -> dict[str, bytes]:
        return {
            path.relative_to(workspace).as_posix(): path.read_bytes()
            for path in workspace.rglob("*")
            if path.is_file()
        }

    def test_chapter_rollback_filters_only_the_matching_cross_chapter_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, text, decisions, clean_sha256 = self.make_workspace(root)
            all_anchors = [
                anchor
                for decision in decisions
                for anchor in decision["anchors"]  # type: ignore[index]
            ]
            first_shared_anchor_id = str(decisions[0]["anchors"][0]["anchor_id"])  # type: ignore[index]
            second_shared_anchor_id = str(decisions[0]["anchors"][1]["anchor_id"])  # type: ignore[index]
            unique_anchor_id = str(decisions[1]["anchors"][0]["anchor_id"])  # type: ignore[index]
            remaining = [
                anchor
                for anchor in all_anchors
                if anchor["anchor_id"] != first_shared_anchor_id
            ]

            report = rollback.rollback_chapter(workspace, "ads", 1)

            self.assertEqual(
                (workspace / "versions/v2_ads_removed.txt").read_text(encoding="utf-8"),
                self.text_after_deleting(text, remaining),
            )
            self.assertEqual(report["restored_anchor_ids"], [first_shared_anchor_id])
            self.assertEqual(
                report["remaining_anchor_ids"],
                sorted([second_shared_anchor_id, unique_anchor_id]),
            )
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["current_head"], "versions/v2_ads_removed.txt")
            for stage in ("2_ads", "5_layout", "6_verify", "7_export", "review"):
                self.assertEqual(manifest["stages"].get(stage, {}).get("status", "pending"), "pending")
            with self.assertRaisesRegex(ValueError, "verification|attestation|verify"):
                export_outputs.run(workspace, "auto", None, root / "exports")

            before_reapply = (workspace / "manifest.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "formal decision provenance"):
                apply_decisions.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "decisions/ads_decisions.jsonl",
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                )
            self.assertEqual((workspace / "manifest.json").read_bytes(), before_reapply)
            self.assertNotEqual(
                common.sha256_file(workspace / "versions/v2_ads_removed.txt"),
                clean_sha256,
            )

    def test_point_rollback_restores_one_candidate_without_using_its_id_in_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, text, decisions, _ = self.make_workspace(Path(directory))
            unique_anchors = decisions[1]["anchors"]  # type: ignore[index]
            shared_anchor_ids = sorted(
                str(anchor["anchor_id"])
                for anchor in decisions[0]["anchors"]  # type: ignore[index]
            )

            report = rollback.rollback_point(workspace, "ads", self.shared_id)

            self.assertEqual(
                (workspace / "versions/v2_ads_removed.txt").read_text(encoding="utf-8"),
                self.text_after_deleting(text, unique_anchors),
            )
            self.assertEqual(report["restored_anchor_ids"], shared_anchor_ids)
            self.assertTrue((workspace / "decisions/rollback_ads_point.jsonl").is_file())
            self.assertEqual(
                json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))["stages"][
                    "rollback_ads_point"
                ]["status"],
                "done",
            )
            self.assertFalse(any(self.shared_id in path.name for path in workspace.rglob("*")))

    def test_zero_duplicate_and_invalid_targets_publish_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _, decisions, _ = self.make_workspace(root)

            for action in (
                lambda: rollback.rollback_point(workspace, "ads", "missing"),
                lambda: rollback.rollback_chapter(workspace, "ads", 99),
                lambda: rollback.rollback_point(
                    workspace,
                    "ads",
                    self.shared_id,
                    "report/not-a-version.txt",
                ),
            ):
                before = self.snapshot(workspace)
                with self.assertRaises(ValueError):
                    action()
                self.assertEqual(self.snapshot(workspace), before)

            duplicated = [*decisions, copy.deepcopy(decisions[0])]
            common.write_jsonl(workspace / "decisions/ads_decisions.jsonl", duplicated)
            before = self.snapshot(workspace)
            with self.assertRaisesRegex(ValueError, "duplicate candidate_id"):
                rollback.rollback_point(workspace, "ads", self.shared_id)
            self.assertEqual(self.snapshot(workspace), before)

    def test_target_anchor_and_fallback_locator_are_preflighted_before_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _, decisions, _ = self.make_workspace(root, "bad-anchor")
            decisions[0]["anchors"][0]["offset"] = 0  # type: ignore[index]
            common.write_jsonl(workspace / "decisions/ads_decisions.jsonl", decisions)
            before = self.snapshot(workspace)

            with self.assertRaisesRegex(ValueError, "preflight"):
                rollback.rollback_point(workspace, "ads", self.shared_id)

            self.assertEqual(self.snapshot(workspace), before)

            fallback, _, _, _ = self.make_workspace(root, "fallback", true_chapters=False)
            before = self.snapshot(fallback)
            with self.assertRaisesRegex(ValueError, "true chapter"):
                rollback.rollback_chapter(fallback, "ads", 1)
            self.assertEqual(self.snapshot(fallback), before)


if __name__ == "__main__":
    unittest.main()
