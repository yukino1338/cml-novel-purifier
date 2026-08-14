from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dry_run  # noqa: E402
import export_outputs  # noqa: E402
import preprocess  # noqa: E402
import verify  # noqa: E402


class CliExitCodeF7Tests(unittest.TestCase):
    def invoke(
        self,
        module: object,
        argv: list[str],
        run_patches: dict[str, object],
    ) -> tuple[dict[str, object], mock.Mock]:
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            sys, "exit"
        ) as exit_mock, redirect_stdout(stdout):
            with ExitStack() as stack:
                for name, value in run_patches.items():
                    stack.enter_context(mock.patch.object(module, name, return_value=value))
                module.main()
        return json.loads(stdout.getvalue()), exit_mock

    def test_verify_exits_nonzero_for_every_non_passed_status(self) -> None:
        for status, expected_exit in (
            ("passed", False),
            ("blocked", True),
            ("incomplete", True),
        ):
            with self.subTest(status=status):
                report = {
                    "status": status,
                    "warnings": [],
                    "char_counts": {"before": 10, "after": 10, "delta": 0},
                }
                output, exit_mock = self.invoke(
                    verify,
                    ["verify.py", "book.cleanwork"],
                    {"run": report},
                )

                self.assertEqual(output["status"], status)
                if expected_exit:
                    exit_mock.assert_called_once_with(1)
                else:
                    exit_mock.assert_not_called()

    def test_preprocess_exits_nonzero_when_encoding_is_blocked(self) -> None:
        workspace = Path("book.txt.cleanwork")
        for blocked, expected_exit in ((False, False), (True, True)):
            with self.subTest(blocked=blocked), mock.patch.object(
                sys, "argv", ["preprocess.py", "book.txt"]
            ), mock.patch.object(
                preprocess, "run", return_value=workspace
            ), mock.patch.object(
                preprocess,
                "read_utf8",
                return_value=json.dumps(
                    {"encoding_detection": {"blocked": blocked}}, ensure_ascii=False
                ),
            ), mock.patch.object(sys, "exit") as exit_mock, redirect_stdout(
                io.StringIO()
            ):
                preprocess.main()

            if expected_exit:
                exit_mock.assert_called_once_with(1)
            else:
                exit_mock.assert_not_called()

    def test_dry_run_exits_nonzero_until_complete(self) -> None:
        for status, expected_exit in (("complete", False), ("pending", True)):
            with self.subTest(status=status):
                output, exit_mock = self.invoke(
                    dry_run,
                    ["dry_run.py", "book.cleanwork"],
                    {"run": {"status": status, "modules": {}}},
                )

                self.assertEqual(output["status"], status)
                if expected_exit:
                    exit_mock.assert_called_once_with(1)
                else:
                    exit_mock.assert_not_called()

    def test_export_single_and_batch_use_passed_as_the_only_success_status(self) -> None:
        cases = (
            (["export_outputs.py", "one.cleanwork"], "passed", False),
            (["export_outputs.py", "one.cleanwork"], "failed", True),
            (
                ["export_outputs.py", "one.cleanwork", "two.cleanwork"],
                "passed",
                False,
            ),
            (
                ["export_outputs.py", "one.cleanwork", "two.cleanwork"],
                "partial",
                True,
            ),
            (
                ["export_outputs.py", "one.cleanwork", "two.cleanwork"],
                "failed",
                True,
            ),
        )
        for argv, status, expected_exit in cases:
            with self.subTest(count=len(argv) - 1, status=status):
                report = {"status": status, "items": []}
                output, exit_mock = self.invoke(
                    export_outputs,
                    argv,
                    {"run": report, "run_batch": report},
                )

                self.assertEqual(output["status"], status)
                if expected_exit:
                    exit_mock.assert_called_once_with(1)
                else:
                    exit_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
