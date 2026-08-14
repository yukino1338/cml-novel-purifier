from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import preprocess  # noqa: E402


VERSION = "versions/v5_fault_injection.txt"
PAGE_SENTINEL = "candidates/ads_pages/ads_page_001.jsonl"
PAGE = "candidates/ads_pages/ads_page_002.jsonl"
LOG = "logs/fault_operations.jsonl"
REPORT = "report/fault_injection_report.json"
ARTIFACTS = (VERSION, PAGE, LOG, REPORT)

OLD_BYTES = {
    VERSION: "第一章 示例\n旧版本正文。\n".encode(),
    LOG: b'{"operation":"old"}\n',
    REPORT: b'{"status":"old"}\n',
}
PAGE_SENTINEL_BYTES = b'{"candidate":"old-page-one"}\n'
NEW_VERSION = "第一章 示例\n新版本正文。\n"
NEW_PAGE = [{"candidate": "new"}]
NEW_LOG = [{"operation": "new"}]
NEW_REPORT = {"status": "new", "complete": True}
NEW_BYTES = {
    VERSION: NEW_VERSION.encode(),
    PAGE: (json.dumps(NEW_PAGE[0], separators=(",", ":")) + "\n").encode(),
    LOG: (json.dumps(NEW_LOG[0], separators=(",", ":")) + "\n").encode(),
    REPORT: (json.dumps(NEW_REPORT, indent=2) + "\n").encode(),
}

SUBPROCESS_INTERRUPT_SCRIPT = r"""
import socket
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import common

workspace = Path(sys.argv[2])
port = int(sys.argv[3])
target = workspace / "versions/v5_fault_injection.txt"
real_replace = common.os.replace


def interrupt_after_publish(source, destination):
    real_replace(source, destination)
    if Path(destination) != target:
        return
    with socket.create_connection(("127.0.0.1", port), timeout=10) as control:
        control.sendall(b"READY")
        control.recv(1)
    raise RuntimeError("parent did not terminate interrupted transaction")


with common.WorkspaceTransaction(workspace) as transaction:
    common.write_utf8(
        transaction.stage_path(target),
        "第一章 示例\n新版本正文。\n",
    )
    common.os.replace = interrupt_after_publish
    transaction.commit(
        {
            "dry_run": (
                "done",
                {
                    "input": "versions/v1_preprocessed.txt",
                    "output": "versions/v5_fault_injection.txt",
                },
            )
        }
    )
"""


class SimulatedProcessInterruption(BaseException):
    """Represent process loss after one atomic replace without terminating the test runner."""


class PersistenceFaultsF7Tests(unittest.TestCase):
    def make_workspace(self, root: Path) -> Path:
        source = root / "anonymous.txt"
        source.write_text("第一章 示例\n原始正文。\n", encoding="utf-8")
        workspace = preprocess.run(source)
        for relative, content in OLD_BYTES.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        page_sentinel = workspace / PAGE_SENTINEL
        page_sentinel.parent.mkdir(parents=True, exist_ok=True)
        page_sentinel.write_bytes(PAGE_SENTINEL_BYTES)
        return workspace

    def stage_run(self, transaction: common.WorkspaceTransaction, workspace: Path) -> None:
        common.write_utf8(transaction.stage_path(workspace / VERSION), NEW_VERSION)
        common.write_jsonl(transaction.stage_path(workspace / PAGE), NEW_PAGE)
        common.write_jsonl(transaction.stage_path(workspace / LOG), NEW_LOG)
        common.write_json(transaction.stage_path(workspace / REPORT), NEW_REPORT)

    def commit_run(self, transaction: common.WorkspaceTransaction) -> dict[str, Any]:
        return transaction.commit(
            {
                "dry_run": (
                    "done",
                    {
                        "input": "versions/v1_preprocessed.txt",
                        "output": VERSION,
                        "report": REPORT,
                    },
                )
            }
        )

    def assert_artifacts(self, workspace: Path, expected: dict[str, bytes]) -> None:
        for relative, content in expected.items():
            self.assertEqual((workspace / relative).read_bytes(), content, relative)
        self.assertEqual((workspace / PAGE_SENTINEL).read_bytes(), PAGE_SENTINEL_BYTES)
        self.assertEqual((workspace / PAGE).exists(), PAGE in expected)
        common.load_jsonl(workspace / PAGE)
        common.load_jsonl(workspace / LOG)
        self.assertIsInstance(
            json.loads((workspace / REPORT).read_text(encoding="utf-8")),
            dict,
        )

    def assert_current_head_is_complete(self, workspace: Path) -> str:
        manifest = common.load_manifest(workspace)
        current = manifest["current_head"]
        self.assertIn(current, {"versions/v1_preprocessed.txt", VERSION})
        current_path = common.resolve_current_head(workspace)
        self.assertEqual(current_path, workspace / current)
        record = manifest["artifacts"][current]
        self.assertEqual(common.sha256_file(current_path), record["sha256"])
        self.assertEqual(current_path.stat().st_size, record["size_bytes"])
        return current

    def assert_no_transaction_debris(self, workspace: Path) -> None:
        runs = workspace / ".runs"
        self.assertFalse(runs.exists() and any(runs.iterdir()))
        self.assertEqual(list(workspace.rglob("*.tmp")), [])

    def replacement_fault(
        self,
        target: Path,
        fault: str,
    ) -> tuple[Callable[[object, object], None], list[int]]:
        real_replace = common.os.replace
        triggered = [0]

        def replace(source: object, destination: object) -> None:
            if Path(destination) != target or triggered[0]:
                real_replace(source, destination)
                return
            triggered[0] += 1
            if fault == "permission":
                raise PermissionError(errno.EACCES, "injected permission failure")
            if fault == "disk_full":
                raise OSError(errno.ENOSPC, "injected disk-full failure")
            if fault == "truncated_write":
                staged = Path(source)
                payload = staged.read_bytes()
                staged.write_bytes(payload[: max(1, len(payload) // 2)])
                raise OSError(errno.EIO, "injected truncated-write failure")
            if fault == "process_interrupt":
                real_replace(source, destination)
                raise SimulatedProcessInterruption("injected process interruption")
            raise AssertionError(f"unknown fault: {fault}")

        return replace, triggered

    def exercise_fault(self, boundary: str, fault: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_workspace(Path(directory))
            v0 = workspace / "versions/v0_original.txt"
            v0_before = v0.read_bytes()
            manifest_before = (workspace / "manifest.json").read_bytes()
            target = workspace / "manifest.json" if boundary == "manifest" else workspace / {
                "version": VERSION,
                "pagination": PAGE,
                "log": LOG,
                "report": REPORT,
            }[boundary]
            replace, triggered = self.replacement_fault(target, fault)

            if fault == "process_interrupt":
                transaction = common.WorkspaceTransaction(workspace)
                transaction.__enter__()
                try:
                    self.stage_run(transaction, workspace)
                    with (
                        mock.patch.object(common.os, "replace", side_effect=replace),
                        self.assertRaises(SimulatedProcessInterruption),
                    ):
                        self.commit_run(transaction)
                finally:
                    # A terminated process releases its OS lock but cannot run __exit__.
                    transaction._lock.release()
                common.resolve_workspace_paths(workspace)
            else:
                with common.WorkspaceTransaction(workspace) as transaction:
                    self.stage_run(transaction, workspace)
                    with (
                        mock.patch.object(common.os, "replace", side_effect=replace),
                        self.assertRaises(OSError),
                    ):
                        self.commit_run(transaction)

            self.assertEqual(triggered, [1])
            self.assertEqual(v0.read_bytes(), v0_before)
            committed_at_interrupt = fault == "process_interrupt" and boundary == "manifest"
            expected_before_retry = NEW_BYTES if committed_at_interrupt else OLD_BYTES
            self.assert_artifacts(workspace, expected_before_retry)
            current = self.assert_current_head_is_complete(workspace)
            self.assertEqual(
                current,
                VERSION if committed_at_interrupt else "versions/v1_preprocessed.txt",
            )
            if not committed_at_interrupt:
                self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            self.assert_no_transaction_debris(workspace)

            with common.WorkspaceTransaction(workspace) as retry:
                self.stage_run(retry, workspace)
                self.commit_run(retry)

            self.assertEqual(v0.read_bytes(), v0_before)
            self.assert_artifacts(workspace, NEW_BYTES)
            self.assertEqual(self.assert_current_head_is_complete(workspace), VERSION)
            manifest = common.load_manifest(workspace)
            self.assertEqual(
                set(manifest["stages"]["dry_run"]["artifacts"]),
                set(ARTIFACTS),
            )
            self.assert_no_transaction_debris(workspace)

    def test_permission_errors_fail_closed_at_every_commit_boundary(self) -> None:
        for boundary in ("version", "pagination", "log", "report", "manifest"):
            with self.subTest(boundary=boundary):
                self.exercise_fault(boundary, "permission")

    def test_truncated_writes_fail_closed_at_every_commit_boundary(self) -> None:
        for boundary in ("version", "pagination", "log", "report", "manifest"):
            with self.subTest(boundary=boundary):
                self.exercise_fault(boundary, "truncated_write")

    def test_disk_full_errors_fail_closed_at_every_commit_boundary(self) -> None:
        for boundary in ("version", "pagination", "log", "report", "manifest"):
            with self.subTest(boundary=boundary):
                self.exercise_fault(boundary, "disk_full")

    def test_process_interruptions_recover_at_every_commit_boundary(self) -> None:
        for boundary in ("version", "pagination", "log", "report", "manifest"):
            with self.subTest(boundary=boundary):
                self.exercise_fault(boundary, "process_interrupt")

    def test_real_terminated_subprocess_releases_lock_and_recovers_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_workspace(Path(directory))
            target = workspace / VERSION
            manifest_before = (workspace / "manifest.json").read_bytes()
            v0_before = (workspace / "versions/v0_original.txt").read_bytes()
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                listener.settimeout(15)
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        SUBPROCESS_INTERRUPT_SCRIPT,
                        str(ROOT / "scripts"),
                        str(workspace),
                        str(listener.getsockname()[1]),
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                try:
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        process.kill()
                        stdout, stderr = process.communicate(timeout=10)
                        self.fail(
                            "interrupted transaction child did not reach publish boundary\n"
                            f"stdout:\n{stdout}\nstderr:\n{stderr}"
                        )
                    with connection:
                        connection.settimeout(10)
                        ready = b""
                        while len(ready) < 5:
                            chunk = connection.recv(5 - len(ready))
                            if not chunk:
                                break
                            ready += chunk
                        self.assertEqual(ready, b"READY")
                        process.terminate()
                        process.wait(timeout=10)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)

            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(target.read_bytes(), NEW_BYTES[VERSION])
            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)
            self.assertTrue(any((workspace / ".runs").glob("*/journal.json")))

            with common.WorkspaceTransaction(workspace) as retry:
                self.assertEqual(target.read_bytes(), OLD_BYTES[VERSION])
                common.write_utf8(retry.stage_path(target), NEW_VERSION)
                retry.commit(
                    {
                        "dry_run": (
                            "done",
                            {
                                "input": "versions/v1_preprocessed.txt",
                                "output": VERSION,
                            },
                        )
                    }
                )

            self.assertEqual((workspace / "versions/v0_original.txt").read_bytes(), v0_before)
            self.assertEqual(target.read_bytes(), NEW_BYTES[VERSION])
            self.assertEqual(self.assert_current_head_is_complete(workspace), VERSION)
            self.assert_no_transaction_debris(workspace)


if __name__ == "__main__":
    unittest.main()
