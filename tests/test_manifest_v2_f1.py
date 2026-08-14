from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import export_outputs  # noqa: E402
import normalize_layout  # noqa: E402
import preprocess  # noqa: E402
import scan_blocked  # noqa: E402
import scan_titles  # noqa: E402


class ManifestV2F1Tests(unittest.TestCase):
    def make_preprocessed_workspace(self, root: Path) -> tuple[Path, Path]:
        source = root / "sample-a.txt"
        source.write_text("第一章 起点\n匿名正文甲。\n", encoding="utf-8")
        return source, preprocess.run(source)

    def read_manifest(self, workspace: Path) -> dict[str, object]:
        return json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))

    def test_preprocess_commits_manifest_v2_with_continuous_artifact_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, workspace = self.make_preprocessed_workspace(Path(directory))
            manifest = self.read_manifest(workspace)
            artifacts = manifest["artifacts"]
            self.assertIsInstance(artifacts, dict)

            v0 = artifacts["versions/v0_original.txt"]
            v1 = artifacts["versions/v1_preprocessed.txt"]
            report = artifacts["report/preprocess_report.json"]
            stage = manifest["stages"]["0_preprocess"]

            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["current_head"], "versions/v1_preprocessed.txt")
            self.assertEqual(v0["sha256"], common.sha256_file(source))
            self.assertIsNone(v0["parent_sha256"])
            self.assertEqual(v1["sha256"], common.sha256_file(workspace / v1["path"]))
            self.assertEqual(v1["parent_sha256"], v0["sha256"])
            self.assertEqual(v1["run_id"], stage["run_id"])
            self.assertIsNone(v1["config_sha256"])
            self.assertIsNone(v1["decision_sha256"])
            self.assertEqual(report["run_id"], stage["run_id"])
            self.assertEqual(
                set(stage["artifacts"]),
                {"versions/v1_preprocessed.txt", "report/preprocess_report.json"},
            )

    def test_current_head_ignores_a_manually_placed_higher_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.make_preprocessed_workspace(Path(directory))
            forged = workspace / "versions/v5_layout_final.txt"
            forged.write_text("不可信高版本。\n", encoding="utf-8")

            selected = common.resolve_current_head(workspace)

            self.assertEqual(selected, workspace / "versions/v1_preprocessed.txt")

    def test_every_auto_consumer_uses_current_head_instead_of_file_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.make_preprocessed_workspace(Path(directory))
            for name in (
                "v2_ads_removed.txt",
                "v3_titles_fixed.txt",
                "v4_words_restored.txt",
                "v5_layout_final.txt",
            ):
                (workspace / "versions" / name).write_text("伪造版本。\n", encoding="utf-8")

            expected_path = workspace / "versions/v1_preprocessed.txt"
            expected_relative = "versions/v1_preprocessed.txt"
            self.assertEqual(export_outputs.choose_input(workspace, "auto"), expected_path)
            self.assertEqual(normalize_layout.choose_input(workspace, "auto"), expected_relative)
            self.assertEqual(scan_titles.choose_input(workspace, "auto"), expected_relative)
            self.assertEqual(scan_blocked.choose_input(workspace, "auto"), expected_relative)

    def test_unknown_status_is_rejected_without_changing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.make_preprocessed_workspace(Path(directory))
            manifest_path = workspace / "manifest.json"
            before = manifest_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "status"):
                common.update_stage(workspace, "0_preprocess", "anything-goes")

            self.assertEqual(manifest_path.read_bytes(), before)

    def test_unknown_stage_is_rejected_without_changing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.make_preprocessed_workspace(Path(directory))
            manifest_path = workspace / "manifest.json"
            before = manifest_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "stage"):
                common.update_stage(workspace, "arbitrary_stage", "done")

            self.assertEqual(manifest_path.read_bytes(), before)

    def test_known_stage_cannot_be_marked_done_without_committed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.make_preprocessed_workspace(Path(directory))
            manifest_path = workspace / "manifest.json"
            before = manifest_path.read_bytes()

            with self.assertRaisesRegex(common.WorkspaceIdentityError, "artifact|run_id"):
                common.update_stage(workspace, "6_verify", "done")

            self.assertEqual(manifest_path.read_bytes(), before)

    def test_missing_done_stage_report_invalidates_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.make_preprocessed_workspace(Path(directory))
            (workspace / "report/preprocess_report.json").unlink()

            with self.assertRaisesRegex(common.WorkspaceIdentityError, "artifact"):
                common.resolve_workspace_paths(workspace)

    def test_same_size_tamper_of_a_non_head_parent_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.make_preprocessed_workspace(Path(directory))
            v1 = workspace / "versions/v1_preprocessed.txt"
            v2 = workspace / "versions/v2_ads_removed.txt"
            with common.WorkspaceTransaction(workspace) as transaction:
                common.write_utf8(transaction.stage_path(v2), v1.read_text(encoding="utf-8"))
                transaction.commit(
                    {
                        "2_ads": (
                            "done",
                            {
                                "input": "versions/v1_preprocessed.txt",
                                "output": "versions/v2_ads_removed.txt",
                            },
                        )
                    }
                )
            original = v1.read_bytes()
            changed = bytearray(original)
            changed[-2] ^= 1
            v1.write_bytes(changed)
            self.assertEqual(v1.stat().st_size, len(original))

            with self.assertRaisesRegex(common.WorkspaceIdentityError, "content"):
                common.resolve_workspace_paths(workspace)

    def test_v0_artifact_ledger_is_bound_to_the_immutable_snapshot_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.make_preprocessed_workspace(Path(directory))
            manifest_path = workspace / "manifest.json"
            manifest = self.read_manifest(workspace)
            artifacts = manifest["artifacts"]
            v0_record = artifacts["versions/v0_original.txt"]
            v1_record = artifacts["versions/v1_preprocessed.txt"]

            forged = workspace / "versions/v1_preprocessed.txt"
            forged.write_text("FORGED CURRENT HEAD\n", encoding="utf-8")
            forged_sha256 = common.sha256_file(forged)
            v1_record["sha256"] = forged_sha256
            v1_record["size_bytes"] = forged.stat().st_size
            v0_record["sha256"] = "b" * 64
            v1_record["parent_sha256"] = "b" * 64
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(common.WorkspaceIdentityError, "v0 artifact"):
                common.resolve_workspace_paths(workspace)

    def test_v0_artifact_ledger_rejects_size_stage_and_parent_drift(self) -> None:
        mutations = (
            lambda record: record.update(size_bytes=record["size_bytes"] + 1),
            lambda record: record.update(stage="not-source-snapshot"),
            lambda record: record.update(
                parent_path="versions/other.txt", parent_sha256="c" * 64
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate), tempfile.TemporaryDirectory() as directory:
                _, workspace = self.make_preprocessed_workspace(Path(directory))
                manifest_path = workspace / "manifest.json"
                manifest = self.read_manifest(workspace)
                mutate(manifest["artifacts"]["versions/v0_original.txt"])
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(common.WorkspaceIdentityError, "v0 artifact"):
                    common.resolve_workspace_paths(workspace)

    def test_layout_artifact_binds_the_effective_config_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.make_preprocessed_workspace(Path(directory))

            report = normalize_layout.run(
                workspace,
                "auto",
                "versions/v5_layout_final.txt",
                None,
            )
            manifest = self.read_manifest(workspace)
            artifact = manifest["artifacts"]["versions/v5_layout_final.txt"]
            expected = hashlib.sha256(
                json.dumps(
                    report["config"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            self.assertEqual(artifact["parent_path"], "versions/v1_preprocessed.txt")
            self.assertEqual(artifact["config_sha256"], expected)
            self.assertEqual(manifest["current_head"], "versions/v5_layout_final.txt")

    def test_legacy_manifest_is_rejected_instead_of_guessed_or_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.make_preprocessed_workspace(Path(directory))
            manifest_path = workspace / "manifest.json"
            manifest = self.read_manifest(workspace)
            manifest["schema_version"] = 1
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            before = manifest_path.read_bytes()

            with self.assertRaisesRegex(common.WorkspaceIdentityError, "schema|rebuild|重建"):
                common.resolve_workspace_paths(workspace)

            self.assertEqual(manifest_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
