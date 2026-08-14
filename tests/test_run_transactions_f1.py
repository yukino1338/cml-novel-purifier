from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402


class SimulatedProcessExit(BaseException):
    pass


class RunTransactionsF1Tests(unittest.TestCase):
    def bind_workspace(self, root: Path) -> tuple[Path, Path]:
        source = root / "sample-a.txt"
        source.write_text("第一章 示例\n正文甲。\n", encoding="utf-8")
        workspace = root / "sample-a.txt.cleanwork"
        common.init_workspace_from_source(source, workspace)
        return source, workspace

    def assert_no_runs(self, workspace: Path) -> None:
        runs = workspace / ".runs"
        self.assertFalse(runs.exists() and any(runs.iterdir()))

    def test_preprocess_write_failure_keeps_all_previous_artifacts_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, workspace = self.bind_workspace(Path(directory))
            v1 = workspace / "versions" / "v1_preprocessed.txt"
            report = workspace / "report" / "preprocess_report.json"
            v1.write_text("旧正文\n", encoding="utf-8")
            report.write_text('{"version":"old"}\n', encoding="utf-8")
            before_manifest = (workspace / "manifest.json").read_bytes()

            with mock.patch.object(preprocess, "write_json", side_effect=OSError("injected report failure")):
                with self.assertRaisesRegex(OSError, "injected report failure"):
                    preprocess.run(source, str(workspace))

            self.assertEqual(v1.read_text(encoding="utf-8"), "旧正文\n")
            self.assertEqual(report.read_text(encoding="utf-8"), '{"version":"old"}\n')
            self.assertEqual((workspace / "manifest.json").read_bytes(), before_manifest)
            self.assert_no_runs(workspace)

    def test_structure_write_failure_keeps_all_previous_artifacts_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, workspace = self.bind_workspace(Path(directory))
            preprocess.run(source, str(workspace))
            chapters = workspace / "meta" / "chapters.json"
            report = workspace / "report" / "structure_report.json"
            chapters.write_text('{"version":"old-chapters"}\n', encoding="utf-8")
            report.write_text('{"version":"old-report"}\n', encoding="utf-8")
            before_manifest = (workspace / "manifest.json").read_bytes()
            real_write_json = parse_structure.write_json
            call_count = 0

            def fail_second_write(path: Path, data: object) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError("injected report failure")
                real_write_json(path, data)

            with mock.patch.object(parse_structure, "write_json", side_effect=fail_second_write):
                with self.assertRaisesRegex(OSError, "injected report failure"):
                    parse_structure.run(workspace)

            self.assertEqual(chapters.read_text(encoding="utf-8"), '{"version":"old-chapters"}\n')
            self.assertEqual(report.read_text(encoding="utf-8"), '{"version":"old-report"}\n')
            self.assertEqual((workspace / "manifest.json").read_bytes(), before_manifest)
            self.assert_no_runs(workspace)

    def test_publish_failure_restores_every_old_artifact_before_manifest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            first = workspace / "report" / "first.json"
            second = workspace / "report" / "second.json"
            first.write_text('{"version":"old-first"}\n', encoding="utf-8")
            second.write_text('{"version":"old-second"}\n', encoding="utf-8")
            before_manifest = (workspace / "manifest.json").read_bytes()
            real_replace = common.os.replace
            failed = False

            def fail_second_publish(source: object, target: object) -> None:
                nonlocal failed
                if Path(target) == second and not failed:
                    failed = True
                    raise OSError("injected publish failure")
                real_replace(source, target)

            with common.WorkspaceTransaction(workspace) as transaction:
                common.write_json(transaction.stage_path(first), {"version": "new-first"})
                common.write_json(transaction.stage_path(second), {"version": "new-second"})
                with mock.patch.object(common.os, "replace", side_effect=fail_second_publish):
                    with self.assertRaisesRegex(OSError, "injected publish failure"):
                        transaction.commit({"dry_run": ("done", {"report": "report/first.json"})})

            self.assertEqual(first.read_text(encoding="utf-8"), '{"version":"old-first"}\n')
            self.assertEqual(second.read_text(encoding="utf-8"), '{"version":"old-second"}\n')
            self.assertEqual((workspace / "manifest.json").read_bytes(), before_manifest)
            self.assert_no_runs(workspace)

    def test_interrupted_rollback_is_recovered_before_the_next_workspace_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            first = workspace / "report" / "first.json"
            second = workspace / "report" / "second.json"
            first.write_text('{"version":"old-first"}\n', encoding="utf-8")
            second.write_text('{"version":"old-second"}\n', encoding="utf-8")
            before_manifest = (workspace / "manifest.json").read_bytes()
            real_replace = common.os.replace

            def permanently_fail_second_publish(source: object, target: object) -> None:
                if Path(target) == second:
                    raise OSError("injected persistent publish failure")
                real_replace(source, target)

            with common.WorkspaceTransaction(workspace) as transaction:
                common.write_json(transaction.stage_path(first), {"version": "new-first"})
                common.write_json(transaction.stage_path(second), {"version": "new-second"})
                with mock.patch.object(common.os, "replace", side_effect=permanently_fail_second_publish):
                    with self.assertRaises(common.WorkspaceTransactionError):
                        transaction.commit({"dry_run": ("done", {"report": "report/first.json"})})

            self.assertTrue((workspace / ".runs").exists())
            common.resolve_workspace_paths(workspace)

            self.assertEqual(first.read_text(encoding="utf-8"), '{"version":"old-first"}\n')
            self.assertEqual(second.read_text(encoding="utf-8"), '{"version":"old-second"}\n')
            self.assertEqual((workspace / "manifest.json").read_bytes(), before_manifest)
            self.assert_no_runs(workspace)

    def test_cleanup_failure_after_manifest_commit_is_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            report = workspace / "report" / "committed.json"
            real_rmtree = common.shutil.rmtree
            failed = False

            def fail_first_run_cleanup(path: object, *args: object, **kwargs: object) -> None:
                nonlocal failed
                if Path(path).parent == workspace / ".runs" and not failed:
                    failed = True
                    raise OSError("injected cleanup failure")
                real_rmtree(path, *args, **kwargs)

            with mock.patch.object(common.shutil, "rmtree", side_effect=fail_first_run_cleanup):
                with common.WorkspaceTransaction(workspace) as transaction:
                    common.write_json(transaction.stage_path(report), {"version": "committed"})
                    transaction.commit(
                        {"dry_run": ("done", {"report": "report/committed.json"})}
                    )

            manifest = common.load_manifest(workspace)
            self.assertEqual(report.read_text(encoding="utf-8"), '{\n  "version": "committed"\n}\n')
            self.assertEqual(manifest["stages"]["dry_run"]["status"], "done")
            self.assertTrue((workspace / ".runs").exists())

            common.resolve_workspace_paths(workspace)
            self.assert_no_runs(workspace)

    def test_manifest_parent_sync_failure_after_replace_keeps_committed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            report = workspace / "report" / "committed.json"
            real_sync = common._fsync_parent_directory

            def fail_manifest_sync(path: Path) -> None:
                if path == workspace / "manifest.json":
                    raise OSError("injected manifest directory sync failure")
                real_sync(path)

            with mock.patch.object(common, "_fsync_parent_directory", side_effect=fail_manifest_sync):
                with common.WorkspaceTransaction(workspace) as transaction:
                    common.write_json(transaction.stage_path(report), {"version": "committed"})
                    transaction.commit(
                        {"dry_run": ("done", {"report": "report/committed.json"})}
                    )

            manifest = common.load_manifest(workspace)
            self.assertEqual(report.read_text(encoding="utf-8"), '{\n  "version": "committed"\n}\n')
            self.assertEqual(manifest["stages"]["dry_run"]["status"], "done")
            self.assert_no_runs(workspace)

    def test_recovery_preserves_a_preexisting_empty_transaction_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            existing = workspace / "candidates" / "ads_pages"
            existing.mkdir(parents=True)
            first = workspace / "report" / "first.json"
            second = workspace / "report" / "second.json"
            first.write_text('{"version":"old-first"}\n', encoding="utf-8")
            second.write_text('{"version":"old-second"}\n', encoding="utf-8")
            real_replace = common.os.replace

            def permanently_fail_second_publish(source: object, target: object) -> None:
                if Path(target) == second:
                    raise OSError("injected persistent publish failure")
                real_replace(source, target)

            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_directory(existing)
                common.write_json(transaction.stage_path(first), {"version": "new-first"})
                common.write_json(transaction.stage_path(second), {"version": "new-second"})
                with mock.patch.object(common.os, "replace", side_effect=permanently_fail_second_publish):
                    with self.assertRaises(common.WorkspaceTransactionError):
                        transaction.commit(
                            {"dry_run": ("done", {"report": "report/first.json"})}
                        )

            common.resolve_workspace_paths(workspace)
            self.assertTrue(existing.is_dir())
            self.assertFalse(any(existing.iterdir()))
            self.assert_no_runs(workspace)

    def test_recovery_retry_does_not_delete_an_already_restored_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            first = workspace / "report" / "first.json"
            second = workspace / "report" / "second.json"
            first.write_text("old-first\n", encoding="utf-8")
            second.write_text("old-second\n", encoding="utf-8")
            transaction = common.WorkspaceTransaction(workspace)

            with mock.patch.object(
                transaction,
                "_rollback",
                side_effect=OSError("injected interrupted rollback"),
            ):
                with transaction:
                    common.write_utf8(transaction.stage_path(first), "new-first\n")
                    common.write_utf8(transaction.stage_path(second), "new-second\n")
                    with (
                        mock.patch.object(
                            common,
                            "update_stages",
                            side_effect=OSError("injected manifest failure"),
                        ),
                        self.assertRaises(common.WorkspaceTransactionError),
                    ):
                        transaction.commit(
                            {"dry_run": ("done", {"report": "report/first.json"})}
                        )

            real_replace = common.os.replace

            def fail_second_restore(source: object, target: object) -> None:
                if Path(target) == second:
                    raise OSError("injected recovery failure")
                real_replace(source, target)

            with (
                mock.patch.object(common.os, "replace", side_effect=fail_second_restore),
                self.assertRaises(common.WorkspaceTransactionError),
            ):
                common.recover_workspace_transactions(workspace)

            common.recover_workspace_transactions(workspace)
            self.assertEqual(first.read_text(encoding="utf-8"), "old-first\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "old-second\n")
            self.assert_no_runs(workspace)

    def test_recovery_rejects_unknown_run_directories_without_deleting_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            sentinel = workspace / ".runs" / "user-backup" / "keep.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("keep\n", encoding="utf-8")

            with self.assertRaisesRegex(common.WorkspaceTransactionError, "unknown run"):
                common.recover_workspace_transactions(workspace)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_recovery_preflights_protected_targets_before_rolling_back_any_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, workspace = self.bind_workspace(Path(directory))
            run_id = "a" * 32
            run_root = workspace / ".runs" / run_id
            ordinary = workspace / "report" / "ordinary.json"
            ordinary.write_text("published\n", encoding="utf-8")
            common.write_utf8(run_root / "run.marker", run_id)
            common.write_json(
                run_root / "journal.json",
                {
                    "schema_version": 2,
                    "run_id": run_id,
                    "deferred": False,
                    "updates": [{"stage": "dry_run", "status": "done"}],
                    "entries": [
                        {
                            "target": "report/ordinary.json",
                            "delete": False,
                            "existed": False,
                            "backup_sha256": None,
                            "backup_size_bytes": None,
                            "staged_sha256": common.sha256_file(ordinary),
                            "staged_size_bytes": ordinary.stat().st_size,
                        },
                        {
                            "target": "manifest.json",
                            "delete": False,
                            "existed": True,
                            "backup_sha256": common.sha256_file(
                                workspace / "manifest.json"
                            ),
                            "backup_size_bytes": (
                                workspace / "manifest.json"
                            ).stat().st_size,
                            "staged_sha256": common.sha256_file(
                                workspace / "manifest.json"
                            ),
                            "staged_size_bytes": (
                                workspace / "manifest.json"
                            ).stat().st_size,
                        },
                    ],
                    "directories": [],
                    "group_commits": [],
                },
            )
            protected_before = {
                path: path.read_bytes()
                for path in (
                    workspace / "manifest.json",
                    workspace / "versions" / "v0_original.txt",
                    source,
                )
            }

            with self.assertRaisesRegex(common.WorkspaceTransactionError, "cannot recover"):
                common.recover_workspace_transactions(workspace)

            self.assertEqual(ordinary.read_text(encoding="utf-8"), "published\n")
            for path, content in protected_before.items():
                self.assertEqual(path.read_bytes(), content)
            self.assertTrue(run_root.is_dir())

    def test_deferred_recovery_requires_manifest_backup_before_rolling_back_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.bind_workspace(root)
            other_root = root / "other"
            other_root.mkdir()
            _, other_workspace = self.bind_workspace(other_root)
            run_id = "b" * 32
            export_report = workspace / "report/export_report.json"
            with common.WorkspaceTransaction(workspace, run_id=run_id) as transaction:
                common.write_json(transaction.stage_path(export_report), {"status": "done"})
                transaction.commit(
                    {"7_export": ("done", {"report": "report/export_report.json"})}
                )
            current_manifest = (workspace / "manifest.json").read_bytes()
            report = workspace / "report" / "export_report.json"
            report.write_text("new-report\n", encoding="utf-8")
            run_root = workspace / ".runs" / run_id
            backup = run_root / "backups" / "report" / "export_report.json"
            common.write_utf8(backup, "old-report\n")
            common.write_utf8(run_root / "run.marker", run_id)
            common.write_json(
                run_root / "journal.json",
                {
                    "schema_version": 2,
                    "run_id": run_id,
                    "deferred": True,
                    "updates": [{"stage": "7_export", "status": "done"}],
                    "entries": [
                        {
                            "target": "report/export_report.json",
                            "delete": False,
                            "existed": True,
                            "backup_sha256": common.sha256_file(backup),
                            "backup_size_bytes": backup.stat().st_size,
                            "staged_sha256": common.sha256_file(report),
                            "staged_size_bytes": report.stat().st_size,
                        }
                    ],
                    "directories": [],
                    "group_commits": [
                        {
                            "workspace": str(workspace),
                            "stage": "7_export",
                            "status": "done",
                        },
                        {
                            "workspace": str(other_workspace),
                            "stage": "7_export",
                            "status": "done",
                        },
                    ],
                },
            )

            with self.assertRaisesRegex(
                common.WorkspaceTransactionError,
                "manifest backup is invalid",
            ):
                common.recover_workspace_transactions(workspace)

            self.assertEqual(report.read_text(encoding="utf-8"), "new-report\n")
            self.assertEqual(backup.read_text(encoding="utf-8"), "old-report\n")
            self.assertEqual((workspace / "manifest.json").read_bytes(), current_manifest)
            self.assertTrue(run_root.is_dir())

    def test_deferred_recovery_rejects_a_replaced_manifest_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.bind_workspace(root)
            other_root = root / "other"
            other_root.mkdir()
            _, other_workspace = self.bind_workspace(other_root)
            original_manifest = (workspace / "manifest.json").read_bytes()
            run_id = "d" * 32
            export_report = workspace / "report/export_report.json"
            with common.WorkspaceTransaction(workspace, run_id=run_id) as transaction:
                common.write_json(transaction.stage_path(export_report), {"status": "done"})
                transaction.commit(
                    {"7_export": ("done", {"report": "report/export_report.json"})}
                )
            current_manifest = (workspace / "manifest.json").read_bytes()
            report = workspace / "report" / "export_report.json"
            report.write_text("new-report\n", encoding="utf-8")
            run_root = workspace / ".runs" / run_id
            report_backup = run_root / "backups" / "report" / "export_report.json"
            common.write_utf8(report_backup, "old-report\n")
            manifest_backup = run_root / "manifest.backup.json"
            manifest_backup.write_bytes(original_manifest)
            original_backup_sha = common.sha256_file(manifest_backup)
            common.write_utf8(run_root / "run.marker", run_id)
            common.write_json(
                run_root / "journal.json",
                {
                    "schema_version": 2,
                    "run_id": run_id,
                    "deferred": True,
                    "manifest_backup_sha256": original_backup_sha,
                    "updates": [{"stage": "7_export", "status": "done"}],
                    "entries": [
                        {
                            "target": "report/export_report.json",
                            "delete": False,
                            "existed": True,
                            "backup_sha256": common.sha256_file(report_backup),
                            "backup_size_bytes": report_backup.stat().st_size,
                            "staged_sha256": common.sha256_file(report),
                            "staged_size_bytes": report.stat().st_size,
                        }
                    ],
                    "directories": [],
                    "group_commits": [
                        {
                            "workspace": str(workspace),
                            "stage": "7_export",
                            "status": "done",
                        },
                        {
                            "workspace": str(other_workspace),
                            "stage": "7_export",
                            "status": "done",
                        },
                    ],
                },
            )
            common.write_json(manifest_backup, {})
            replaced_backup = manifest_backup.read_bytes()

            with self.assertRaisesRegex(
                common.WorkspaceTransactionError,
                "manifest backup checksum is invalid",
            ):
                common.recover_workspace_transactions(workspace)

            self.assertEqual(report.read_text(encoding="utf-8"), "new-report\n")
            self.assertEqual(report_backup.read_text(encoding="utf-8"), "old-report\n")
            self.assertEqual((workspace / "manifest.json").read_bytes(), current_manifest)
            self.assertEqual(manifest_backup.read_bytes(), replaced_backup)
            self.assertTrue(run_root.is_dir())

    def test_recovery_cannot_roll_back_an_active_workspace_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            target = workspace / "report" / "active.json"

            with common.WorkspaceTransaction(workspace) as transaction:
                staged = transaction.stage_path(target)
                common.write_utf8(staged, "active\n")
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "active"):
                    common.recover_workspace_transactions(workspace)
                self.assertEqual(staged.read_text(encoding="utf-8"), "active\n")

            self.assertFalse(target.exists())
            self.assert_no_runs(workspace)

    def test_transaction_recovery_namespace_cannot_be_used_as_an_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))

            with self.assertRaisesRegex(common.WorkspacePathError, "reserved"):
                common.resolve_workspace_paths(
                    workspace,
                    writes={"poison": ".runs/poison.json"},
                )
            with common.WorkspaceTransaction(workspace) as transaction:
                with self.assertRaisesRegex(common.WorkspacePathError, "recovery namespace"):
                    transaction.stage_path(workspace / ".runs" / "poison.json")

            self.assert_no_runs(workspace)

    def test_transaction_file_targets_cannot_replace_existing_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            sentinel = workspace / "report" / "keep.txt"
            sentinel.write_text("keep\n", encoding="utf-8")

            with common.WorkspaceTransaction(workspace) as transaction:
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a file"):
                    transaction.stage_path(workspace / "report")
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a file"):
                    transaction.stage_delete(workspace / "report")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
            self.assertTrue((workspace / "report").is_dir())
            self.assert_no_runs(workspace)

    def test_transaction_delete_removes_a_stale_artifact_on_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            stale = workspace / "candidates" / "ads_pages" / "page-0002.jsonl"
            stale.parent.mkdir(parents=True)
            stale.write_text('{"stale":true}\n', encoding="utf-8")
            report = workspace / "report/dry_run_report.json"

            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_delete(stale)
                common.write_json(transaction.stage_path(report), {"deleted": "page-0002.jsonl"})
                transaction.commit(
                    {
                        "dry_run": (
                            "done",
                            {
                                "deleted": "page-0002.jsonl",
                                "report": "report/dry_run_report.json",
                            },
                        )
                    }
                )

            self.assertFalse(stale.exists())
            self.assertEqual(
                common.load_manifest(workspace)["stages"]["dry_run"]["status"],
                "done",
            )
            self.assert_no_runs(workspace)

    def test_journal_is_never_published_before_existing_targets_are_backed_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            stale = workspace / "candidates" / "ads_pages" / "page-0002.jsonl"
            stale.parent.mkdir(parents=True)
            stale.write_text('{"stale":true}\n', encoding="utf-8")
            report = workspace / "report/dry_run_report.json"
            report.write_text('{"version":"old"}\n', encoding="utf-8")
            manifest_before = (workspace / "manifest.json").read_bytes()
            transaction = common.WorkspaceTransaction(workspace)
            transaction.__enter__()
            transaction.stage_delete(stale)
            common.write_json(
                transaction.stage_path(report),
                {"version": "new"},
            )
            real_write_journal = transaction._write_journal

            def exit_after_journal(*args: object, **kwargs: object) -> None:
                real_write_journal(*args, **kwargs)
                self.assertTrue(transaction._backup_path(transaction._entries[stale]).is_file())
                self.assertTrue(transaction._backup_path(transaction._entries[report]).is_file())
                raise SimulatedProcessExit("injected process exit after journal")

            try:
                with (
                    mock.patch.object(
                        transaction,
                        "_write_journal",
                        side_effect=exit_after_journal,
                    ),
                    self.assertRaisesRegex(SimulatedProcessExit, "after journal"),
                ):
                    transaction.commit(
                        {
                            "dry_run": (
                                "done",
                                {"report": "report/dry_run_report.json"},
                            )
                        }
                    )
            finally:
                transaction._lock.release()

            common.recover_workspace_transactions(workspace)

            self.assertEqual(stale.read_text(encoding="utf-8"), '{"stale":true}\n')
            self.assertEqual(report.read_text(encoding="utf-8"), '{"version":"old"}\n')
            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            self.assert_no_runs(workspace)

    def test_prebackup_interruption_never_publishes_a_journal_or_changes_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            stale = workspace / "candidates" / "ads_pages" / "page-0002.jsonl"
            stale.parent.mkdir(parents=True)
            stale.write_text('{"stale":true}\n', encoding="utf-8")
            report = workspace / "report/dry_run_report.json"
            report.write_text('{"version":"old"}\n', encoding="utf-8")
            manifest_before = (workspace / "manifest.json").read_bytes()
            transaction = common.WorkspaceTransaction(workspace)
            transaction.__enter__()
            transaction.stage_delete(stale)
            common.write_json(transaction.stage_path(report), {"version": "new"})
            report_backup = transaction._backup_path(transaction._entries[report])
            real_copy = common._atomic_copy_file

            def exit_during_prebackup(source: Path, target: Path) -> None:
                if target == report_backup:
                    raise SimulatedProcessExit("injected process exit during prebackup")
                real_copy(source, target)

            try:
                with (
                    mock.patch.object(
                        common,
                        "_atomic_copy_file",
                        side_effect=exit_during_prebackup,
                    ),
                    self.assertRaisesRegex(SimulatedProcessExit, "during prebackup"),
                ):
                    transaction.commit(
                        {
                            "dry_run": (
                                "done",
                                {"report": "report/dry_run_report.json"},
                            )
                        }
                    )
            finally:
                transaction._lock.release()

            self.assertFalse((transaction.root / "journal.json").exists())
            common.recover_workspace_transactions(workspace)
            self.assertEqual(stale.read_text(encoding="utf-8"), '{"stale":true}\n')
            self.assertEqual(report.read_text(encoding="utf-8"), '{"version":"old"}\n')
            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            self.assert_no_runs(workspace)

    def test_recovery_rejects_tampered_backup_or_target_before_any_rollback(self) -> None:
        for tamper in ("backup", "target"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as directory:
                _, workspace = self.bind_workspace(Path(directory))
                report = workspace / "report/dry_run_report.json"
                report.write_text('{"version":"old"}\n', encoding="utf-8")
                manifest_before = (workspace / "manifest.json").read_bytes()
                transaction = common.WorkspaceTransaction(workspace)
                transaction.__enter__()
                common.write_json(transaction.stage_path(report), {"version": "new"})
                real_write_journal = transaction._write_journal

                def exit_after_journal(*args: object, **kwargs: object) -> None:
                    real_write_journal(*args, **kwargs)
                    raise SimulatedProcessExit("injected process exit after journal")

                try:
                    with (
                        mock.patch.object(
                            transaction,
                            "_write_journal",
                            side_effect=exit_after_journal,
                        ),
                        self.assertRaises(SimulatedProcessExit),
                    ):
                        transaction.commit(
                            {
                                "dry_run": (
                                    "done",
                                    {"report": "report/dry_run_report.json"},
                                )
                            }
                        )
                finally:
                    transaction._lock.release()

                backup = transaction._backup_path(transaction._entries[report])
                if tamper == "backup":
                    backup.write_text("unknown backup\n", encoding="utf-8")
                else:
                    report.write_text("unknown target\n", encoding="utf-8")
                target_before = report.read_bytes()
                backup_before = backup.read_bytes()

                with self.assertRaisesRegex(
                    common.WorkspaceTransactionError,
                    "checksum is invalid|content is unrecognized",
                ):
                    common.recover_workspace_transactions(workspace)

                self.assertEqual(report.read_bytes(), target_before)
                self.assertEqual(backup.read_bytes(), backup_before)
                self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
                self.assertTrue(transaction.root.is_dir())

    def test_commit_rejects_a_target_changed_after_the_journal_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            target = workspace / "report/delete-me.json"
            target.write_text("old target\n", encoding="utf-8")
            manifest_before = (workspace / "manifest.json").read_bytes()

            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_delete(target)
                common.write_json(
                    transaction.stage_path(workspace / "report/dry_run_report.json"),
                    {"deleted": "report/delete-me.json"},
                )
                real_write_journal = transaction._write_journal

                def replace_target_after_journal(
                    *args: object,
                    **kwargs: object,
                ) -> None:
                    real_write_journal(*args, **kwargs)
                    target.write_text("concurrent replacement\n", encoding="utf-8")

                with (
                    mock.patch.object(
                        transaction,
                        "_write_journal",
                        side_effect=replace_target_after_journal,
                    ),
                    self.assertRaisesRegex(
                        common.WorkspaceTransactionError,
                        "changed after backup",
                    ),
                ):
                    transaction.commit(
                        {
                            "dry_run": (
                                "done",
                                {
                                    "deleted": "report/delete-me.json",
                                    "report": "report/dry_run_report.json",
                                },
                            )
                        }
                    )

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "concurrent replacement\n",
            )
            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            self.assert_no_runs(workspace)

    def test_recovery_rejects_removed_identity_fields_without_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            report = workspace / "report/dry_run_report.json"
            report.write_text('{"version":"old"}\n', encoding="utf-8")
            manifest_before = (workspace / "manifest.json").read_bytes()
            transaction = common.WorkspaceTransaction(workspace)
            transaction.__enter__()
            common.write_json(transaction.stage_path(report), {"version": "new"})
            real_write_journal = transaction._write_journal

            def exit_after_journal(*args: object, **kwargs: object) -> None:
                real_write_journal(*args, **kwargs)
                raise SimulatedProcessExit("injected process exit after journal")

            try:
                with (
                    mock.patch.object(
                        transaction,
                        "_write_journal",
                        side_effect=exit_after_journal,
                    ),
                    self.assertRaises(SimulatedProcessExit),
                ):
                    transaction.commit(
                        {
                            "dry_run": (
                                "done",
                                {"report": "report/dry_run_report.json"},
                            )
                        }
                    )
            finally:
                transaction._lock.release()

            journal_path = transaction.root / "journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            entry = journal["entries"][0]
            for field in (
                "backup_sha256",
                "backup_size_bytes",
                "staged_sha256",
                "staged_size_bytes",
            ):
                entry.pop(field)
            common.write_json(journal_path, journal)
            backup = transaction._backup_path(transaction._entries[report])
            backup.write_text("unknown backup\n", encoding="utf-8")
            target_before = report.read_bytes()
            backup_before = backup.read_bytes()

            with self.assertRaisesRegex(
                common.WorkspaceTransactionError,
                "identity fields",
            ):
                common.recover_workspace_transactions(workspace)

            self.assertEqual(report.read_bytes(), target_before)
            self.assertEqual(backup.read_bytes(), backup_before)
            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            self.assertTrue(transaction.root.is_dir())

    def test_require_new_workspace_directory_is_published_by_one_tree_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            bundle = workspace / "output" / "book"
            real_replace = common.os.replace
            published_targets: list[Path] = []

            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_directory(bundle, require_new=True)
                common.write_utf8(transaction.stage_path(bundle / "book.txt"), "text\n")
                common.write_utf8(transaction.stage_path(bundle / "book.md"), "markdown\n")

                def observe_replace(source: object, target: object) -> None:
                    target_path = Path(target)
                    published_targets.append(target_path)
                    if target_path == bundle:
                        source_path = Path(source)
                        self.assertTrue((source_path / "book.txt").is_file())
                        self.assertTrue((source_path / "book.md").is_file())
                        self.assertFalse(bundle.exists())
                    real_replace(source, target)

                with mock.patch.object(common.os, "replace", side_effect=observe_replace):
                    transaction.commit(
                        {"dry_run": ("done", {"output": "output/book"})}
                    )

            self.assertIn(bundle, published_targets)
            self.assertNotIn(bundle / "book.txt", published_targets)
            self.assertNotIn(bundle / "book.md", published_targets)
            self.assertEqual((bundle / "book.txt").read_text(encoding="utf-8"), "text\n")
            self.assertEqual((bundle / "book.md").read_text(encoding="utf-8"), "markdown\n")
            self.assert_no_runs(workspace)

    def test_workspace_transaction_lock_is_reentrant_in_the_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = self.bind_workspace(Path(directory))
            target = workspace / "report" / "nested-lock.json"

            with common.workspace_transaction_lock(workspace):
                resolved, _, _ = common.resolve_workspace_paths(workspace)
                with common.WorkspaceTransaction(workspace) as transaction:
                    common.write_json(transaction.stage_path(target), {"staged": True})

            self.assertEqual(resolved, workspace)
            self.assertFalse(target.exists())
            self.assert_no_runs(workspace)


if __name__ == "__main__":
    unittest.main()
