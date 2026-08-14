from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import preprocess  # noqa: E402


def make_workspace(root: Path, name: str = "book") -> tuple[Path, Path]:
    source = root / f"{name}.txt"
    source.write_text("第一章 示例\n正文。\n", encoding="utf-8")
    workspace = root / f"{name}.txt.cleanwork"
    preprocess.run(source, str(workspace), "utf-8")
    return source.resolve(), workspace.resolve()


class CommonManifestCoverageF7Tests(unittest.TestCase):
    def test_manifest_v2_rejects_every_ledger_shape_binding_and_lineage_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, workspace = make_workspace(Path(directory))
            base = common.load_manifest(workspace)
            common._validate_manifest_v2(workspace, base)
            current = str(base["current_head"])
            v0 = "versions/v0_original.txt"
            active = "0_preprocess"

            mutations = {
                "schema": lambda m: m.update(schema_version=1),
                "artifacts missing": lambda m: m.update(artifacts={}),
                "head missing": lambda m: m.update(current_head=""),
                "stages missing": lambda m: m.update(stages=[]),
                "stage entry": lambda m: m["stages"].update({"bad/stage": {}}),
                "stage status": lambda m: m["stages"][active].update(status="unknown"),
                "stage artifacts type": lambda m: m["stages"][active].update(artifacts="bad"),
                "active run": lambda m: m["stages"][active].update(run_id="bad"),
                "active empty": lambda m: m["stages"][active].update(artifacts=[]),
                "stage untracked": lambda m: m["stages"][active].update(artifacts=["report/missing.json"]),
                "artifact entry": lambda m: m["artifacts"].update({"report/bad.json": []}),
                "artifact path unsafe": lambda m: m["artifacts"].update({"../bad": {}}),
                "artifact path mismatch": lambda m: m["artifacts"][current].update(path="other"),
                "artifact hash": lambda m: m["artifacts"][current].update(sha256="bad"),
                "artifact size": lambda m: m["artifacts"][current].update(size_bytes=True),
                "artifact run": lambda m: m["artifacts"][current].update(run_id="bad"),
                "artifact stage": lambda m: m["artifacts"][current].update(stage=""),
                "parent path": lambda m: m["artifacts"][current].update(parent_path=""),
                "parent hash": lambda m: m["artifacts"][current].update(parent_sha256="bad"),
                "owner stage": lambda m: m["artifacts"][m["stages"][active]["artifacts"][0]].update(stage="other"),
                "owner run": lambda m: m["artifacts"][m["stages"][active]["artifacts"][0]].update(run_id="1" * 32),
                "v0 missing": lambda m: m["artifacts"].pop(v0),
                "head untracked": lambda m: m["artifacts"].pop(current),
                "lineage cycle": lambda m: m["artifacts"][current].update(
                    parent_path=current,
                    parent_sha256=m["artifacts"][current]["sha256"],
                ),
                "lineage incomplete": lambda m: m["artifacts"][current].update(
                    parent_path="versions/missing.txt",
                    parent_sha256="a" * 64,
                ),
                "untrusted root": lambda m: (
                    m.update(current_head=v0),
                    m["artifacts"][v0].update(parent_sha256="a" * 64),
                ),
                "parent inconsistent": lambda m: m["artifacts"][current].update(parent_sha256="a" * 64),
            }
            for label, mutate in mutations.items():
                manifest = copy.deepcopy(base)
                mutate(manifest)
                with self.subTest(label=label), self.assertRaises(common.WorkspaceIdentityError):
                    common._validate_manifest_v2(workspace, manifest)

            artifact_paths = [
                workspace / relative
                for relative in base["stages"][active]["artifacts"]
            ]
            owned = artifact_paths[0]
            original = owned.read_bytes()
            owned.unlink()
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "artifact is missing"):
                common._validate_manifest_v2(workspace, base)
            owned.write_bytes(original + b"x")
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "size does not match"):
                common._validate_manifest_v2(workspace, base)
            owned.write_bytes(b"x" * len(original))
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "content does not match"):
                common._validate_manifest_v2(workspace, base)
            owned.write_bytes(original)

            current_path = workspace / current
            current_bytes = current_path.read_bytes()
            head_manifest = copy.deepcopy(base)
            head_manifest["stages"][active] = {"status": "pending", "artifacts": []}
            current_path.unlink()
            with self.assertRaisesRegex(
                common.WorkspaceIdentityError, "current_head.*artifact is missing"
            ):
                common._validate_manifest_v2(workspace, head_manifest)
            current_path.write_bytes(current_bytes + b"x")
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "current_head.*size"):
                common._validate_manifest_v2(workspace, head_manifest)
            current_path.write_bytes(b"x" * len(current_bytes))
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "current_head.*content"):
                common._validate_manifest_v2(workspace, head_manifest)
            current_path.write_bytes(current_bytes)
            self.assertTrue(source.is_file())

    def test_bound_snapshot_identity_rejects_manifest_source_and_v0_mismatches(self) -> None:
        mutations = {
            "not object": lambda m, _s, _w: [],
            "identity missing": lambda m, _s, _w: {**m, "source": None},
            "workspace": lambda m, _s, _w: {**m, "workspace": "C:/wrong"},
            "v0 path": lambda m, _s, _w: {**m, "v0": {**m["v0"], "path": "bad"}},
            "source path missing": lambda m, _s, _w: {**m, "source": {**m["source"], "path": ""}},
            "source path relative": lambda m, _s, _w: {**m, "source": {**m["source"], "path": "relative.txt"}},
            "source path noncanonical": lambda m, s, _w: {
                **m,
                "source": {**m["source"], "path": str(s.parent / ".." / s.parent.name / s.name)},
            },
            "source name": lambda m, _s, _w: {**m, "source": {**m["source"], "name": "wrong"}},
            "source hash": lambda m, _s, _w: {**m, "source": {**m["source"], "sha256": "a" * 64}},
            "source size": lambda m, _s, _w: {**m, "source": {**m["source"], "size_bytes": -1}},
        }
        for label, mutate in mutations.items():
            with tempfile.TemporaryDirectory() as directory:
                source, workspace = make_workspace(Path(directory), label.replace(" ", "-"))
                manifest_path = workspace / "manifest.json"
                manifest = common.load_manifest(workspace)
                changed = mutate(copy.deepcopy(manifest), source, workspace)
                manifest_path.write_text(json.dumps(changed), encoding="utf-8")
                with self.subTest(label=label), self.assertRaises(common.WorkspaceIdentityError):
                    common._validate_bound_snapshot_identity(workspace)

        with tempfile.TemporaryDirectory() as directory:
            source, workspace = make_workspace(Path(directory), "alias")
            manifest = common.load_manifest(workspace)
            v0 = workspace / "versions/v0_original.txt"
            manifest["source"].update(
                path=str(v0),
                name=v0.name,
                sha256=common.sha256_file(v0),
                size_bytes=v0.stat().st_size,
            )
            (workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "aliases"):
                common._validate_bound_snapshot_identity(workspace)
            self.assertTrue(source.is_file())

    def test_init_workspace_rejects_source_and_shell_failures_and_tolerates_chmod_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                common.init_workspace_from_source(root / "missing.txt", root / "w.cleanwork")
            source_dir = root / "source-dir"
            source_dir.mkdir()
            with self.assertRaisesRegex(ValueError, "not a file"):
                common.init_workspace_from_source(source_dir, root / "w.cleanwork")
            source = root / "book.txt"
            source.write_text("正文", encoding="utf-8")
            conflict = root / "conflict.cleanwork" / "versions" / "v0_original.txt"
            conflict.parent.mkdir(parents=True)
            conflict.write_text("正文", encoding="utf-8")
            with self.assertRaises(common.WorkspacePathError):
                common.init_workspace_from_source(conflict, root / "conflict.cleanwork")

            partial = root / "partial.cleanwork"
            (partial / "versions").mkdir(parents=True)
            (partial / "versions/v0_original.txt").write_text("正文", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "incomplete"):
                common.init_workspace_from_source(source, partial)

            unbound = root / "unbound.cleanwork"
            unbound.mkdir()
            (unbound / "extra.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "non-empty"):
                common.init_workspace_from_source(source, unbound)

            workspace = root / "chmod.cleanwork"
            with mock.patch.object(Path, "chmod", side_effect=OSError("denied")):
                manifest = common.init_workspace_from_source(source, workspace)
            self.assertEqual(manifest["schema_version"], 2)
            reused = common.init_workspace_from_source(source, workspace)
            self.assertEqual(reused["workspace"], str(workspace.resolve()))

    def test_artifact_and_json_helpers_cover_invalid_and_empty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.txt"
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "missing"):
                common._artifact_record(missing, "report/missing.txt", run_id="a" * 32, stage="x")
            file = root / "file.txt"
            file.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "run_id"):
                common._artifact_record(file, "report/file.txt", run_id="bad", stage="x")
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "config_sha256"):
                common._artifact_record(
                    file,
                    "report/file.txt",
                    run_id="a" * 32,
                    stage="x",
                    config_sha256="bad",
                )
            empty = root / "empty.jsonl"
            empty.write_text("\n\n", encoding="utf-8")
            self.assertEqual(common.load_jsonl(empty), [])
            with self.assertRaisesRegex(ValueError, "run_id"):
                common.load_jsonl_for_run(empty, "")
            self.assertEqual(common.load_manifest(root), {})


class CommonIdentityAndPathCoverageF7Tests(unittest.TestCase):
    def test_core_path_guards_reject_invalid_boundaries_and_unheld_release_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            lock = common._PathTransactionLock("workspace", root / "unused.cleanwork")
            lock.release()

            with self.assertRaisesRegex(common.WorkspacePathError, "non-empty relative string"):
                common._validate_internal_value("")
            with self.assertRaisesRegex(common.WorkspacePathError, "does not exist"):
                common.validate_workspace(root / "missing.cleanwork")

            regular_file = root / "not-a-workspace.cleanwork"
            regular_file.write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspacePathError, "not a directory"):
                common.validate_workspace(regular_file)

            workspace = root / "book.txt.cleanwork"
            workspace.mkdir()
            with self.assertRaisesRegex(common.WorkspacePathError, "escapes the workspace"):
                common._workspace_transaction_target(workspace, root / "outside.txt")

    def test_workspace_identity_rejects_each_bound_source_and_snapshot_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, workspace = make_workspace(Path(directory))
            base = common.load_manifest(workspace)
            v0 = workspace / "versions/v0_original.txt"

            mutations = {
                "identities": lambda m: m.update(source=None),
                "source path": lambda m: m["source"].update(path="C:/wrong.txt"),
                "workspace path": lambda m: m.update(workspace="C:/wrong.cleanwork"),
                "v0 path": lambda m: m["v0"].update(path="versions/wrong.txt"),
                "source hash": lambda m: m["source"].update(sha256="a" * 64),
                "v0 hash": lambda m: m["v0"].update(sha256="a" * 64),
                "source name": lambda m: m["source"].update(name="wrong.txt"),
                "source size": lambda m: m["source"].update(size_bytes=-1),
                "v0 size": lambda m: m["v0"].update(size_bytes=-1),
            }
            for label, mutate in mutations.items():
                manifest = copy.deepcopy(base)
                mutate(manifest)
                with self.subTest(label=label), self.assertRaises(common.WorkspaceIdentityError):
                    common._validate_workspace_identity(source, workspace, v0, manifest)

            original = v0.read_bytes()
            v0.chmod(0o666)
            v0.write_bytes(original + b"x")
            changed = copy.deepcopy(base)
            changed["v0"].update(
                sha256=common.sha256_file(v0),
                size_bytes=v0.stat().st_size,
            )
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "identities do not match"):
                common._validate_workspace_identity(source, workspace, v0, changed)
            v0.write_bytes(original)

            v0.unlink()
            v0.mkdir()
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "not a file"):
                common._validate_workspace_identity(source, workspace, v0, base)

    def test_update_stages_rejects_invalid_updates_without_persisting_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))

            with self.assertRaisesRegex(ValueError, "at least one"):
                common.update_stages(workspace, {})
            for updates, message in (
                ({"unknown": ("done", {})}, "stage name"),
                ({"review": ("unknown", {})}, "stage status"),
                ({"review": ("done", {"_artifact_records": []})}, "artifact records"),
                ({"review": ("done", {"_deleted_artifacts": [1]})}, "deleted artifact"),
                (
                    {
                        "review": (
                            "done",
                            {"_deleted_artifacts": ["versions/v1_preprocessed.txt"]},
                        )
                    },
                    "current_head",
                ),
                ({"review": ("done", {"_current_head": "report/missing.txt"})}, "current_head"),
            ):
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    common.update_stages(workspace, updates)

            manifest = common.load_manifest(workspace)
            manifest["stages"].setdefault("review", {})
            manifest["stages"]["review"]["status"] = "invalid"
            common.write_json(workspace / "manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "existing stage status"):
                common.update_stages(workspace, {"review": ("done", {})}, _published_artifacts=True)

            manifest["stages"]["review"]["status"] = "skipped"
            common.write_json(workspace / "manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "illegal stage status transition"):
                common.update_stages(workspace, {"review": ("done", {})}, _published_artifacts=True)

    def test_manifest_and_internal_path_helpers_cover_invalid_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            self.assertIsNone(common._manifest_source_path(workspace))

            manifest_path = workspace / "manifest.json"
            manifest_path.write_text("[1]", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspacePathError, "JSON object"):
                common._manifest_source_path(workspace)
            manifest_path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspacePathError, "cannot read"):
                common._manifest_source_path(workspace)
            manifest_path.write_text(json.dumps({"source": {"path": "source.txt"}}), encoding="utf-8")
            self.assertEqual(common._manifest_source_path(workspace), workspace / "source.txt")

            with self.assertRaisesRegex(common.WorkspacePathError, "unsupported.*role"):
                common._resolve_in_validated_workspace(workspace, "a.txt", "bad", (), ())
            for value, message in (
                (".runs/a", "reserved"),
                ("dir/a:b", "alternate data stream"),
                ("a. ", "trailing spaces"),
            ):
                with self.subTest(value=value), self.assertRaisesRegex(common.WorkspacePathError, message):
                    common._resolve_in_validated_workspace(workspace, value, "read", (), ())

    def test_workspace_path_preflight_rejects_missing_alias_nested_and_duplicate_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(common.WorkspacePathError, "does not exist"):
                common.resolve_workspace_paths(root / "missing")

            _, workspace = make_workspace(root)
            manifest = workspace / "manifest.json"
            with self.assertRaisesRegex(common.WorkspacePathError, "manifest update target aliases"):
                common.resolve_workspace_paths(
                    workspace,
                    reads={"manifest": "manifest.json"},
                )

            read_directory = workspace / "meta/read-dir"
            read_directory.mkdir()
            with self.assertRaisesRegex(common.WorkspacePathError, "inside read directory"):
                common.resolve_workspace_paths(
                    workspace,
                    reads={"input": "meta/read-dir"},
                    writes={"output": "meta/read-dir/output.json"},
                )

            with self.assertRaisesRegex(common.WorkspacePathError, "write targets conflict"):
                common.resolve_workspace_paths(
                    workspace,
                    writes={"left": "report/a.json", "right": "report/a.json"},
                )

            with mock.patch.object(common, "_validate_bound_snapshot_identity", return_value=None):
                changed = common.load_manifest(workspace)
                changed["current_head"] = "report/a.json"
                common.write_json(manifest, changed)
                with self.assertRaisesRegex(common.WorkspaceIdentityError, "versioned text"):
                    common.resolve_current_head(workspace)
                changed["current_head"] = "versions/missing.json"
                common.write_json(manifest, changed)
                with self.assertRaisesRegex(common.WorkspaceIdentityError, "readable text"):
                    common.resolve_current_head(workspace)

    def test_external_path_preflight_rejects_files_roots_overlaps_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_file = root / "output"
            output_file.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspacePathError, "not a directory"):
                common.resolve_external_output_dir(output_file)
            with self.assertRaisesRegex(common.WorkspacePathError, "filesystem roots"):
                common.resolve_external_output_dir(Path(output_file.anchor))

            source, workspace = make_workspace(root, "external")
            with self.assertRaisesRegex(common.WorkspacePathError, "overlaps"):
                common.resolve_external_output_dir(workspace / "delivery", workspaces=(workspace,))
            with self.assertRaisesRegex(common.WorkspacePathError, "overlaps"):
                common.resolve_external_output_dir(root, workspaces=(workspace,))

            delivery = root / "delivery"
            resolved = common.resolve_external_output_path(delivery, "book/a.txt")
            self.assertEqual(resolved, delivery / "book/a.txt")
            with self.assertRaisesRegex(common.WorkspacePathError, "protected input"):
                common.resolve_external_output_path(
                    source.parent,
                    source.name,
                    inputs=(source,),
                )
            with self.assertRaisesRegex(common.WorkspacePathError, "targets conflict"):
                common.resolve_external_output_paths(
                    delivery,
                    writes={"left": "a.txt", "right": "a.txt"},
                )

            protected = (source,)
            with self.assertRaisesRegex(common.WorkspacePathError, "delivery root"):
                common._external_transaction_target(delivery, delivery, protected)
            with self.assertRaisesRegex(common.WorkspacePathError, "recovery state"):
                common._external_transaction_target(
                    delivery,
                    delivery / ".delivery-runs/a",
                    protected,
                )
            with self.assertRaisesRegex(common.WorkspacePathError, "protected input"):
                common._external_transaction_target(source.parent, source, protected)


class CommonTransactionApiCoverageF7Tests(unittest.TestCase):
    def test_process_lock_rejects_a_second_direct_acquire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = common._PathTransactionLock(
                Path(directory),
                "workspace",
                allow_children=False,
            )
            lock.acquire()
            try:
                with self.assertRaisesRegex(
                    common.WorkspaceTransactionError,
                    "already held",
                ):
                    lock.acquire()
            finally:
                lock.release()

    def test_workspace_transaction_stage_api_rejects_conflicts_and_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            target = workspace / "report/item.json"
            existing_dir = workspace / "report/existing"
            existing_dir.mkdir()
            existing_file = workspace / "report/file"
            existing_file.write_text("x", encoding="utf-8")

            with self.assertRaises(common.WorkspaceTransactionError):
                common.WorkspaceTransaction(workspace, run_id="bad")
            with common.WorkspaceTransaction(workspace) as transaction:
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a file"):
                    transaction.stage_path(existing_dir)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a file"):
                    transaction.stage_delete(existing_dir)
                staged = transaction.stage_path(target)
                self.assertEqual(transaction.stage_path(target), staged)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "also be deleted"):
                    transaction.stage_delete(target)
                transaction.discard_unwritten_stage(target)
                self.assertNotIn(target, transaction._entries)

            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_delete(target)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "also be staged"):
                    transaction.stage_path(target)

            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(target).write_text("x", encoding="utf-8")
                transaction.discard_unwritten_stage(target)
                self.assertIn(target, transaction._entries)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "file target"):
                    transaction.stage_directory(target)

            with common.WorkspaceTransaction(workspace) as transaction:
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a directory"):
                    transaction.stage_directory(existing_file)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "already exists"):
                    transaction.stage_directory(existing_dir, require_new=True)
                first = workspace / "output/new"
                transaction.stage_directory(first, require_new=True)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "overlap"):
                    transaction.stage_directory(first / "child", require_new=True)

    def test_workspace_commit_validation_and_lineage_errors_roll_back_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            report = workspace / "report/new.json"
            with common.WorkspaceTransaction(workspace) as transaction:
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "no staged"):
                    transaction.commit({"review": ("done", {})})

            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(report)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "staged artifact"):
                    transaction.commit({"review": ("done", {})})

            target_dir = workspace / "report/becomes-dir"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(target_dir).write_text("x", encoding="utf-8")
                target_dir.mkdir()
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a file"):
                    transaction.commit({"review": ("done", {})})
            target_dir.rmdir()

            outside = workspace / "report/outside.json"
            atomic = workspace / "output/atomic"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(outside).write_text("x", encoding="utf-8")
                transaction.stage_directory(atomic, require_new=True)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "atomic transaction directory"):
                    transaction.commit({"review": ("done", {})})

            for extra, message in (
                ({"config_sha256": "bad"}, "config_sha256"),
                ({"decision_sha256": "bad"}, "decision_sha256"),
            ):
                with common.WorkspaceTransaction(workspace) as transaction:
                    transaction.stage_path(report).write_text("x", encoding="utf-8")
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, message):
                        transaction.commit({"review": ("done", extra)})
                self.assertFalse(report.exists())

            orphan = workspace / "versions/orphan.txt"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(orphan).write_text("x", encoding="utf-8")
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "no committed parent"):
                    transaction.commit({"review": ("done", {"output": "versions/orphan.txt"})})
            self.assertFalse(orphan.exists())

            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(report).write_text("ok", encoding="utf-8")
                transaction.commit({"review": ("done", {})})
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "already committed"):
                    transaction.commit({"review": ("done", {})})

    def test_deferred_workspace_rollback_finalize_and_marker_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            target = workspace / "report/deferred.json"
            transaction = common.WorkspaceTransaction(workspace)
            with transaction:
                transaction.stage_path(target).write_text("new", encoding="utf-8")
                transaction.commit(
                    {"review": ("done", {})},
                    defer_cleanup=True,
                    group_commits=((workspace, "review", "done"),),
                )
                transaction.rollback()
            self.assertFalse(target.exists())

            with common.WorkspaceTransaction(workspace) as uncommitted:
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "deferred committed"):
                    uncommitted.finalize()

            transaction = common.WorkspaceTransaction(workspace)
            with transaction:
                transaction.stage_path(target).write_text("new", encoding="utf-8")
                transaction.commit(
                    {"review": ("done", {})},
                    defer_cleanup=True,
                    group_commits=((workspace, "review", "done"),),
                )
                with mock.patch.object(common, "write_utf8", side_effect=OSError("marker")):
                    transaction.finalize()
                transaction.finalize()
            self.assertTrue(target.is_file())

    def test_external_delivery_api_covers_targets_directories_publish_rollback_and_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "delivery"
            root.mkdir()
            existing_dir = root / "existing"
            existing_dir.mkdir()
            existing_file = root / "file"
            existing_file.write_text("old", encoding="utf-8")

            with common.ExternalDeliveryTransaction(root) as delivery:
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a file"):
                    delivery.stage_path(existing_dir)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a directory"):
                    delivery.stage_directory(existing_file)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "already exists"):
                    delivery.stage_directory(existing_dir, require_new=True)
                atomic = root / "atomic"
                delivery.stage_directory(atomic, require_new=True)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "overlap"):
                    delivery.stage_directory(atomic / "child", require_new=True)

            with common.ExternalDeliveryTransaction(root) as delivery:
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "no staged"):
                    delivery.publish()

            target = root / "new/item.txt"
            with common.ExternalDeliveryTransaction(root) as delivery:
                delivery.stage_path(target)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "staged delivery"):
                    delivery.publish()

            with common.ExternalDeliveryTransaction(root) as delivery:
                delivery.stage_path(target).write_text("new", encoding="utf-8")
                delivery.publish()
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "already published"):
                    delivery.publish()
                delivery.rollback()
                delivery.rollback()
            self.assertFalse(target.exists())

            with common.ExternalDeliveryTransaction(root) as delivery:
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "must be published"):
                    delivery.finalize()

            with common.ExternalDeliveryTransaction(root) as delivery:
                delivery.stage_path(target).write_text("new", encoding="utf-8")
                delivery.publish()
                with mock.patch.object(common, "write_utf8", side_effect=OSError("marker")):
                    with self.assertRaises(OSError):
                        delivery.finalize()
                delivery.finalize()
                delivery.finalize()
            self.assertEqual(target.read_text(encoding="utf-8"), "new")


class CommonTransactionFailureCoverageF7Tests(unittest.TestCase):
    def test_transaction_enter_releases_locks_when_recovery_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            transaction = common.WorkspaceTransaction(workspace)
            with mock.patch.object(
                common,
                "recover_workspace_transactions",
                side_effect=OSError("recovery"),
            ):
                with self.assertRaisesRegex(OSError, "recovery"):
                    transaction.__enter__()
            self.assertFalse(transaction._lock._acquired)

            delivery = common.ExternalDeliveryTransaction(Path(directory) / "delivery")
            with mock.patch.object(
                common,
                "recover_external_delivery_transactions",
                side_effect=OSError("recovery"),
            ):
                with self.assertRaisesRegex(OSError, "recovery"):
                    delivery.__enter__()
            self.assertFalse(delivery._lock._acquired)

    def test_workspace_commit_rejects_mutated_directory_and_atomic_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            original_target = common._workspace_transaction_target

            target = workspace / "report/changed.json"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(target).write_text("x", encoding="utf-8")
                with mock.patch.object(
                    common,
                    "_workspace_transaction_target",
                    return_value=workspace / "report/other.json",
                ):
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, "target changed"):
                        transaction.commit({"review": ("done", {})})

            target = workspace / "report/missing-stage.json"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(target).write_text("x", encoding="utf-8")
                transaction._entries[target].staged = None
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "staged artifact is missing"):
                    transaction.commit({"review": ("done", {})})

            target = workspace / "report/directory-change.json"
            staged_directory = workspace / "output/new-directory"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(target).write_text("x", encoding="utf-8")
                transaction.stage_directory(staged_directory)

                def redirect_directory(workspace_value: Path, value: Path) -> Path:
                    resolved = original_target(workspace_value, value)
                    return workspace / "output/other" if resolved == staged_directory else resolved

                with mock.patch.object(
                    common,
                    "_workspace_transaction_target",
                    side_effect=redirect_directory,
                ):
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, "directory changed"):
                        transaction.commit({"review": ("done", {})})

            target = workspace / "report/directory-file.json"
            staged_directory = workspace / "output/becomes-file"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(target).write_text("x", encoding="utf-8")
                transaction.stage_directory(staged_directory)
                staged_directory.parent.mkdir(parents=True, exist_ok=True)
                staged_directory.write_text("file", encoding="utf-8")
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "directory target"):
                    transaction.commit({"review": ("done", {})})
            staged_directory.unlink()

            atomic = workspace / "output/existing-atomic"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_directory(atomic, require_new=True)
                transaction.stage_path(atomic / "a.txt").write_text("x", encoding="utf-8")
                atomic.mkdir(parents=True)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "already exists"):
                    transaction.commit({"review": ("done", {})})
            atomic.rmdir()

            atomic = workspace / "output/redirected-atomic"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_directory(atomic, require_new=True)
                transaction.stage_path(atomic / "a.txt").write_text("x", encoding="utf-8")
                original_resolve = common._resolve_path

                def redirect_atomic(value: Path, label: str) -> Path:
                    resolved = original_resolve(value, label)
                    if label == "atomic staged transaction directory":
                        return resolved.parent / "other"
                    return resolved

                with mock.patch.object(common, "_resolve_path", side_effect=redirect_atomic):
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, "staged.*invalid"):
                        transaction.commit({"review": ("done", {})})

    def test_workspace_lineage_and_rollback_failure_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            with common.WorkspaceTransaction(workspace) as transaction:
                with mock.patch.object(common, "load_manifest", return_value={"artifacts": []}):
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, "ledger"):
                        transaction._attach_lineage_metadata({"review": ("done", {})})

            decisions = workspace / "decisions/manual.json"
            decisions.write_text("{}", encoding="utf-8")
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(workspace / "report/unpublished.json").write_text(
                    "x", encoding="utf-8"
                )
                committed = transaction._attach_lineage_metadata(
                    {"review": ("done", {"decisions": "decisions/manual.json"})}
                )
                self.assertEqual(committed["review"][1]["run_id"], transaction.run_id)

            target = workspace / "report/rollback-created.json"
            created = workspace / "output/created-for-rollback"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(target).write_text("x", encoding="utf-8")
                transaction.stage_directory(created)
                with mock.patch.object(
                    transaction,
                    "_attach_lineage_metadata",
                    side_effect=ValueError("lineage"),
                ):
                    with self.assertRaisesRegex(ValueError, "lineage"):
                        transaction.commit({"review": ("done", {})})
            self.assertFalse(created.exists())

            target = workspace / "report/rollback-fails.json"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(target).write_text("x", encoding="utf-8")
                with (
                    mock.patch.object(transaction, "_write_journal", side_effect=OSError("journal")),
                    mock.patch.object(transaction, "_rollback", side_effect=OSError("rollback")),
                ):
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, "rollback could not"):
                        transaction.commit({"review": ("done", {})})
            shutil.rmtree(workspace / ".runs", ignore_errors=True)

            target = workspace / "report/deferred-rollback.json"
            with common.WorkspaceTransaction(workspace) as transaction:
                transaction.stage_path(target).write_text("x", encoding="utf-8")
                transaction.commit({"review": ("done", {})}, defer_cleanup=True)
                with mock.patch.object(transaction, "_rollback", side_effect=OSError("rollback")):
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, "deferred.*rollback"):
                        transaction.rollback()
                transaction.rollback()

    def test_private_rollback_paths_cover_atomic_and_missing_backup_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            with common.WorkspaceTransaction(workspace) as transaction:
                atomic = workspace / "output/atomic-rollback"
                target = atomic / "a.txt"
                atomic.mkdir(parents=True)
                target.write_text("published", encoding="utf-8")
                transaction._atomic_directories.add(atomic)
                transaction._rollback([common._TransactionEntry(target=target, published=True)])
                self.assertFalse(atomic.exists())

                missing = common._TransactionEntry(
                    target=workspace / "report/missing-backup.json",
                    backed_up=True,
                )
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "backup is missing"):
                    transaction._rollback([missing])

            delivery_root = Path(directory) / "delivery"
            with common.ExternalDeliveryTransaction(delivery_root) as delivery:
                missing = common._DeliveryEntry(
                    target=delivery_root / "missing.txt",
                    staged=delivery.files_root / "missing.txt",
                    backed_up=True,
                )
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "backup is missing"):
                    delivery._rollback([missing])

    def test_external_publish_rejects_mutated_directory_and_atomic_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original_target = common._external_transaction_target

            root = base / "changed"
            with common.ExternalDeliveryTransaction(root) as delivery:
                delivery.stage_path(root / "a.txt").write_text("x", encoding="utf-8")
                with mock.patch.object(
                    common,
                    "_external_transaction_target",
                    return_value=root / "other.txt",
                ):
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, "target changed"):
                        delivery.publish()

            root = base / "target-directory"
            with common.ExternalDeliveryTransaction(root) as delivery:
                target = root / "a.txt"
                delivery.stage_path(target).write_text("x", encoding="utf-8")
                target.mkdir(parents=True)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "target is not a file"):
                    delivery.publish()
            shutil.rmtree(root, ignore_errors=True)

            root = base / "directory-changed"
            with common.ExternalDeliveryTransaction(root) as delivery:
                target = root / "a.txt"
                staged_directory = root / "new-directory"
                delivery.stage_path(target).write_text("x", encoding="utf-8")
                delivery.stage_directory(staged_directory)

                def redirect_directory(root_value: Path, value: Path, protected) -> Path:
                    resolved = original_target(root_value, value, protected)
                    return root / "other" if resolved == staged_directory else resolved

                with mock.patch.object(
                    common,
                    "_external_transaction_target",
                    side_effect=redirect_directory,
                ):
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, "directory changed"):
                        delivery.publish()

            root = base / "directory-file"
            with common.ExternalDeliveryTransaction(root) as delivery:
                delivery.stage_path(root / "a.txt").write_text("x", encoding="utf-8")
                staged_directory = root / "new-directory"
                delivery.stage_directory(staged_directory)
                staged_directory.write_text("file", encoding="utf-8")
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "directory target"):
                    delivery.publish()
            shutil.rmtree(root, ignore_errors=True)

            root = base / "atomic-empty"
            with common.ExternalDeliveryTransaction(root) as delivery:
                delivery.stage_path(root / "a.txt").write_text("x", encoding="utf-8")
                delivery.stage_directory(root / "atomic", require_new=True)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "no staged artifacts"):
                    delivery.publish()

            root = base / "atomic-exists"
            with common.ExternalDeliveryTransaction(root) as delivery:
                atomic = root / "atomic"
                delivery.stage_directory(atomic, require_new=True)
                delivery.stage_path(atomic / "a.txt").write_text("x", encoding="utf-8")
                atomic.mkdir(parents=True)
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "already exists"):
                    delivery.publish()
            shutil.rmtree(root, ignore_errors=True)

            root = base / "atomic-redirected"
            with common.ExternalDeliveryTransaction(root) as delivery:
                atomic = root / "atomic"
                delivery.stage_directory(atomic, require_new=True)
                delivery.stage_path(atomic / "a.txt").write_text("x", encoding="utf-8")
                original_resolve = common._resolve_path

                def redirect_atomic(value: Path, label: str) -> Path:
                    resolved = original_resolve(value, label)
                    if label == "atomic staged delivery directory":
                        return resolved.parent / "other"
                    return resolved

                with mock.patch.object(common, "_resolve_path", side_effect=redirect_atomic):
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, "staged.*invalid"):
                        delivery.publish()

            root = base / "rollback-fails"
            with common.ExternalDeliveryTransaction(root) as delivery:
                delivery.stage_path(root / "a.txt").write_text("x", encoding="utf-8")
                with (
                    mock.patch.object(delivery, "_write_journal", side_effect=OSError("journal")),
                    mock.patch.object(delivery, "_rollback", side_effect=OSError("rollback")),
                ):
                    with self.assertRaisesRegex(common.WorkspaceTransactionError, "rollback could not"):
                        delivery.publish()
            shutil.rmtree(root, ignore_errors=True)


class CommonRecoveryCoverageF7Tests(unittest.TestCase):
    def workspace_entry(
        self,
        target: str,
        *,
        existed: bool = False,
        delete: bool = False,
        backup: bytes = b"old",
        staged: bytes = b"new",
    ) -> dict[str, object]:
        return {
            "target": target,
            "delete": delete,
            "existed": existed,
            "backup_sha256": (
                hashlib.sha256(backup).hexdigest() if existed else None
            ),
            "backup_size_bytes": len(backup) if existed else None,
            "staged_sha256": (
                None if delete else hashlib.sha256(staged).hexdigest()
            ),
            "staged_size_bytes": None if delete else len(staged),
        }

    def workspace_run(
        self,
        workspace: Path,
        journal: object | None,
        *,
        run_id: str = "a" * 32,
        marker: str | None = None,
    ) -> Path:
        root = workspace / ".runs" / run_id
        root.mkdir(parents=True)
        if marker is not None:
            (root / "run.marker").write_text(marker, encoding="utf-8")
        if journal is not None:
            (root / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
        return root

    def base_workspace_journal(self, run_id: str = "a" * 32) -> dict[str, object]:
        return {
            "schema_version": 2,
            "run_id": run_id,
            "deferred": False,
            "updates": [{"stage": "review", "status": "done"}],
            "entries": [self.workspace_entry("report/recovered.txt")],
            "directories": [],
            "atomic_directories": [],
            "group_commits": [],
        }

    def test_workspace_recovery_cleans_unjournaled_committed_and_valid_unpublished_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            root = self.workspace_run(workspace, None, marker="a" * 32)
            common.recover_workspace_transactions(workspace)
            self.assertFalse(root.exists())

            journal = self.base_workspace_journal()
            root = self.workspace_run(workspace, journal, marker="a" * 32)
            staged = root / "files/report/recovered.txt"
            staged.parent.mkdir(parents=True)
            staged.write_text("new", encoding="utf-8")
            common.recover_workspace_transactions(workspace)
            self.assertFalse(root.exists())

            root = self.workspace_run(workspace, journal, marker="a" * 32)
            (root / "commit.marker").write_text("a" * 32, encoding="utf-8")
            common.recover_workspace_transactions(workspace)
            self.assertFalse(root.exists())

    def test_workspace_recovery_rejects_run_marker_journal_entry_and_directory_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            runs = workspace / ".runs"
            manifest_bytes = (workspace / "manifest.json").read_bytes()

            def reject(
                label: str,
                mutate,
                message: str,
                setup=None,
            ) -> None:
                shutil.rmtree(runs, ignore_errors=True)
                (workspace / "manifest.json").write_bytes(manifest_bytes)
                target = workspace / "report/recovered.txt"
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                journal = self.base_workspace_journal()
                mutate(journal)
                root = self.workspace_run(workspace, journal, marker="a" * 32)
                staged = root / "files/report/recovered.txt"
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_text("new", encoding="utf-8")
                if setup is not None:
                    setup(root, target, journal)
                    (root / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
                with self.subTest(label=label), self.assertRaisesRegex(
                    common.WorkspaceTransactionError,
                    message,
                ):
                    common.recover_workspace_transactions(workspace, _lock_held=True)

            reject("journal object", lambda j: j.clear(), "identity")
            reject("schema", lambda j: j.update(schema_version=1), "identity")
            reject("run id", lambda j: j.update(run_id="b" * 32), "identity")
            reject("entries type", lambda j: j.update(entries={}), "no artifact entries")
            reject("entries empty", lambda j: j.update(entries=[]), "no artifact entries")
            reject("entry object", lambda j: j.update(entries=["bad"]), "invalid artifact entry")
            reject(
                "entry target",
                lambda j: j.update(entries=[{"target": 1}]),
                "invalid artifact entry",
            )
            reject(
                "entry existed",
                lambda j: j.update(entries=[{"target": "report/a", "existed": 1}]),
                "invalid artifact entry",
            )
            reject(
                "entry delete",
                lambda j: j.update(entries=[{"target": "report/a", "delete": 1}]),
                "invalid artifact entry",
            )
            reject(
                "entry traversal",
                lambda j: j.update(entries=[self.workspace_entry("../a")]),
                "cannot recover interrupted transaction",
            )
            reject(
                "duplicate",
                lambda j: j.update(
                    entries=[
                        self.workspace_entry("report/recovered.txt"),
                        self.workspace_entry("report/recovered.txt"),
                    ]
                ),
                "duplicate targets",
            )
            reject(
                "target directory",
                lambda _j: None,
                "target is not a file",
                setup=lambda _r, target, _j: target.mkdir(),
            )
            reject(
                "backup directory",
                lambda _j: None,
                "state is not a file",
                setup=lambda root, _t, _j: (root / "backups/report/recovered.txt").mkdir(parents=True),
            )
            reject(
                "staged directory",
                lambda _j: None,
                "state is not a file",
                setup=lambda root, _t, _j: (
                    (root / "files/report/recovered.txt").unlink(),
                    (root / "files/report/recovered.txt").mkdir(),
                ),
            )
            reject(
                "missing backup",
                lambda j: j.update(
                    entries=[
                        self.workspace_entry(
                            "report/recovered.txt",
                            existed=True,
                        )
                    ]
                ),
                "backup is missing",
                setup=lambda root, target, _j: (
                    (root / "files/report/recovered.txt").unlink(),
                    target.write_text("published", encoding="utf-8"),
                ),
            )
            reject(
                "missing identity fields",
                lambda j: j.update(entries=[{"target": "report/recovered.txt"}]),
                "invalid artifact entry",
                setup=lambda root, target, _j: (
                    (root / "files/report/recovered.txt").unlink(),
                    target.write_text("published", encoding="utf-8"),
                ),
            )
            reject("directories type", lambda j: j.update(directories={}), "invalid directories")
            reject(
                "directory item",
                lambda j: j.update(directories=[1]),
                "invalid directories",
            )
            reject(
                "directory conflict",
                lambda j: j.update(directories=["report/recovered.txt"]),
                "conflicts with an artifact",
            )
            reject(
                "directory file",
                lambda j: j.update(directories=["report/new-dir"]),
                "not a directory",
                setup=lambda _r, _t, _j: (workspace / "report/new-dir").write_text(
                    "file", encoding="utf-8"
                ),
            )
            reject(
                "atomic type",
                lambda j: j.update(atomic_directories={}),
                "invalid atomic directories",
            )
            reject(
                "atomic unregistered",
                lambda j: j.update(atomic_directories=["output/book"]),
                "atomic directory is invalid",
            )
            reject(
                "atomic no entries",
                lambda j: j.update(
                    directories=[{"path": "output/book", "created": True}],
                    atomic_directories=["output/book"],
                ),
                "invalid artifacts",
            )
            reject(
                "atomic delete",
                lambda j: j.update(
                    entries=[
                        self.workspace_entry(
                            "output/book/a.txt",
                            delete=True,
                        )
                    ],
                    directories=[{"path": "output/book", "created": True}],
                    atomic_directories=["output/book"],
                ),
                "invalid artifacts",
            )
            reject(
                "deferred backup",
                lambda j: j.update(deferred=True),
                "manifest backup is invalid",
            )
            reject(
                "deferred checksum",
                lambda j: j.update(deferred=True, manifest_backup_sha256="0" * 64),
                "checksum is invalid",
                setup=lambda root, _t, _j: (root / "manifest.backup.json").write_text(
                    "{}", encoding="utf-8"
                ),
            )

            shutil.rmtree(runs, ignore_errors=True)

    def test_workspace_recovery_restores_backup_removes_new_target_directory_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            manifest_before = (workspace / "manifest.json").read_bytes()
            journal = self.base_workspace_journal()
            target = workspace / "report/recovered.txt"
            target.write_text("new", encoding="utf-8")
            journal["entries"] = [
                self.workspace_entry(
                    "report/recovered.txt",
                    existed=True,
                    backup=b"old",
                    staged=b"new",
                )
            ]
            root = self.workspace_run(workspace, journal, marker="a" * 32)
            backup = root / "backups/report/recovered.txt"
            backup.parent.mkdir(parents=True)
            backup.write_text("old", encoding="utf-8")
            common.recover_workspace_transactions(workspace, _lock_held=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")

            journal = self.base_workspace_journal()
            target.write_text("published", encoding="utf-8")
            journal["entries"] = [
                self.workspace_entry(
                    "report/recovered.txt",
                    staged=b"published",
                )
            ]
            root = self.workspace_run(workspace, journal, marker="a" * 32)
            common.recover_workspace_transactions(workspace, _lock_held=True)
            self.assertFalse(target.exists())

            journal = self.base_workspace_journal()
            journal["entries"] = [
                self.workspace_entry("output/book/a.txt", staged=b"new")
            ]
            journal["directories"] = [{"path": "output/book", "created": True}]
            journal["atomic_directories"] = ["output/book"]
            root = self.workspace_run(workspace, journal, marker="a" * 32)
            published = workspace / "output/book"
            published.mkdir(parents=True)
            (published / "a.txt").write_text("new", encoding="utf-8")
            common.recover_workspace_transactions(workspace, _lock_held=True)
            self.assertFalse(published.exists())

            journal = self.base_workspace_journal()
            journal.update(
                deferred=True,
                group_commits=[
                    {"workspace": str(workspace), "stage": "review", "status": "done"}
                ],
            )
            root = self.workspace_run(workspace, journal, marker="a" * 32)
            manifest_backup = root / "manifest.backup.json"
            manifest_backup.write_bytes(manifest_before)
            journal["manifest_backup_sha256"] = common.sha256_file(manifest_backup)
            (root / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
            (workspace / "manifest.json").write_text("{}", encoding="utf-8")
            common.recover_workspace_transactions(workspace, _lock_held=True)
            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)

    def test_workspace_recovery_rejects_corrupt_containers_markers_and_backup_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            runs = workspace / ".runs"

            runs.write_text("file", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a directory"):
                common.recover_workspace_transactions(workspace, _lock_held=True)
            runs.unlink()

            runs.mkdir()
            (runs / "not-a-directory").write_text("file", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "non-directory entry"):
                common.recover_workspace_transactions(workspace, _lock_held=True)
            shutil.rmtree(runs)

            self.workspace_run(workspace, None, marker="wrong")
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "run marker"):
                common.recover_workspace_transactions(workspace, _lock_held=True)
            shutil.rmtree(runs)

            root = self.workspace_run(workspace, None, marker="a" * 32)
            (root / "journal.json").mkdir()
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "journal is redirected"):
                common.recover_workspace_transactions(workspace, _lock_held=True)
            shutil.rmtree(runs)

            self.workspace_run(workspace, [1], marker="a" * 32)
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "JSON object"):
                common.recover_workspace_transactions(workspace, _lock_held=True)
            shutil.rmtree(runs)

            root = self.workspace_run(
                workspace,
                self.base_workspace_journal(),
                marker="a" * 32,
            )
            (root / "commit.marker").write_text("wrong", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "commit marker"):
                common.recover_workspace_transactions(workspace, _lock_held=True)
            shutil.rmtree(runs)

            journal = self.base_workspace_journal()
            journal["deferred"] = True
            root = self.workspace_run(workspace, journal, marker="a" * 32)
            staged = root / "files/report/recovered.txt"
            staged.parent.mkdir(parents=True)
            staged.write_text("new", encoding="utf-8")
            backup = root / "manifest.backup.json"
            backup.write_text("[]", encoding="utf-8")
            journal["manifest_backup_sha256"] = common.sha256_file(backup)
            (root / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "manifest backup is invalid"):
                common.recover_workspace_transactions(workspace, _lock_held=True)
            shutil.rmtree(runs)

    def external_run(
        self,
        delivery_root: Path,
        journal: object | None,
        *,
        run_id: str = "b" * 32,
        marker: str | None = None,
    ) -> Path:
        run_root = delivery_root / ".delivery-runs" / run_id
        run_root.mkdir(parents=True)
        if marker is not None:
            (run_root / "run.marker").write_text(marker, encoding="utf-8")
        if journal is not None:
            (run_root / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
        return run_root

    def base_external_journal(self, root: Path, run_id: str = "b" * 32) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "root": str(root),
            "workspaces": [],
            "inputs": [],
            "entries": [{"target": "books/a.txt", "existed": False}],
            "directories": [],
            "atomic_directories": [],
            "commits": [],
        }

    def test_external_recovery_cleans_valid_runs_and_rejects_journal_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            delivery = Path(directory) / "delivery"
            delivery.mkdir()
            runs = delivery / ".delivery-runs"

            root = self.external_run(delivery, None, marker="b" * 32)
            common.recover_external_delivery_transactions(delivery)
            self.assertFalse(root.exists())

            journal = self.base_external_journal(delivery)
            root = self.external_run(delivery, journal, marker="b" * 32)
            staged = root / "files/books/a.txt"
            staged.parent.mkdir(parents=True)
            staged.write_text("new", encoding="utf-8")
            common.recover_external_delivery_transactions(delivery)
            self.assertFalse(root.exists())

            def reject(label: str, mutate, message: str, setup=None) -> None:
                shutil.rmtree(runs, ignore_errors=True)
                target = delivery / "books/a.txt"
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                journal = self.base_external_journal(delivery)
                mutate(journal)
                root = self.external_run(delivery, journal, marker="b" * 32)
                staged = root / "files/books/a.txt"
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_text("new", encoding="utf-8")
                if setup is not None:
                    setup(root, target, journal)
                    (root / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
                with self.subTest(label=label), self.assertRaisesRegex(
                    common.WorkspaceTransactionError,
                    message,
                ):
                    common.recover_external_delivery_transactions(delivery, _lock_held=True)

            reject("journal identity", lambda j: j.update(schema_version=2), "identity")
            reject("run identity", lambda j: j.update(run_id="c" * 32), "identity")
            reject("root identity", lambda j: j.update(root="C:/wrong"), "identity")
            reject("workspace protections", lambda j: j.update(workspaces={}), "protections")
            reject("input protections", lambda j: j.update(inputs={}), "protections")
            reject("entries type", lambda j: j.update(entries={}), "no artifact entries")
            reject("entries empty", lambda j: j.update(entries=[]), "no artifact entries")
            reject("entry object", lambda j: j.update(entries=["bad"]), "invalid entry")
            reject(
                "entry target",
                lambda j: j.update(entries=[{"target": 1}]),
                "invalid entry",
            )
            reject(
                "entry existed",
                lambda j: j.update(entries=[{"target": "books/a.txt", "existed": 1}]),
                "invalid entry",
            )
            reject(
                "duplicate target",
                lambda j: j.update(
                    entries=[
                        {"target": "books/a.txt", "existed": False},
                        {"target": "books/a.txt", "existed": False},
                    ]
                ),
                "duplicate targets",
            )
            reject(
                "target directory",
                lambda _j: None,
                "target is not a file",
                setup=lambda _r, target, _j: target.mkdir(parents=True),
            )
            reject(
                "backup directory",
                lambda _j: None,
                "state is not a file",
                setup=lambda root, _t, _j: (root / "backups/books/a.txt").mkdir(parents=True),
            )
            reject(
                "missing backup",
                lambda j: j.update(entries=[{"target": "books/a.txt", "existed": True}]),
                "backup is missing",
                setup=lambda root, target, _j: (
                    (root / "files/books/a.txt").unlink(),
                    target.parent.mkdir(parents=True, exist_ok=True),
                    target.write_text("published", encoding="utf-8"),
                ),
            )
            reject("directories type", lambda j: j.update(directories={}), "invalid directories")
            reject("directories item", lambda j: j.update(directories=[1]), "invalid directories")
            reject("atomic type", lambda j: j.update(atomic_directories={}), "invalid atomic")
            reject(
                "directory conflict",
                lambda j: j.update(directories=["books/a.txt"]),
                "conflicts with an artifact",
            )
            reject(
                "atomic unregistered",
                lambda j: j.update(atomic_directories=["books"]),
                "atomic directory is invalid",
            )
            reject(
                "atomic no artifacts",
                lambda j: j.update(
                    entries=[{"target": "other/a.txt", "existed": False}],
                    directories=["books"],
                    atomic_directories=["books"],
                ),
                "has no artifacts",
            )

            shutil.rmtree(runs, ignore_errors=True)

    def test_external_recovery_restores_backup_removes_new_and_atomic_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            delivery = Path(directory) / "delivery"
            delivery.mkdir()
            target = delivery / "books/a.txt"
            target.parent.mkdir()
            target.write_text("new", encoding="utf-8")
            journal = self.base_external_journal(delivery)
            journal["entries"] = [{"target": "books/a.txt", "existed": True}]
            root = self.external_run(delivery, journal, marker="b" * 32)
            backup = root / "backups/books/a.txt"
            backup.parent.mkdir(parents=True)
            backup.write_text("old", encoding="utf-8")
            common.recover_external_delivery_transactions(delivery, _lock_held=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")

            target.write_text("published", encoding="utf-8")
            journal = self.base_external_journal(delivery)
            root = self.external_run(delivery, journal, marker="b" * 32)
            common.recover_external_delivery_transactions(delivery, _lock_held=True)
            self.assertFalse(target.exists())

            journal = self.base_external_journal(delivery)
            journal["directories"] = ["books"]
            journal["atomic_directories"] = ["books"]
            root = self.external_run(delivery, journal, marker="b" * 32)
            target.parent.mkdir(exist_ok=True)
            target.write_text("published", encoding="utf-8")
            common.recover_external_delivery_transactions(delivery, _lock_held=True)
            self.assertFalse(target.parent.exists())

    def test_external_recovery_rejects_corrupt_containers_markers_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            delivery = Path(directory) / "delivery"
            delivery.mkdir()
            runs = delivery / ".delivery-runs"

            runs.write_text("file", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a directory"):
                common.recover_external_delivery_transactions(delivery, _lock_held=True)
            runs.unlink()

            root = self.external_run(delivery, None, marker="b" * 32)
            (root / "journal.json").mkdir()
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "journal is redirected"):
                common.recover_external_delivery_transactions(delivery, _lock_held=True)
            shutil.rmtree(runs)

            self.external_run(delivery, [1], marker="b" * 32)
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "JSON object"):
                common.recover_external_delivery_transactions(delivery, _lock_held=True)
            shutil.rmtree(runs)

            root = self.external_run(
                delivery,
                self.base_external_journal(delivery),
                marker="b" * 32,
            )
            (root / "commit.marker").write_text("wrong", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "commit marker"):
                common.recover_external_delivery_transactions(delivery, _lock_held=True)
            shutil.rmtree(runs)

            journal = self.base_external_journal(delivery)
            journal["directories"] = ["books/not-a-directory"]
            root = self.external_run(delivery, journal, marker="b" * 32)
            staged = root / "files/books/a.txt"
            staged.parent.mkdir(parents=True)
            staged.write_text("new", encoding="utf-8")
            not_directory = delivery / "books/not-a-directory"
            not_directory.parent.mkdir()
            not_directory.write_text("file", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceTransactionError, "not a directory"):
                common.recover_external_delivery_transactions(delivery, _lock_held=True)
            shutil.rmtree(runs)

    def test_commit_state_helpers_cover_invalid_missing_mismatch_and_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, workspace = make_workspace(Path(directory))
            run_id = "d" * 32
            manifest = common.load_manifest(workspace)
            manifest["stages"]["review"] = {"run_id": run_id, "status": "done"}
            common.write_json(workspace / "manifest.json", manifest)
            valid_update = {
                "run_id": run_id,
                "updates": [{"stage": "review", "status": "done"}],
            }
            self.assertTrue(common._transaction_manifest_is_committed(workspace, valid_update))
            for value in ({}, {"run_id": run_id, "updates": []}, {**valid_update, "updates": ["bad"]}):
                self.assertFalse(common._transaction_manifest_is_committed(workspace, value))
            wrong = copy.deepcopy(valid_update)
            wrong["updates"][0]["status"] = "failed"
            self.assertFalse(common._transaction_manifest_is_committed(workspace, wrong))

            group = {
                "run_id": run_id,
                "group_commits": [
                    {"workspace": str(workspace), "stage": "review", "status": "done"}
                ],
            }
            self.assertTrue(common._transaction_group_is_committed(group))
            self.assertTrue(
                common._delivery_manifest_committed(
                    {"run_id": run_id, "commits": group["group_commits"]}
                )
            )
            for function, key in (
                (common._transaction_group_is_committed, "group_commits"),
                (common._delivery_manifest_committed, "commits"),
            ):
                self.assertFalse(function({}))
                self.assertFalse(function({"run_id": run_id, key: ["bad"]}))
                bad = copy.deepcopy(group["group_commits"])
                bad[0]["workspace"] = ""
                self.assertFalse(function({"run_id": run_id, key: bad}))
                mismatch = copy.deepcopy(group["group_commits"])
                mismatch[0]["status"] = "failed"
                self.assertFalse(function({"run_id": run_id, key: mismatch}))

    def test_low_level_path_and_recovery_failures_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("正文", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                common.source_identity_id("invalid", source)

            workspace = root / "workspace"
            workspace.mkdir()
            with mock.patch.object(common, "validate_workspace", side_effect=ValueError("bad")):
                self.assertFalse(common._existing_workspace_matches_source(source, workspace))

            left = root / "left.txt"
            right = root / "right.txt"
            left.write_text("left", encoding="utf-8")
            right.write_text("right", encoding="utf-8")
            with mock.patch.object(common.os.path, "samefile", side_effect=OSError("unsupported")):
                self.assertFalse(common._same_file_or_path(left, right))
            with mock.patch.object(common.os, "name", "posix"):
                self.assertEqual(common._without_windows_extended_prefix("plain"), "plain")
            with mock.patch.object(common.os, "name", "nt"):
                self.assertEqual(
                    common._without_windows_extended_prefix(r"\\?\UNC\server\share"),
                    r"\\server\share",
                )
            with mock.patch.object(Path, "resolve", side_effect=OSError("denied")):
                with self.assertRaisesRegex(common.WorkspacePathError, "cannot resolve"):
                    common._resolve_path(root, "test path")

            with mock.patch.object(common.os, "scandir", side_effect=OSError("denied")):
                with self.assertRaisesRegex(common.WorkspacePathError, "cannot inspect workspace directory"):
                    common._validate_workspace_tree(workspace)

            entry_path = workspace / "unreadable.txt"
            entry_path.write_text("x", encoding="utf-8")

            class UnreadableEntry:
                def __init__(self, path: Path) -> None:
                    self.path = str(path)

                @staticmethod
                def stat(*_args, **_kwargs):
                    raise OSError("denied")

            with mock.patch.object(common.os, "scandir", return_value=[UnreadableEntry(entry_path)]):
                with self.assertRaisesRegex(common.WorkspacePathError, "cannot inspect workspace path"):
                    common._validate_workspace_tree(workspace)

            transaction_root = root / ".runs" / ("a" * 32)
            transaction_root.mkdir(parents=True)
            with mock.patch.object(Path, "rmdir", side_effect=OSError("not empty")):
                common._cleanup_transaction_root(transaction_root)

            run_id = "b" * 32
            updates = {"run_id": run_id, "updates": [{"stage": "review", "status": "done"}]}
            with mock.patch.object(common, "load_manifest", side_effect=OSError("missing")):
                self.assertFalse(common._transaction_manifest_is_committed(workspace, updates))
            with mock.patch.object(common, "load_manifest", return_value=[]):
                self.assertFalse(common._transaction_manifest_is_committed(workspace, updates))

            commits = [{"workspace": str(workspace), "stage": "review", "status": "done"}]
            group = {"run_id": run_id, "group_commits": commits}
            delivery = {"run_id": run_id, "commits": commits}
            for function, journal in (
                (common._transaction_group_is_committed, group),
                (common._delivery_manifest_committed, delivery),
            ):
                with mock.patch.object(common, "load_manifest", side_effect=OSError("missing")):
                    self.assertFalse(function(journal))
                with mock.patch.object(common, "load_manifest", return_value=[]):
                    self.assertFalse(function(journal))

            delivery_root = root / "delivery" / ".delivery-runs" / ("c" * 32)
            delivery_root.mkdir(parents=True)
            with mock.patch.object(Path, "rmdir", side_effect=OSError("not empty")):
                common._cleanup_delivery_run(delivery_root)
            for manifest in ({"stages": []}, {"stages": {"review": []}}):
                with mock.patch.object(common, "load_manifest", return_value=manifest):
                    self.assertFalse(common._delivery_manifest_committed(delivery))

            snapshot_workspace = root / "snapshot"
            snapshot_workspace.mkdir()
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "marker is invalid"):
                common._validate_snapshot_init_marker(snapshot_workspace)
            marker = snapshot_workspace / common._SNAPSHOT_INIT_MARKER
            marker.touch()
            with mock.patch.object(common.os, "open", side_effect=FileExistsError):
                self.assertEqual(common._create_snapshot_init_marker(snapshot_workspace), marker)
            invalid_temp = snapshot_workspace / ".manifest.json.abcdefgh.tmp"
            invalid_temp.mkdir()
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "temporary file is invalid"):
                common._remove_snapshot_atomic_temps(snapshot_workspace)

            with mock.patch.object(common, "validate_workspace", return_value=snapshot_workspace):
                with self.assertRaisesRegex(common.WorkspacePathError, "aliases a protected"):
                    common.save_manifest(
                        snapshot_workspace,
                        {"source": {"path": str(snapshot_workspace / "manifest.json")}},
                    )

            identity_workspace = root / "identity"
            identity_workspace.mkdir()
            (identity_workspace / "manifest.json").mkdir()
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "manifest is not a file"):
                common._validate_bound_snapshot_identity(identity_workspace)

            _, complete_workspace = make_workspace(root, "complete")
            with common.WorkspaceTransaction(complete_workspace) as transaction:
                transaction.discard_unwritten_stage(
                    complete_workspace / "report" / "never-created.json"
                )

            escaped_manifest = common.load_manifest(complete_workspace)
            original_resolve = common._resolve_path

            def redirect_artifact(path: Path, label: str) -> Path:
                if label == "manifest artifact":
                    return root
                return original_resolve(path, label)

            with mock.patch.object(common, "_resolve_path", side_effect=redirect_artifact):
                with self.assertRaisesRegex(common.WorkspaceIdentityError, "artifact escapes"):
                    common._validate_manifest_v2(complete_workspace, escaped_manifest)

    def test_snapshot_recovery_requires_a_durable_marker_and_valid_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("正文", encoding="utf-8")

            missing_marker = root / "missing-marker"
            missing_marker.mkdir()
            (missing_marker / common._SNAPSHOT_INIT_JOURNAL).write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "no durable marker"):
                common._load_snapshot_init_journal(source, missing_marker)

            unknown_entry = root / "unknown-entry"
            unknown_entry.mkdir()
            (unknown_entry / common._SNAPSHOT_INIT_MARKER).touch()
            (unknown_entry / "unrelated.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "unknown entry"):
                common._load_snapshot_init_journal(source, unknown_entry)

            invalid_journal = root / "invalid-journal"
            invalid_journal.mkdir()
            (invalid_journal / common._SNAPSHOT_INIT_MARKER).touch()
            (invalid_journal / common._SNAPSHOT_INIT_JOURNAL).write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(common.WorkspaceIdentityError, "journal cannot be read"):
                common._load_snapshot_init_journal(source, invalid_journal)


if __name__ == "__main__":
    unittest.main()
