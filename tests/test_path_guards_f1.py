from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Iterator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import apply_decisions  # noqa: E402
import build_review_html  # noqa: E402
import common  # noqa: E402
import normalize_layout  # noqa: E402
import preprocess  # noqa: E402
import rollback  # noqa: E402
import scan_ads  # noqa: E402
from support_attestation import bind_passed_attestation  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def tree_snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {".": "missing"}
    snapshot = {".": "dir"}
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            snapshot[relative + "/"] = "dir"
        else:
            snapshot[relative] = "other"
    return snapshot


def make_workspace(root: Path, *, source_inside: bool = False) -> tuple[Path, Path, Path]:
    workspace = root / "sample-a.txt.cleanwork"
    for name in common.WORKSPACE_DIRS:
        (workspace / name).mkdir(parents=True, exist_ok=True)
    outside = root / "outside"
    outside.mkdir()
    source = workspace / "source.txt" if source_inside else root / "sample-a.txt"
    source_text = "第一章 起点\n正文甲。\n"
    source.write_text(source_text, encoding="utf-8")
    v0 = workspace / "versions/v0_original.txt"
    v0.write_text(source_text, encoding="utf-8")
    (workspace / "versions/v1_preprocessed.txt").write_text(
        "第一章 起点\n访问 https://reader.example/path\n正文甲。\n",
        encoding="utf-8",
    )
    v1 = workspace / "versions/v1_preprocessed.txt"
    v0_hash = hashlib.sha256(v0.read_bytes()).hexdigest()
    v1_hash = hashlib.sha256(v1.read_bytes()).hexdigest()
    stages = common.default_stages()
    stages["0_preprocess"] = {
        "status": "done",
        "input": "versions/v0_original.txt",
        "output": "versions/v1_preprocessed.txt",
        "run_id": "1" * 32,
        "artifacts": ["versions/v1_preprocessed.txt"],
    }
    write_json(
        workspace / "manifest.json",
        {
            "schema_version": 2,
            "source": {
                "path": str(source.resolve()),
                "name": source.name,
                "size_bytes": source.stat().st_size,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            },
            "v0": {
                "path": "versions/v0_original.txt",
                "size_bytes": v0.stat().st_size,
                "sha256": v0_hash,
            },
            "workspace": str(workspace.resolve()),
            "current_head": "versions/v1_preprocessed.txt",
            "artifacts": {
                "versions/v0_original.txt": {
                    "path": "versions/v0_original.txt",
                    "sha256": v0_hash,
                    "size_bytes": v0.stat().st_size,
                    "parent_path": None,
                    "parent_sha256": None,
                    "run_id": "0" * 32,
                    "stage": "source_snapshot",
                    "config_sha256": None,
                    "decision_sha256": None,
                },
                "versions/v1_preprocessed.txt": {
                    "path": "versions/v1_preprocessed.txt",
                    "sha256": v1_hash,
                    "size_bytes": v1.stat().st_size,
                    "parent_path": "versions/v0_original.txt",
                    "parent_sha256": v0_hash,
                    "run_id": "1" * 32,
                    "stage": "0_preprocess",
                    "config_sha256": None,
                    "decision_sha256": None,
                },
            },
            "stages": stages,
        },
    )
    return workspace, outside, source


@contextlib.contextmanager
def directory_link(link: Path, target: Path) -> Iterator[None]:
    if link.exists():
        link.rmdir()
    if os.name == "nt":
        process = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if process.returncode != 0:
            raise AssertionError(f"could not create test junction: {process.stderr or process.stdout}")
    else:
        link.symlink_to(target, target_is_directory=True)
    if link.resolve() != target.resolve():
        raise AssertionError("test link does not resolve to its target")
    try:
        yield
    finally:
        if link.exists() or link.is_symlink():
            if os.name == "nt":
                os.rmdir(link)
            else:
                link.unlink()


class WorkspacePathGuardF1Tests(unittest.TestCase):
    def assert_zero_write_rejection(self, root: Path, call: Callable[[], Any]) -> None:
        before = tree_snapshot(root)
        error: BaseException | None = None
        try:
            call()
        except BaseException as exc:
            error = exc
        after = tree_snapshot(root)
        guard_type = getattr(common, "WorkspacePathError", None)
        self.assertIsNotNone(guard_type, "common.WorkspacePathError must exist")
        self.assertIsInstance(error, guard_type)
        self.assertEqual(after, before, "guard rejection must happen before the first write")

    def test_transactions_recheck_late_junctions_before_atomic_directory_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, outside, _ = make_workspace(root)
            manifest_before = (workspace / "manifest.json").read_bytes()

            with common.WorkspaceTransaction(workspace) as transaction:
                bundle = workspace / "late-workspace" / "bundle"
                transaction.stage_directory(bundle, require_new=True)
                common.write_utf8(transaction.stage_path(bundle / "book.txt"), "staged\n")
                with directory_link(workspace / "late-workspace", outside):
                    with self.assertRaises(common.WorkspacePathError):
                        transaction.commit(
                            {"dry_run": ("done", {"output": "late-workspace/bundle"})}
                        )

            self.assertFalse((outside / "bundle" / "book.txt").exists())
            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)

            delivery_root = root / "delivery"
            delivery_root.mkdir()
            with common.ExternalDeliveryTransaction(
                delivery_root,
                workspaces=(workspace,),
            ) as delivery:
                bundle = delivery_root / "late-external" / "bundle"
                delivery.stage_directory(bundle, require_new=True)
                common.write_utf8(delivery.stage_path(bundle / "book.txt"), "staged\n")
                with directory_link(delivery_root / "late-external", outside):
                    with self.assertRaises(common.WorkspacePathError):
                        delivery.publish()

            self.assertFalse((outside / "bundle" / "book.txt").exists())

    def test_rejects_parent_segments_and_absolute_internal_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, outside, _ = make_workspace(root)
            unsafe = [
                "../outside/input.txt",
                "versions/../report/input.txt",
                "candidates/data.jsonl:stream",
                "candidates/trailing.",
                "candidates/trailing ",
                str((workspace / "versions/v1_preprocessed.txt").resolve()),
                str((outside / "input.txt").resolve()),
            ]
            if os.name == "nt":
                unsafe.extend([r"\rooted\input.txt", r"C:relative.txt", r"\\server\share\input.txt"])
            for value in unsafe:
                with self.subTest(value=value):
                    with self.assertRaises(getattr(common, "WorkspacePathError", ValueError)):
                        common.resolve_in_workspace(workspace, value, role="read")

    def test_rejects_real_link_escape_for_read_and_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, outside, _ = make_workspace(root)
            (outside / "external.txt").write_text("外部哨兵。\n", encoding="utf-8")
            link = workspace / "candidates/escape"
            with directory_link(link, outside):
                before = tree_snapshot(root)
                for value, role in (
                    ("candidates/escape/external.txt", "read"),
                    ("candidates/escape/new.jsonl", "write"),
                ):
                    with self.subTest(role=role):
                        with self.assertRaises(getattr(common, "WorkspacePathError", ValueError)):
                            common.resolve_in_workspace(workspace, value, role=role)
                self.assertEqual(tree_snapshot(root), before)

    def test_write_role_protects_inputs_v0_manifest_source_and_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _, source = make_workspace(root, source_inside=True)
            input_path = common.resolve_in_workspace(
                workspace,
                "versions/v1_preprocessed.txt",
                role="read",
            )
            hardlink = workspace / "versions/input-alias.txt"
            os.link(input_path, hardlink)
            values = (
                "versions/v1_preprocessed.txt",
                "versions/input-alias.txt",
                "versions/v0_original.txt",
                "manifest.json",
                str(source.relative_to(workspace)),
            )
            before = tree_snapshot(root)
            for value in values:
                with self.subTest(value=value):
                    with self.assertRaises(getattr(common, "WorkspacePathError", ValueError)):
                        common.resolve_in_workspace(
                            workspace,
                            value,
                            role="write",
                            inputs=(input_path,),
                        )
            self.assertEqual(tree_snapshot(root), before)

    def test_run_preflight_protects_inputs_from_manifest_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _, _ = make_workspace(root)
            manifest_alias = workspace / "decisions/manifest-alias.jsonl"
            os.link(workspace / "manifest.json", manifest_alias)
            self.assert_zero_write_rejection(
                root,
                lambda: common.resolve_workspace_paths(
                    workspace,
                    reads={"decisions": "decisions/manifest-alias.jsonl"},
                    writes={"output": "versions/v2_ads_removed.txt"},
                ),
            )

    def test_inherited_config_files_are_protected_as_run_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _, _ = make_workspace(root)
            parent_config = root / "parent-config.json"
            child_config = root / "child-config.json"
            write_json(parent_config, {})
            write_json(child_config, {"inherits": parent_config.name})
            os.link(parent_config, workspace / "report/layout_report.json")
            self.assert_zero_write_rejection(
                root,
                lambda: normalize_layout.run(
                    workspace,
                    "versions/v1_preprocessed.txt",
                    "versions/v5_layout_final.txt",
                    child_config,
                ),
            )

    def test_scan_rejects_unsafe_inputs_and_outputs_without_writing(self) -> None:
        cases = ("parent", "absolute_inside", "absolute_outside", "v0", "manifest", "source", "same")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace, outside, source = make_workspace(root, source_inside=True)
                (outside / "input.txt").write_text("第一章 起点\n外部正文。\n", encoding="utf-8")
                output_values = {
                    "parent": "../outside/escaped.jsonl",
                    "absolute_inside": str((workspace / "candidates/absolute.jsonl").resolve()),
                    "absolute_outside": str((outside / "absolute.jsonl").resolve()),
                    "v0": "versions/v0_original.txt",
                    "manifest": "manifest.json",
                    "source": str(source.relative_to(workspace)),
                    "same": "versions/v1_preprocessed.txt",
                }
                self.assert_zero_write_rejection(
                    root,
                    lambda value=output_values[case]: scan_ads.run(
                        workspace,
                        "versions/v1_preprocessed.txt",
                        value,
                        12,
                        20,
                        20,
                    ),
                )

        for input_value in ("../outside/input.txt",):
            with self.subTest(input=input_value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace, outside, _ = make_workspace(root)
                (outside / "input.txt").write_text("第一章 起点\n外部正文。\n", encoding="utf-8")
                self.assert_zero_write_rejection(
                    root,
                    lambda: scan_ads.run(workspace, input_value, "candidates/ads.jsonl", 12, 20, 20),
                )

    def test_scan_preflights_junction_output_and_fixed_report_directory(self) -> None:
        for target_name in ("custom-output", "fixed-report"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace, outside, _ = make_workspace(root)
                target = outside / target_name
                target.mkdir()
                if target_name == "custom-output":
                    link = workspace / "candidates/escape"
                    output = "candidates/escape/ads.jsonl"
                else:
                    link = workspace / "report"
                    output = "candidates/ads.jsonl"
                with directory_link(link, target):
                    self.assert_zero_write_rejection(
                        root,
                        lambda value=output: scan_ads.run(
                            workspace,
                            "versions/v1_preprocessed.txt",
                            value,
                            12,
                            20,
                            20,
                        ),
                    )

    def test_apply_preflights_logs_and_output_input_collision(self) -> None:
        for case in ("logs-junction", "same-output"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace, outside, _ = make_workspace(root)
                text = (workspace / "versions/v1_preprocessed.txt").read_text(encoding="utf-8")
                original = "https://reader.example/path"
                write_jsonl(
                    workspace / "decisions/ads_decisions.jsonl",
                    [
                        {
                            "candidate_id": "AD-0001",
                            "verdict": "delete",
                            "anchors": [{"offset": text.index(original), "original": original}],
                        }
                    ],
                )
                output = "versions/v1_preprocessed.txt" if case == "same-output" else "versions/v2_ads_removed.txt"
                if case == "logs-junction":
                    target = outside / "logs"
                    target.mkdir()
                    link_context = directory_link(workspace / "logs", target)
                else:
                    link_context = contextlib.nullcontext()
                with link_context:
                    self.assert_zero_write_rejection(
                        root,
                        lambda: apply_decisions.run(
                            workspace,
                            "ads",
                            "versions/v1_preprocessed.txt",
                            "decisions/ads_decisions.jsonl",
                            output,
                            "2_ads",
                        ),
                    )

    def test_preprocess_source_collision_is_rejected_before_workspace_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _, _ = make_workspace(root)
            source = workspace / "versions/v1_preprocessed.txt"
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            manifest["source"] = {
                "path": str(source.resolve()),
                "name": source.name,
                "size_bytes": manifest["v0"]["size_bytes"],
                "sha256": manifest["v0"]["sha256"],
            }
            write_json(workspace / "manifest.json", manifest)
            self.assert_zero_write_rejection(root, lambda: preprocess.run(source, str(workspace)))

    def test_all_rollback_levels_preflight_unsafe_output(self) -> None:
        for level in ("all", "module", "chapter", "point"):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace, outside, _ = make_workspace(root)
                (workspace / "versions/v2_ads_removed.txt").write_text(
                    "第一章 起点\n正文甲。\n",
                    encoding="utf-8",
                )
                write_jsonl(
                    workspace / "decisions/ads_decisions.jsonl",
                    [
                        {
                            "candidate_id": "AD-0001",
                            "verdict": "delete",
                            "anchors": [
                                {
                                    "original": "https://reader.example/path",
                                    "chapter": {"index": 1},
                                }
                            ],
                        }
                    ],
                )
                output = "../outside/rollback.txt"
                calls = {
                    "all": lambda: rollback.rollback_all(workspace, output, True),
                    "module": lambda: rollback.rollback_module(workspace, "ads", output, True),
                    "chapter": lambda: rollback.rollback_chapter(workspace, "ads", 1, output),
                    "point": lambda: rollback.rollback_point(workspace, "ads", "AD-0001", output),
                }
                self.assert_zero_write_rejection(root, calls[level])
                self.assertEqual(list((workspace / "decisions").glob("_rollback_*.jsonl")), [])
                self.assertEqual(list(outside.glob("rollback.txt")), [])

    def test_external_output_directory_is_a_separate_explicit_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, outside, _ = make_workspace(root)
            external = outside / "exports"
            resolved = common.resolve_external_output_dir(external, workspaces=(workspace,))
            self.assertEqual(resolved, external.resolve())
            self.assertFalse(external.exists(), "validation must not create the external directory")
            with self.assertRaises(getattr(common, "WorkspacePathError", ValueError)):
                common.resolve_in_workspace(workspace, str(external), role="write")
            with self.assertRaises(getattr(common, "WorkspacePathError", ValueError)):
                common.resolve_external_output_dir(workspace / "report", workspaces=(workspace,))
            with self.assertRaises(getattr(common, "WorkspacePathError", ValueError)):
                common.resolve_external_output_dir(root, workspaces=(workspace,))

    def test_external_output_children_cannot_escape_or_alias_workspace_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, outside, _ = make_workspace(root)
            output_root = outside / "exports"
            output_root.mkdir()
            input_path = workspace / "versions/v1_preprocessed.txt"
            alias = output_root / "alias.txt"
            os.link(input_path, alias)
            with self.assertRaises(getattr(common, "WorkspacePathError", ValueError)):
                common.resolve_external_output_paths(
                    output_root,
                    writes={"txt": "alias.txt"},
                    workspaces=(workspace,),
                    inputs=(input_path,),
                )

            escape_target = outside / "escaped"
            escape_target.mkdir()
            with directory_link(output_root / "linked", escape_target):
                with self.assertRaises(getattr(common, "WorkspacePathError", ValueError)):
                    common.resolve_external_output_paths(
                        output_root,
                        writes={"txt": "linked/output.txt"},
                        workspaces=(workspace,),
                        inputs=(input_path,),
                    )

    def test_single_export_cli_honors_explicit_external_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, outside, _ = make_workspace(root)
            bind_passed_attestation(workspace)
            output_root = outside / "exports"
            process = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/export_outputs.py"),
                    str(workspace),
                    "--input",
                    "versions/v1_preprocessed.txt",
                    "--output-root",
                    str(output_root),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(process.stdout)
            delivered = Path(report["output_dir_abs"]).resolve()
            self.assertEqual(delivered, output_root.resolve() / delivered.name)

    def test_recursive_review_discovery_stays_within_its_search_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            search_root = root / "search"
            search_root.mkdir()
            outside_root = root / "outside-root"
            outside_workspace, _, _ = make_workspace(outside_root)
            link = search_root / "escaped.cleanwork"
            with directory_link(link, outside_workspace):
                self.assertEqual(
                    build_review_html.discover_workspaces([search_root], recursive=True),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
