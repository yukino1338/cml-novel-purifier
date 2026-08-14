from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import apply_decisions  # noqa: E402
import common  # noqa: E402
import finalize_ad_decisions  # noqa: E402
import make_ad_decisions  # noqa: E402
import normalize_layout  # noqa: E402
import parse_structure  # noqa: E402
import preprocess  # noqa: E402
import rollback  # noqa: E402
import scan_ads  # noqa: E402
import verify  # noqa: E402
from support_provenance import run_isolated_apply, run_isolated_verify  # noqa: E402


class ExecutionTransactionsF1Tests(unittest.TestCase):
    def bind_workspace(self, root: Path) -> Path:
        source = root / "sample-a.txt"
        source.write_text("第一章 示例\n正文甲。\n", encoding="utf-8")
        workspace = root / "sample-a.txt.cleanwork"
        preprocess.run(source, str(workspace))
        return workspace

    def old_file(self, workspace: Path, relative: str, content: bytes) -> Path:
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def assert_unchanged(self, workspace: Path, paths: list[Path], manifest: bytes) -> None:
        self.assertEqual((workspace / "manifest.json").read_bytes(), manifest)
        self.assertFalse((workspace / ".runs").exists())
        for path, before in self.before.items():
            self.assertEqual(path.read_bytes(), before)

    def remember(self, paths: list[Path]) -> None:
        self.before = {path: path.read_bytes() for path in paths}

    def test_apply_failure_does_not_publish_body_or_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            decisions = self.old_file(workspace, "decisions/ads_decisions.jsonl", b"")
            output = self.old_file(workspace, "versions/v2_ads_removed.txt", b"old body\n")
            operations = self.old_file(workspace, "logs/operations.jsonl", b'{"old":"operation"}\n')
            anomalies = self.old_file(workspace, "logs/anomalies.jsonl", b'{"old":"anomaly"}\n')
            paths = [output, operations, anomalies]
            self.remember(paths)
            manifest = (workspace / "manifest.json").read_bytes()

            with (
                mock.patch.object(apply_decisions, "log_operations", side_effect=OSError("injected log failure")),
                self.assertRaisesRegex(OSError, "injected log failure"),
            ):
                run_isolated_apply(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    str(decisions.relative_to(workspace)),
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                )

            self.assert_unchanged(workspace, paths, manifest)

    def test_layout_failure_does_not_publish_body_or_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            output = self.old_file(workspace, "versions/v5_layout_final.txt", b"old body\n")
            report = self.old_file(workspace, "report/layout_report.json", b'{"old":"report"}\n')
            paths = [output, report]
            self.remember(paths)
            manifest = (workspace / "manifest.json").read_bytes()

            with (
                mock.patch.object(normalize_layout, "write_json", side_effect=OSError("injected report failure")),
                self.assertRaisesRegex(OSError, "injected report failure"),
            ):
                normalize_layout.run(workspace, "versions/v1_preprocessed.txt", "versions/v5_layout_final.txt", None)

            self.assert_unchanged(workspace, paths, manifest)

    def test_verify_failure_does_not_publish_any_report_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            self.old_file(
                workspace,
                "versions/v2_ads_removed.txt",
                "第一章 示例\n正文甲。\n".encode("utf-8"),
            )
            self.old_file(workspace, "decisions/ads_decisions.jsonl", b"")
            output = self.old_file(workspace, "report/verify_report.json", b'{"old":"verify"}\n')
            diff = self.old_file(workspace, "report/diff_v1_v2.html", b"old diff\n")
            final = self.old_file(workspace, "report/final_report.md", b"old final\n")
            paths = [output, diff, final]
            self.remember(paths)
            manifest = (workspace / "manifest.json").read_bytes()

            with (
                mock.patch.object(verify, "write_final_report", side_effect=OSError("injected final report failure")),
                self.assertRaisesRegex(OSError, "injected final report failure"),
            ):
                run_isolated_verify(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "versions/v2_ads_removed.txt",
                    "decisions/ads_decisions.jsonl",
                    True,
                )

            self.assert_unchanged(workspace, paths, manifest)

    def test_rollback_failure_does_not_publish_version_or_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.bind_workspace(Path(directory))
            output = self.old_file(workspace, "versions/rollback_v0_original.txt", b"old rollback\n")
            report = self.old_file(workspace, "report/rollback_report.json", b'{"old":"report"}\n')
            paths = [output, report]
            self.remember(paths)
            manifest = (workspace / "manifest.json").read_bytes()

            with (
                mock.patch.object(rollback, "write_json", side_effect=OSError("injected rollback report failure")),
                self.assertRaisesRegex(OSError, "injected rollback report failure"),
            ):
                rollback.rollback_all(workspace, None, True)

            self.assert_unchanged(workspace, paths, manifest)

    def test_ads_mutators_hold_the_workspace_lock_before_their_first_read(self) -> None:
        def assert_locked_before(
            workspace: Path,
            module: object,
            read_name: str,
            call: object,
        ) -> None:
            entered = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []
            original = getattr(module, read_name)

            def blocked_read(*args: object, **kwargs: object) -> object:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("test read barrier timed out")
                return original(*args, **kwargs)

            def invoke() -> None:
                try:
                    call()  # type: ignore[operator]
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(module, read_name, side_effect=blocked_read):
                thread = threading.Thread(target=invoke, daemon=True)
                thread.start()
                self.assertTrue(entered.wait(10), f"{module}.{read_name} was not reached")
                try:
                    with self.assertRaisesRegex(
                        common.WorkspaceTransactionError,
                        "another workspace transaction is active",
                    ):
                        with common.workspace_transaction_lock(workspace):
                            pass
                finally:
                    release.set()
                    thread.join(20)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "anonymous.txt"
            source.write_text("第一章 起点\n匿名正文。\n", encoding="utf-8")
            workspace = preprocess.run(source)
            parse_structure.run(workspace)

            assert_locked_before(
                workspace,
                scan_ads,
                "read_utf8",
                lambda: scan_ads.run(
                    workspace,
                    "versions/v1_preprocessed.txt",
                    "candidates/ads.jsonl",
                    12,
                    50,
                    100,
                ),
            )
            assert_locked_before(
                workspace,
                make_ad_decisions,
                "load_current_ad_candidates",
                lambda: make_ad_decisions.run(
                    workspace,
                    "candidates/ads_pages",
                    "decisions/ads_decisions.draft.jsonl",
                    "meta/book_profile.json",
                    True,
                ),
            )
            common.write_jsonl(workspace / "decisions/ads_agent_reviews.jsonl", [])
            assert_locked_before(
                workspace,
                finalize_ad_decisions,
                "load_current_ad_candidates",
                lambda: finalize_ad_decisions.run(
                    workspace,
                    "candidates/ads_pages",
                    "decisions/ads_agent_reviews.jsonl",
                    "decisions/ads_decisions.draft.jsonl",
                    "decisions/ads_decisions.jsonl",
                ),
            )
            assert_locked_before(
                workspace,
                apply_decisions,
                "read_utf8",
                lambda: apply_decisions.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "decisions/ads_decisions.jsonl",
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                ),
            )


if __name__ == "__main__":
    unittest.main()
