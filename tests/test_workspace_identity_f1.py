from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import rollback  # noqa: E402


class SimulatedProcessExit(BaseException):
    pass


def tree_snapshot(root: Path) -> dict[str, str]:
    snapshot = {".": "dir"}
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[relative] = f"file:{digest}:{stat.S_IMODE(path.stat().st_mode):o}"
        elif path.is_dir():
            snapshot[relative + "/"] = "dir"
        else:
            snapshot[relative] = "other"
    return snapshot


def write_manifest(workspace: Path, manifest: dict[str, Any]) -> None:
    (workspace / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class WorkspaceIdentityF1Tests(unittest.TestCase):
    def bind_workspace(self, root: Path) -> tuple[Path, Path]:
        source = root / "sample-a.txt"
        source.write_text("第一章 示例\n正文甲。\n", encoding="utf-8")
        workspace = root / "sample-a.txt.cleanwork"
        common.init_workspace_from_source(source, workspace)
        return source, workspace

    def assert_identity_rejection_without_writes(
        self,
        root: Path,
        call: Callable[[], object],
    ) -> None:
        before = tree_snapshot(root)
        with self.assertRaises(common.WorkspaceIdentityError):
            call()
        self.assertEqual(tree_snapshot(root), before)

    def assert_preprocess_is_pending(self, workspace: Path) -> None:
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["stages"]["0_preprocess"]["status"], "pending")
        self.assertFalse((workspace / "versions/v1_preprocessed.txt").exists())
        self.assertFalse((workspace / "report/preprocess_report.json").exists())

    def test_first_snapshot_binds_exact_source_and_v0_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_bytes("第一章 示例\r\n正文甲。\r\n".encode("gb18030"))

            workspace = preprocess.run(source)
            v0 = workspace / "versions" / "v0_original.txt"
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            expected_hash = common.sha256_file(source)

            self.assertEqual(v0.read_bytes(), source.read_bytes())
            self.assertEqual(common.sha256_file(v0), expected_hash)
            self.assertEqual(manifest["source"]["sha256"], expected_hash)
            self.assertEqual(manifest["v0"]["sha256"], expected_hash)
            self.assertEqual(manifest["source"]["path"], str(source.resolve()))
            self.assertEqual(manifest["v0"]["path"], "versions/v0_original.txt")
            self.assertEqual(manifest["workspace"], str(workspace.resolve()))
            self.assertEqual(v0.stat().st_mode & stat.S_IWUSR, 0)

    def test_matching_identity_allows_workspace_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, workspace = self.bind_workspace(root)
            v0 = workspace / "versions" / "v0_original.txt"
            original = v0.read_bytes()
            original_manifest = json.loads(
                (workspace / "manifest.json").read_text(encoding="utf-8")
            )
            source.write_bytes(source.read_bytes())

            self.assertEqual(preprocess.run(source, str(workspace)), workspace.resolve())
            self.assertEqual(v0.read_bytes(), original)
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["created_at"], original_manifest["created_at"])
            self.assertEqual(manifest["v0"], original_manifest["v0"])
            self.assertEqual(manifest["stages"]["0_preprocess"]["status"], "done")
            if os.name == "nt":
                self.assertEqual(
                    preprocess.run(Path(str(source).upper()), str(workspace).upper()),
                    workspace.resolve(),
                )

    @unittest.skipUnless(os.name == "nt", "Windows extended path syntax")
    def test_windows_extended_path_identity_is_stable_across_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, workspace = self.bind_workspace(root)
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["workspace"] = "\\\\?\\" + manifest["workspace"]
            manifest["source"]["path"] = "\\\\?\\" + manifest["source"]["path"]
            write_manifest(workspace, manifest)

            self.assertEqual(preprocess.run(source, str(workspace)), workspace.resolve())
            self.assertEqual(
                common.load_manifest(workspace)["stages"]["0_preprocess"]["status"],
                "done",
            )

    def test_different_source_path_is_rejected_even_when_bytes_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, workspace = self.bind_workspace(root)
            other = root / "sample-b.txt"
            other.write_bytes(source.read_bytes())

            self.assert_identity_rejection_without_writes(
                root,
                lambda: preprocess.run(other, str(workspace)),
            )
            self.assert_preprocess_is_pending(workspace)

            hardlink = root / "sample-link.txt"
            try:
                os.link(source, hardlink)
            except OSError:
                hardlink = None
            if hardlink is not None:
                self.assert_identity_rejection_without_writes(
                    root,
                    lambda: preprocess.run(hardlink, str(workspace)),
                )
                self.assert_preprocess_is_pending(workspace)

    def test_source_content_drift_is_rejected_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, workspace = self.bind_workspace(root)
            source.write_text("第一章 示例\n已变化的正文。\n", encoding="utf-8")

            self.assert_identity_rejection_without_writes(
                root,
                lambda: preprocess.run(source, str(workspace)),
            )
            self.assert_preprocess_is_pending(workspace)

    def test_v0_content_drift_is_rejected_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, workspace = self.bind_workspace(root)
            v0 = workspace / "versions/v0_original.txt"
            v0.chmod(stat.S_IREAD | stat.S_IWRITE)
            v0.write_text("被篡改的快照。\n", encoding="utf-8")

            self.assert_identity_rejection_without_writes(
                root,
                lambda: preprocess.run(source, str(workspace)),
            )
            self.assert_preprocess_is_pending(workspace)

    def test_manifest_identity_mismatches_are_rejected_without_outputs(self) -> None:
        mutations = {
            "source-path": lambda value, root: value["source"].__setitem__(
                "path", str((root / "sample-b.txt").resolve())
            ),
            "source-hash": lambda value, root: value["source"].__setitem__(
                "sha256", "0" * 64
            ),
            "missing-source-hash": lambda value, root: value["source"].pop("sha256"),
            "v0-hash": lambda value, root: value["v0"].__setitem__("sha256", "0" * 64),
            "v0-path": lambda value, root: value["v0"].__setitem__(
                "path", "versions/other.txt"
            ),
            "workspace": lambda value, root: value.__setitem__(
                "workspace", str((root / "other.cleanwork").resolve())
            ),
            "missing-workspace": lambda value, root: value.pop("workspace"),
            "missing-v0-identity": lambda value, root: value.pop("v0"),
        }
        for case, mutate in mutations.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, workspace = self.bind_workspace(root)
                manifest = json.loads(
                    (workspace / "manifest.json").read_text(encoding="utf-8")
                )
                mutate(manifest, root)
                write_manifest(workspace, manifest)

                self.assert_identity_rejection_without_writes(
                    root,
                    lambda: preprocess.run(source, str(workspace)),
                )
                self.assert_preprocess_is_pending(workspace)

    def test_missing_v0_is_not_silently_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, workspace = self.bind_workspace(root)
            v0 = workspace / "versions/v0_original.txt"
            v0.chmod(stat.S_IREAD | stat.S_IWRITE)
            v0.unlink()

            self.assert_identity_rejection_without_writes(
                root,
                lambda: preprocess.run(source, str(workspace)),
            )
            self.assertFalse(v0.exists())

    def test_missing_manifest_is_not_silently_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, workspace = self.bind_workspace(root)
            manifest_path = workspace / "manifest.json"
            manifest_path.unlink()

            self.assert_identity_rejection_without_writes(
                root,
                lambda: preprocess.run(source, str(workspace)),
            )
            self.assertFalse(manifest_path.exists())

    def test_nonempty_unbound_workspace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_text("第一章 示例\n正文甲。\n", encoding="utf-8")
            workspace = root / "other.cleanwork"
            (workspace / "versions").mkdir(parents=True)
            (workspace / "versions/v1_preprocessed.txt").write_text(
                "不应被接管的工件。\n",
                encoding="utf-8",
            )

            self.assert_identity_rejection_without_writes(
                root,
                lambda: preprocess.run(source, str(workspace)),
            )
            self.assertFalse((workspace / "manifest.json").exists())
            self.assertFalse((workspace / "versions/v0_original.txt").exists())

    def test_malformed_manifests_fail_closed_without_internal_errors(self) -> None:
        payloads = (b"[]", b"{", b"\xff")
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, workspace = self.bind_workspace(root)
                (workspace / "manifest.json").write_bytes(payload)

                self.assert_identity_rejection_without_writes(
                    root,
                    lambda: preprocess.run(source, str(workspace)),
                )
                self.assertFalse((workspace / "versions/v1_preprocessed.txt").exists())

    def test_v0_drift_blocks_non_preprocess_workspace_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.bind_workspace(root)
            v0 = workspace / "versions/v0_original.txt"
            v0.chmod(stat.S_IREAD | stat.S_IWRITE)
            common.resolve_workspace_paths(workspace)
            timestamps = v0.stat()
            changed = bytearray(v0.read_bytes())
            changed[-1] ^= 1
            v0.write_bytes(changed)
            os.utime(v0, ns=(timestamps.st_atime_ns, timestamps.st_mtime_ns))

            self.assert_identity_rejection_without_writes(
                root,
                lambda: rollback.rollback_all(workspace, None, True),
            )
            self.assertFalse((workspace / "versions/rollback_v0_original.txt").exists())
            self.assertFalse((workspace / "report/rollback_report.json").exists())

    def test_identity_fields_cannot_be_removed_to_bypass_v0_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.bind_workspace(root)
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("v0")
            manifest.pop("workspace")
            manifest["source"].pop("sha256")
            write_manifest(workspace, manifest)
            v0 = workspace / "versions/v0_original.txt"
            v0.chmod(stat.S_IREAD | stat.S_IWRITE)
            changed = bytearray(v0.read_bytes())
            changed[-1] ^= 1
            v0.write_bytes(changed)

            self.assert_identity_rejection_without_writes(
                root,
                lambda: rollback.rollback_all(workspace, None, True),
            )
            self.assertFalse((workspace / "versions/rollback_v0_original.txt").exists())

    def test_snapshot_and_identity_deletion_cannot_downgrade_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_text("第一章 示例\n正文甲。\n", encoding="utf-8")
            workspace = preprocess.run(source)
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("v0")
            manifest.pop("workspace")
            manifest["source"].pop("sha256")
            write_manifest(workspace, manifest)
            v0 = workspace / "versions/v0_original.txt"
            v0.chmod(stat.S_IREAD | stat.S_IWRITE)
            v0.unlink()

            self.assert_identity_rejection_without_writes(
                root,
                lambda: parse_structure.run(workspace),
            )
            self.assertFalse((workspace / "meta/chapters.json").exists())
            self.assertFalse((workspace / "report/structure_report.json").exists())

    def test_source_change_during_copy_never_commits_identity_or_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_text("第一章 示例\n正文甲。\n", encoding="utf-8")
            workspace = root / "sample-a.txt.cleanwork"
            real_copy = common._copy_snapshot_source

            def copy_then_change(copy_source: Path, target: Path) -> None:
                real_copy(copy_source, target)
                source.write_text("第一章 示例\n复制期间变化。\n", encoding="utf-8")

            with mock.patch.object(
                common,
                "_copy_snapshot_source",
                side_effect=copy_then_change,
            ):
                with self.assertRaises(common.WorkspaceIdentityError):
                    preprocess.run(source, str(workspace))

            self.assertFalse((workspace / "manifest.json").exists())
            self.assertFalse((workspace / "versions/v1_preprocessed.txt").exists())
            self.assertFalse((workspace / "report/preprocess_report.json").exists())

    def test_first_snapshot_retry_after_staged_copy_before_v0_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_bytes(("第一章 示例\n" + "正文内容。\n" * 128).encode("utf-8"))
            source_before = source.read_bytes()
            workspace = root / "sample-a.txt.cleanwork"
            v0 = workspace / "versions" / "v0_original.txt"
            real_replace = common.os.replace

            def exit_before_v0_publish(source_path: object, target_path: object) -> None:
                if Path(target_path) == v0:
                    raise SimulatedProcessExit("injected process exit before v0 publish")
                real_replace(source_path, target_path)

            with (
                mock.patch.object(common.os, "replace", side_effect=exit_before_v0_publish),
                self.assertRaisesRegex(SimulatedProcessExit, "before v0 publish"),
            ):
                preprocess.run(source, str(workspace))

            self.assertEqual(source.read_bytes(), source_before)
            self.assertFalse(v0.exists())
            self.assertFalse((workspace / "manifest.json").exists())

            self.assertEqual(preprocess.run(source, str(workspace)), workspace.resolve())
            manifest = common.load_manifest(workspace)

            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(v0.read_bytes(), source_before)
            self.assertEqual(manifest["source"]["sha256"], common.sha256_file(source))
            self.assertFalse((workspace / ".snapshot-init.json").exists())

    def test_first_snapshot_retry_after_v0_publish_before_manifest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_bytes(("第一章 示例\n" + "正文内容。\n" * 128).encode("utf-8"))
            source_before = source.read_bytes()
            workspace = root / "sample-a.txt.cleanwork"
            v0 = workspace / "versions" / "v0_original.txt"

            with (
                mock.patch.object(
                    common,
                    "save_manifest",
                    side_effect=SimulatedProcessExit(
                        "injected process exit before manifest commit"
                    ),
                ),
                self.assertRaisesRegex(SimulatedProcessExit, "before manifest commit"),
            ):
                preprocess.run(source, str(workspace))

            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(v0.read_bytes(), source_before)
            self.assertFalse((workspace / "manifest.json").exists())

            self.assertEqual(preprocess.run(source, str(workspace)), workspace.resolve())
            manifest = common.load_manifest(workspace)

            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(v0.read_bytes(), source_before)
            self.assertEqual(manifest["v0"]["sha256"], common.sha256_file(v0))
            self.assertFalse((workspace / ".snapshot-init.json").exists())

    def test_first_snapshot_retry_after_atomic_journal_write_is_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_bytes(("第一章 示例\n" + "正文内容。\n" * 128).encode("utf-8"))
            source_before = source.read_bytes()
            workspace = root / "sample-a.txt.cleanwork"
            real_write_json = common.write_json

            def exit_during_journal_write(path: Path, data: object) -> None:
                if path.name == ".snapshot-init.json":
                    (workspace / "..snapshot-init.json.deadbeef.tmp").write_bytes(
                        b'{"schema_version":'
                    )
                    raise SimulatedProcessExit(
                        "injected process exit during initialization journal write"
                    )
                real_write_json(path, data)

            with (
                mock.patch.object(
                    common,
                    "write_json",
                    side_effect=exit_during_journal_write,
                ),
                self.assertRaisesRegex(SimulatedProcessExit, "journal write"),
            ):
                preprocess.run(source, str(workspace))

            self.assertEqual(source.read_bytes(), source_before)
            self.assertTrue((workspace / ".snapshot-init.marker").is_file())
            self.assertFalse((workspace / ".snapshot-init.json").exists())

            self.assertEqual(preprocess.run(source, str(workspace)), workspace.resolve())

            self.assertEqual(
                (workspace / "versions/v0_original.txt").read_bytes(),
                source_before,
            )
            self.assertFalse(
                (workspace / "..snapshot-init.json.deadbeef.tmp").exists()
            )
            self.assertFalse((workspace / ".snapshot-init.marker").exists())
            self.assertFalse((workspace / ".snapshot-init.json").exists())

    def test_first_snapshot_retry_cleans_an_interrupted_manifest_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_bytes(("第一章 示例\n" + "正文内容。\n" * 128).encode("utf-8"))
            source_before = source.read_bytes()
            workspace = root / "sample-a.txt.cleanwork"

            def exit_during_manifest_write(
                manifest_workspace: Path,
                manifest: dict[str, Any],
            ) -> None:
                (manifest_workspace / ".manifest.json.deadbeef.tmp").write_bytes(b'{"v0":')
                raise SimulatedProcessExit(
                    "injected process exit during manifest atomic write"
                )

            with (
                mock.patch.object(
                    common,
                    "save_manifest",
                    side_effect=exit_during_manifest_write,
                ),
                self.assertRaisesRegex(SimulatedProcessExit, "manifest atomic write"),
            ):
                preprocess.run(source, str(workspace))

            self.assertEqual(source.read_bytes(), source_before)
            self.assertTrue((workspace / ".snapshot-init.marker").is_file())
            self.assertTrue((workspace / ".snapshot-init.json").is_file())
            self.assertTrue((workspace / ".manifest.json.deadbeef.tmp").is_file())

            self.assertEqual(preprocess.run(source, str(workspace)), workspace.resolve())

            self.assertEqual(
                (workspace / "versions/v0_original.txt").read_bytes(),
                source_before,
            )
            self.assertFalse((workspace / ".manifest.json.deadbeef.tmp").exists())
            self.assertFalse((workspace / ".snapshot-init.marker").exists())
            self.assertFalse((workspace / ".snapshot-init.json").exists())

    def test_interrupted_snapshot_preserves_an_unknown_temp_shaped_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_bytes(("第一章 示例\n" + "正文内容。\n" * 128).encode("utf-8"))
            workspace = root / "sample-a.txt.cleanwork"

            with (
                mock.patch.object(
                    common,
                    "save_manifest",
                    side_effect=SimulatedProcessExit(
                        "injected process exit before manifest commit"
                    ),
                ),
                self.assertRaises(SimulatedProcessExit),
            ):
                preprocess.run(source, str(workspace))

            unknown = workspace / ".manifest.json.not-an-owned-token.tmp"
            unknown.write_text("keep\n", encoding="utf-8")
            before = tree_snapshot(workspace)

            with self.assertRaisesRegex(
                common.WorkspaceIdentityError,
                "unknown entry",
            ):
                preprocess.run(source, str(workspace))

            self.assertEqual(tree_snapshot(workspace), before)

    def test_interrupted_snapshot_tamper_fails_closed_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_bytes(("第一章 示例\n" + "正文内容。\n" * 128).encode("utf-8"))
            workspace = root / "sample-a.txt.cleanwork"
            v0 = workspace / "versions" / "v0_original.txt"

            with (
                mock.patch.object(
                    common,
                    "save_manifest",
                    side_effect=SimulatedProcessExit(
                        "injected process exit before manifest commit"
                    ),
                ),
                self.assertRaises(SimulatedProcessExit),
            ):
                preprocess.run(source, str(workspace))

            v0.chmod(stat.S_IREAD | stat.S_IWRITE)
            v0.write_bytes(b"tampered snapshot\n")
            before = tree_snapshot(workspace)

            with self.assertRaisesRegex(
                common.WorkspaceIdentityError,
                "does not match",
            ):
                preprocess.run(source, str(workspace))

            self.assertEqual(tree_snapshot(workspace), before)

    def test_interrupted_snapshot_does_not_delete_an_unknown_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_bytes(("第一章 示例\n" + "正文内容。\n" * 128).encode("utf-8"))
            workspace = root / "sample-a.txt.cleanwork"
            v0 = workspace / "versions" / "v0_original.txt"
            real_replace = common.os.replace

            def exit_before_v0_publish(source_path: object, target_path: object) -> None:
                if Path(target_path) == v0:
                    raise SimulatedProcessExit("injected process exit before v0 publish")
                real_replace(source_path, target_path)

            with (
                mock.patch.object(common.os, "replace", side_effect=exit_before_v0_publish),
                self.assertRaises(SimulatedProcessExit),
            ):
                preprocess.run(source, str(workspace))

            run_id = "a" * 32
            sentinel = workspace / ".runs" / run_id / "keep.txt"
            common.write_utf8(workspace / ".runs" / run_id / "run.marker", run_id)
            sentinel.write_text("unknown\n", encoding="utf-8")
            before = tree_snapshot(workspace)

            with self.assertRaisesRegex(
                common.WorkspaceIdentityError,
                "unknown entry",
            ):
                preprocess.run(source, str(workspace))

            self.assertEqual(tree_snapshot(workspace), before)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unknown\n")


if __name__ == "__main__":
    unittest.main()
