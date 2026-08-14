from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_review_html  # noqa: E402
import common  # noqa: E402
import apply_decisions  # noqa: E402
import export_outputs  # noqa: E402
import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import scan_ads  # noqa: E402
import verify  # noqa: E402


class DeliveryTransactionsF1Tests(unittest.TestCase):
    def bind_workspace(self, root: Path, name: str) -> Path:
        source = root / f"{name}.txt"
        source.write_text("第一章 示例\n正文甲。\n", encoding="utf-8")
        workspace = root / f"{name}.txt.cleanwork"
        preprocess.run(source, str(workspace), "utf-8")
        parse_structure.run(workspace)
        scan_ads.run(
            workspace,
            "versions/v1_preprocessed.txt",
            "candidates/ads.jsonl",
            12,
            20,
            20,
        )
        common.write_json(workspace / "meta/book_profile.json", {})
        make_ad_decisions.run(
            workspace,
            "candidates/ads_pages",
            "decisions/ads_decisions.draft.jsonl",
            "meta/book_profile.json",
            True,
        )
        common.write_jsonl(workspace / "decisions/ads_agent_reviews.jsonl", [])
        finalize_ad_decisions.run(
            workspace,
            "candidates/ads_pages",
            "decisions/ads_agent_reviews.jsonl",
            "decisions/ads_decisions.draft.jsonl",
            "decisions/ads_decisions.jsonl",
        )
        apply_decisions.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "decisions/ads_decisions.jsonl",
            "versions/v2_ads_removed.txt",
            "2_ads",
        )
        verify.run(
            workspace,
            "ads",
            "versions/v1_preprocessed.txt",
            "versions/v2_ads_removed.txt",
            "decisions/ads_decisions.jsonl",
            False,
        )
        return workspace

    def remember_workspace(self, workspace: Path) -> dict[Path, bytes]:
        paths = [
            workspace / "manifest.json",
            workspace / "versions" / "v0_original.txt",
            workspace / "report" / "export_report.json",
        ]
        report = paths[-1]
        report.write_text('{"version":"old"}\n', encoding="utf-8")
        return {path: path.read_bytes() for path in paths}

    def assert_workspace_unchanged(self, before: dict[Path, bytes]) -> None:
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        for workspace in {path.parent for path in before if path.name == "manifest.json"}:
            runs = workspace / ".runs"
            self.assertFalse(runs.exists() and any(runs.iterdir()))

    def assert_no_delivery_runs(self, root: Path) -> None:
        runs = root / ".delivery-runs"
        self.assertFalse(runs.exists() and any(runs.iterdir()))

    def test_external_single_export_failure_rolls_back_and_retry_commits_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.bind_workspace(root, "sample-a")
            before = self.remember_workspace(workspace)
            output_root = root / "exports"

            with (
                mock.patch.object(export_outputs, "write_epub", side_effect=OSError("injected epub failure")),
                self.assertRaisesRegex(OSError, "injected epub failure"),
            ):
                export_outputs.run(
                    workspace,
                    "auto",
                    None,
                    output_root,
                    requested_formats=export_outputs.ALL_FORMATS,
                )

            self.assert_workspace_unchanged(before)
            self.assertFalse(output_root.exists() and any(output_root.rglob("*")))

            report = export_outputs.run(
                workspace,
                "auto",
                None,
                output_root,
                requested_formats=export_outputs.ALL_FORMATS,
            )
            manifest = export_outputs.load_manifest(workspace)
            stage = manifest["stages"]["7_export"]
            self.assertEqual(stage["status"], "done")
            self.assertTrue(stage.get("run_id"))
            self.assertEqual(set(report["outputs"]), {"txt", "markdown", "epub"})
            self.assertTrue(all(Path(path).is_file() for path in report["outputs"].values()))
            self.assert_no_delivery_runs(output_root)

    def test_require_new_delivery_directory_is_published_by_one_tree_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "delivery"
            output_root.mkdir()
            bundle = output_root / "book"
            real_replace = common.os.replace
            published_targets: list[Path] = []

            with common.ExternalDeliveryTransaction(output_root) as delivery:
                delivery.stage_directory(bundle, require_new=True)
                common.write_utf8(delivery.stage_path(bundle / "book.txt"), "text\n")
                common.write_utf8(delivery.stage_path(bundle / "book.md"), "markdown\n")

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
                    delivery.publish()
                delivery.finalize()

            self.assertIn(bundle, published_targets)
            self.assertNotIn(bundle / "book.txt", published_targets)
            self.assertNotIn(bundle / "book.md", published_targets)
            self.assertEqual((bundle / "book.txt").read_text(encoding="utf-8"), "text\n")
            self.assertEqual((bundle / "book.md").read_text(encoding="utf-8"), "markdown\n")

    def test_batch_export_failure_rolls_back_every_book_and_retry_uses_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspaces = [
                self.bind_workspace(root, "sample-a"),
                self.bind_workspace(root, "sample-b"),
            ]
            before = [self.remember_workspace(workspace) for workspace in workspaces]
            output_root = root / "exports"
            real_write_epub = export_outputs.write_epub
            calls = 0

            def fail_second_epub(*args: object, **kwargs: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second-book failure")
                real_write_epub(*args, **kwargs)

            with (
                mock.patch.object(export_outputs, "write_epub", side_effect=fail_second_epub),
                self.assertRaisesRegex(OSError, "injected second-book failure"),
            ):
                export_outputs.run_batch(
                    workspaces,
                    "auto",
                    None,
                    output_root,
                    requested_formats=export_outputs.ALL_FORMATS,
                )

            for snapshot in before:
                self.assert_workspace_unchanged(snapshot)
            self.assertFalse(output_root.exists() and any(output_root.rglob("*")))

            report = export_outputs.run_batch(
                workspaces,
                "auto",
                None,
                output_root,
                requested_formats=export_outputs.ALL_FORMATS,
            )
            run_ids = {
                export_outputs.load_manifest(workspace)["stages"]["7_export"].get("run_id")
                for workspace in workspaces
            }
            self.assertEqual(len(run_ids), 1)
            self.assertNotIn(None, run_ids)
            self.assertEqual(report["count"], 2)
            self.assertTrue(Path(report["output_dir"], "batch_export_report.json").is_file())
            self.assert_no_delivery_runs(output_root)

    def test_batch_manifest_failure_rolls_back_prior_book_and_external_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspaces = [
                self.bind_workspace(root, "sample-a"),
                self.bind_workspace(root, "sample-b"),
            ]
            before = [self.remember_workspace(workspace) for workspace in workspaces]
            output_root = root / "exports"
            real_save_manifest = common.save_manifest

            def fail_second_workspace(workspace: Path, manifest: dict[str, object]) -> None:
                if Path(workspace) == workspaces[1]:
                    raise OSError("injected second manifest failure")
                real_save_manifest(workspace, manifest)

            with (
                mock.patch.object(common, "save_manifest", side_effect=fail_second_workspace),
                self.assertRaisesRegex(OSError, "injected second manifest failure"),
            ):
                export_outputs.run_batch(workspaces, "auto", None, output_root)

            for snapshot in before:
                self.assert_workspace_unchanged(snapshot)
            self.assertFalse(output_root.exists() and any(output_root.rglob("*")))

    def test_single_review_failure_preserves_internal_and_external_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("internal", "external"):
                with self.subTest(mode=mode):
                    workspace = self.bind_workspace(root, f"sample-{mode}")
                    out_dir = workspace / "report" if mode == "internal" else root / f"review-{mode}"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    paths = [
                        out_dir / "review.html",
                        out_dir / "review_data.json",
                        out_dir / "rollback_guide.md",
                    ]
                    for index, path in enumerate(paths, 1):
                        path.write_text(f"old-{index}\n", encoding="utf-8")
                    before = {path: path.read_bytes() for path in paths}
                    manifest_before = (workspace / "manifest.json").read_bytes()
                    output_value = None if mode == "internal" else str(out_dir)

                    with (
                        mock.patch.object(
                            build_review_html,
                            "write_rollback_guide",
                            side_effect=OSError("injected guide failure"),
                        ),
                        self.assertRaisesRegex(OSError, "injected guide failure"),
                    ):
                        build_review_html.run([workspace], output_value, True, 80, 3)

                    self.assertEqual({path: path.read_bytes() for path in paths}, before)
                    self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)

                    result = build_review_html.run([workspace], output_value, True, 80, 3)
                    manifest = build_review_html.load_manifest(workspace)
                    self.assertTrue(all(Path(result[key]).is_file() for key in ("html", "data", "rollback_guide")))
                    self.assertTrue(manifest["stages"]["review"].get("run_id"))
                    self.assert_no_delivery_runs(out_dir)

    def test_batch_review_failure_preserves_bundle_and_retry_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspaces = [
                self.bind_workspace(root, "sample-a"),
                self.bind_workspace(root, "sample-b"),
            ]
            out_dir = root / "review"
            out_dir.mkdir()
            paths = [
                out_dir / "review_index.html",
                out_dir / "review_index.json",
                out_dir / "rollback_guide.md",
            ]
            for index, path in enumerate(paths, 1):
                path.write_text(f"old-{index}\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in paths}

            with (
                mock.patch.object(
                    build_review_html,
                    "write_rollback_guide",
                    side_effect=OSError("injected batch guide failure"),
                ),
                self.assertRaisesRegex(OSError, "injected batch guide failure"),
            ):
                build_review_html.run(workspaces, str(out_dir), True, 80, 3)

            self.assertEqual({path: path.read_bytes() for path in paths}, before)

            result = build_review_html.run(workspaces, str(out_dir), True, 80, 3)
            self.assertEqual(result["workspace_count"], 2)
            self.assertTrue(all(Path(result[key]).is_file() for key in ("html", "data", "rollback_guide")))
            self.assertEqual(len(result["books"]), 2)
            index = build_review_html.read_json(Path(result["data"]))
            self.assertNotIn("workspaces", index)
            self.assertEqual(index["books"], result["books"])
            index_text = Path(result["data"]).read_text(encoding="utf-8")
            index_html = Path(result["html"]).read_text(encoding="utf-8")
            for workspace in workspaces:
                self.assertNotIn(str(workspace), index_text)
                self.assertNotIn(str(workspace), index_html)
            for book in result["books"]:
                child = out_dir / book["html"]
                self.assertRegex(Path(book["html"]).name, r"^[0-9a-f]{64}\.html$")
                self.assertTrue(child.is_file())
                self.assertEqual(common.sha256_file(child), book["sha256"])
                other_names = {
                    item["name"] for item in result["books"] if item["id"] != book["id"]
                }
                child_text = child.read_text(encoding="utf-8")
                self.assertIn(book["name"], child_text)
                self.assertTrue(all(name not in child_text for name in other_names))
            run_ids = {
                common.load_manifest(workspace)["stages"]["review"].get("run_id")
                for workspace in workspaces
            }
            self.assertEqual(len(run_ids), 1)
            self.assertNotIn(None, run_ids)
            for workspace in workspaces:
                stage = common.load_manifest(workspace)["stages"]["review"]
                expected_id = build_review_html.batch_book_id(workspace)
                self.assertEqual(stage["book_id"], expected_id)
                self.assertEqual(stage["book_html"], f"books/{expected_id}.html")
                self.assertEqual(
                    stage["book_sha256"],
                    common.sha256_file(out_dir / stage["book_html"]),
                )
            self.assert_no_delivery_runs(out_dir)

    def test_batch_review_published_child_validation_failure_rolls_back_every_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspaces = [
                self.bind_workspace(root, "sample-a"),
                self.bind_workspace(root, "sample-b"),
            ]
            out_dir = root / "review"
            out_dir.mkdir()
            paths = [
                out_dir / "review_index.html",
                out_dir / "review_index.json",
                out_dir / "rollback_guide.md",
                *[
                    out_dir / "books" / f"{build_review_html.batch_book_id(workspace)}.html"
                    for workspace in workspaces
                ],
            ]
            for index, path in enumerate(paths, 1):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"old-{index}\n", encoding="utf-8")
            before_files = {path: path.read_bytes() for path in paths}
            before_manifests = {
                workspace: (workspace / "manifest.json").read_bytes()
                for workspace in workspaces
            }
            real_validate = build_review_html.validate_batch_book_pages
            validation_calls = 0

            def fail_published_validation(index_path: Path) -> None:
                nonlocal validation_calls
                validation_calls += 1
                real_validate(index_path)
                if validation_calls == 2:
                    raise RuntimeError("injected published child validation failure")

            with (
                mock.patch.object(
                    build_review_html,
                    "validate_batch_book_pages",
                    side_effect=fail_published_validation,
                ),
                self.assertRaisesRegex(RuntimeError, "injected published child validation failure"),
            ):
                build_review_html.run(workspaces, str(out_dir), True, 80, 3)

            self.assertEqual({path: path.read_bytes() for path in paths}, before_files)
            self.assertEqual(
                {
                    workspace: (workspace / "manifest.json").read_bytes()
                    for workspace in workspaces
                },
                before_manifests,
            )
            self.assert_no_delivery_runs(out_dir)

    def test_batch_review_manifest_failure_rolls_back_every_workspace_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspaces = [
                self.bind_workspace(root, "sample-a"),
                self.bind_workspace(root, "sample-b"),
            ]
            out_dir = root / "review"
            out_dir.mkdir()
            paths = [
                out_dir / "review_index.html",
                out_dir / "review_index.json",
                out_dir / "rollback_guide.md",
            ]
            for index, path in enumerate(paths, 1):
                path.write_text(f"old-{index}\n", encoding="utf-8")
            before_files = {path: path.read_bytes() for path in paths}
            before_manifests = {
                workspace: (workspace / "manifest.json").read_bytes()
                for workspace in workspaces
            }
            real_save_manifest = common.save_manifest

            def fail_second_workspace(workspace: Path, manifest: dict[str, object]) -> None:
                if Path(workspace) == workspaces[1]:
                    raise OSError("injected second review manifest failure")
                real_save_manifest(workspace, manifest)

            with (
                mock.patch.object(common, "save_manifest", side_effect=fail_second_workspace),
                self.assertRaisesRegex(OSError, "injected second review manifest failure"),
            ):
                build_review_html.run(workspaces, str(out_dir), True, 80, 3)

            self.assertEqual({path: path.read_bytes() for path in paths}, before_files)
            self.assertEqual(
                {
                    workspace: (workspace / "manifest.json").read_bytes()
                    for workspace in workspaces
                },
                before_manifests,
            )
            self.assert_no_delivery_runs(out_dir)
            for workspace in workspaces:
                self.assertFalse((workspace / ".runs").exists())

    def test_review_html_activation_failure_keeps_the_old_complete_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.bind_workspace(root, "sample-a")
            out_dir = root / "review"
            out_dir.mkdir()
            paths = [
                out_dir / "review.html",
                out_dir / "review_data.json",
                out_dir / "rollback_guide.md",
            ]
            for index, path in enumerate(paths, 1):
                path.write_text(f"old-{index}\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in paths}
            real_replace = common.os.replace
            failed = False

            def fail_html_activation(source: object, target: object) -> None:
                nonlocal failed
                if Path(target) == paths[0] and not failed:
                    failed = True
                    self.assertEqual(paths[0].read_bytes(), before[paths[0]])
                    raise OSError("injected HTML activation failure")
                real_replace(source, target)

            with (
                mock.patch.object(common.os, "replace", side_effect=fail_html_activation),
                self.assertRaisesRegex(OSError, "injected HTML activation failure"),
            ):
                build_review_html.run([workspace], str(out_dir), True, 80, 3)

            self.assertEqual({path: path.read_bytes() for path in paths}, before)
            self.assertNotIn("review", common.load_manifest(workspace)["stages"])
            self.assert_no_delivery_runs(out_dir)

    def test_external_delivery_recovery_distinguishes_uncommitted_and_committed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.bind_workspace(root, "sample-a")
            output_root = root / "delivery"
            output_root.mkdir()
            target = output_root / "review.html"
            target.write_text("old\n", encoding="utf-8")

            delivery = common.ExternalDeliveryTransaction(
                output_root,
                workspaces=(workspace,),
            )
            with (
                self.assertRaises(common.WorkspaceTransactionError),
                mock.patch.object(
                    delivery,
                    "_rollback",
                    side_effect=OSError("injected interrupted rollback"),
                ),
            ):
                with delivery:
                    common.write_utf8(delivery.stage_path(target), "uncommitted\n")
                    delivery.publish(commits=((workspace, "review", "done"),))
                    raise OSError("trigger rollback")

            self.assertEqual(target.read_text(encoding="utf-8"), "uncommitted\n")
            common.recover_external_delivery_transactions(output_root)
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
            self.assert_no_delivery_runs(output_root)

            real_rmtree = common.shutil.rmtree
            cleanup_failed = False

            def fail_first_cleanup(path: object, *args: object, **kwargs: object) -> None:
                nonlocal cleanup_failed
                if Path(path).parent == output_root / ".delivery-runs" and not cleanup_failed:
                    cleanup_failed = True
                    raise OSError("injected committed cleanup failure")
                real_rmtree(path, *args, **kwargs)

            with common.ExternalDeliveryTransaction(
                output_root,
                workspaces=(workspace,),
            ) as delivery:
                common.write_utf8(delivery.stage_path(target), "committed\n")
                delivery.publish(commits=((workspace, "review", "done"),))
                common.update_stage(
                    workspace,
                    "review",
                    "done",
                    run_id=delivery.run_id,
                    html=str(target),
                )
                with mock.patch.object(common.shutil, "rmtree", side_effect=fail_first_cleanup):
                    delivery.finalize()

            self.assertTrue((output_root / ".delivery-runs").exists())
            common.recover_external_delivery_transactions(output_root)
            self.assertEqual(target.read_text(encoding="utf-8"), "committed\n")
            self.assert_no_delivery_runs(output_root)

    def test_export_recovers_an_uncommitted_directory_before_choosing_its_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.bind_workspace(root, "sample-a")
            output_root = root / "exports"
            stale = output_root / "sample-a" / "stale.txt"
            delivery = common.ExternalDeliveryTransaction(
                output_root,
                workspaces=(workspace,),
            )

            with (
                self.assertRaises(common.WorkspaceTransactionError),
                mock.patch.object(
                    delivery,
                    "_rollback",
                    side_effect=OSError("injected interrupted rollback"),
                ),
            ):
                with delivery:
                    common.write_utf8(delivery.stage_path(stale), "uncommitted\n")
                    delivery.publish(commits=((workspace, "7_export", "done"),))
                    raise OSError("trigger rollback")

            self.assertTrue(stale.is_file())
            plan = export_outputs.prepare_export(workspace, "auto", None, output_root)
            self.assertEqual(plan["output_dir"], output_root / "sample-a")
            self.assertFalse(stale.exists())
            self.assert_no_delivery_runs(output_root)

    def test_external_recovery_retry_preserves_every_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.bind_workspace(root, "sample-a")
            output_root = root / "delivery"
            output_root.mkdir()
            first = output_root / "first.txt"
            second = output_root / "second.txt"
            first.write_text("old-first\n", encoding="utf-8")
            second.write_text("old-second\n", encoding="utf-8")
            delivery = common.ExternalDeliveryTransaction(
                output_root,
                workspaces=(workspace,),
            )

            with (
                self.assertRaises(common.WorkspaceTransactionError),
                mock.patch.object(
                    delivery,
                    "_rollback",
                    side_effect=OSError("injected interrupted rollback"),
                ),
            ):
                with delivery:
                    common.write_utf8(delivery.stage_path(first), "new-first\n")
                    common.write_utf8(delivery.stage_path(second), "new-second\n")
                    delivery.publish(commits=((workspace, "review", "done"),))
                    raise OSError("trigger rollback")

            real_replace = common.os.replace

            def fail_second_restore(source: object, target: object) -> None:
                if Path(target) == first:
                    raise OSError("injected recovery failure")
                real_replace(source, target)

            with (
                mock.patch.object(common.os, "replace", side_effect=fail_second_restore),
                self.assertRaises(common.WorkspaceTransactionError),
            ):
                common.recover_external_delivery_transactions(output_root)

            common.recover_external_delivery_transactions(output_root)
            self.assertEqual(first.read_text(encoding="utf-8"), "old-first\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "old-second\n")
            self.assert_no_delivery_runs(output_root)

    def test_external_recovery_rejects_unknown_directories_without_deleting_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory) / "delivery"
            sentinel = output_root / ".delivery-runs" / "user-backup" / "keep.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("keep\n", encoding="utf-8")

            with self.assertRaises(common.WorkspaceTransactionError):
                common.recover_external_delivery_transactions(output_root)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_external_recovery_rejects_an_unmarked_hex_run_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory) / "exports"
            sentinel = output_root / ".delivery-runs" / ("a" * 32) / "keep.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("keep\n", encoding="utf-8")

            with self.assertRaisesRegex(common.WorkspaceTransactionError, "marker"):
                common.recover_external_delivery_transactions(output_root)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_external_recovery_preflights_reserved_and_protected_targets(self) -> None:
        for label, invalid_target in (
            ("recovery namespace", ".delivery-runs/poison.txt"),
            ("recorded input", "protected.txt"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                output_root = Path(directory) / "delivery"
                output_root.mkdir()
                protected = output_root / "protected.txt"
                protected.write_text("protected\n", encoding="utf-8")
                ordinary = output_root / "ordinary.txt"
                ordinary.write_text("published\n", encoding="utf-8")
                run_id = "c" * 32
                run_root = output_root / ".delivery-runs" / run_id
                common.write_utf8(run_root / "run.marker", run_id)
                common.write_json(
                    run_root / "journal.json",
                    {
                        "schema_version": 1,
                        "run_id": run_id,
                        "root": str(output_root),
                        "workspaces": [],
                        "inputs": [str(protected)],
                        "entries": [
                            {"target": "ordinary.txt", "existed": False},
                            {"target": invalid_target, "existed": True},
                        ],
                        "directories": [],
                        "commits": [],
                    },
                )

                with self.assertRaisesRegex(
                    common.WorkspaceTransactionError,
                    "cannot recover",
                ):
                    common.recover_external_delivery_transactions(output_root)

                self.assertEqual(ordinary.read_text(encoding="utf-8"), "published\n")
                self.assertEqual(protected.read_text(encoding="utf-8"), "protected\n")
                self.assertTrue(run_root.is_dir())

    def test_external_recovery_cannot_roll_back_an_active_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.bind_workspace(root, "sample-a")
            output_root = root / "delivery"
            target = output_root / "review.html"

            with common.ExternalDeliveryTransaction(
                output_root,
                workspaces=(workspace,),
            ) as delivery:
                staged = delivery.stage_path(target)
                common.write_utf8(staged, "active\n")
                with self.assertRaisesRegex(common.WorkspaceTransactionError, "active"):
                    common.recover_external_delivery_transactions(output_root)
                self.assertEqual(staged.read_text(encoding="utf-8"), "active\n")

            self.assertFalse(target.exists())
            self.assert_no_delivery_runs(output_root)

    def test_export_auto_input_does_not_read_an_active_uncommitted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.bind_workspace(root, "sample-a")
            input_path = workspace / "versions" / "v1_preprocessed.txt"
            committed_text = input_path.read_text(encoding="utf-8")
            uncommitted_text = "UNCOMMITTED EXPORT INPUT\n"
            artifact_published = threading.Event()
            allow_rollback = threading.Event()
            writer_errors: list[BaseException] = []

            def fail_manifest_after_publish(*args: object, **kwargs: object) -> None:
                artifact_published.set()
                if not allow_rollback.wait(timeout=5):
                    raise TimeoutError("test did not release the active transaction")
                raise OSError("injected manifest failure")

            def publish_without_manifest() -> None:
                try:
                    with common.WorkspaceTransaction(workspace) as transaction:
                        common.write_utf8(transaction.stage_path(input_path), uncommitted_text)
                        transaction.commit(
                            {
                                "dry_run": (
                                    "done",
                                    {"output": "versions/v1_preprocessed.txt"},
                                )
                            }
                        )
                except OSError as exc:
                    if str(exc) != "injected manifest failure":
                        writer_errors.append(exc)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    writer_errors.append(exc)

            writer = threading.Thread(target=publish_without_manifest)
            with mock.patch.object(common, "update_stages", side_effect=fail_manifest_after_publish):
                writer.start()
                try:
                    self.assertTrue(artifact_published.wait(timeout=5))
                    self.assertEqual(input_path.read_text(encoding="utf-8"), uncommitted_text)
                    with (
                        mock.patch.object(export_outputs, "read_utf8", wraps=common.read_utf8) as read,
                        self.assertRaisesRegex(common.WorkspaceTransactionError, "active"),
                    ):
                        export_outputs.prepare_export(workspace, "auto", None)
                    read.assert_not_called()
                finally:
                    allow_rollback.set()
                    writer.join(timeout=5)

            self.assertFalse(writer.is_alive())
            self.assertEqual(writer_errors, [])
            self.assertEqual(input_path.read_text(encoding="utf-8"), committed_text)
            self.assertEqual(
                export_outputs.prepare_export(workspace, "auto", None)["text"],
                committed_text,
            )
            self.assertFalse((workspace / ".runs").exists())


if __name__ == "__main__":
    unittest.main()
