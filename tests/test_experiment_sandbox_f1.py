from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import experiment  # noqa: E402


class ExperimentSandboxF1Tests(unittest.TestCase):
    def test_only_a_valid_marked_sandbox_can_be_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples = root / "samples"
            samples.mkdir()
            sandbox = root / "experiments" / "run"

            prepared, first_run = experiment.prepare_sandbox(
                samples,
                sandbox,
                project_root=root / "project",
                user_home=root / "home",
            )
            sentinel = prepared / "old.txt"
            sentinel.write_text("old\n", encoding="utf-8")
            marker = json.loads((prepared / experiment.SANDBOX_MARKER).read_text(encoding="utf-8"))
            self.assertEqual(marker["run_id"], first_run)
            self.assertEqual(marker["sandbox"], str(prepared))

            prepared_again, second_run = experiment.prepare_sandbox(
                samples,
                sandbox,
                project_root=root / "project",
                user_home=root / "home",
            )

            self.assertEqual(prepared_again, prepared)
            self.assertNotEqual(second_run, first_run)
            self.assertFalse(sentinel.exists())

    def test_unmarked_or_tampered_existing_directory_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples = root / "samples"
            samples.mkdir()
            for case in ("unmarked", "bad-run", "wrong-path"):
                with self.subTest(case=case):
                    sandbox = root / case / "run"
                    sandbox.mkdir(parents=True)
                    sentinel = sandbox / "keep.txt"
                    sentinel.write_text("keep\n", encoding="utf-8")
                    if case != "unmarked":
                        marker = {
                            "schema_version": 1,
                            "tool": "cml-novel-purifier-experiment",
                            "run_id": "not-a-run" if case == "bad-run" else "a" * 32,
                            "sandbox": str(sandbox if case == "bad-run" else root / "other"),
                            "allowed_root": str(sandbox.parent),
                        }
                        (sandbox / experiment.SANDBOX_MARKER).write_text(
                            json.dumps(marker),
                            encoding="utf-8",
                        )

                    with self.assertRaisesRegex(ValueError, "marker|sandbox"):
                        experiment.prepare_sandbox(
                            samples,
                            sandbox,
                            project_root=root / "project",
                            user_home=root / "home",
                        )

                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_sample_project_home_root_and_workspace_targets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples = root / "samples"
            samples.mkdir()
            project = root / "project"
            project.mkdir()
            home = root / "home"
            home.mkdir()
            workspace = root / "sample-a.txt.cleanwork"
            workspace.mkdir()
            dangerous = (
                samples,
                root,
                project,
                home,
                workspace,
            )

            for target in dangerous:
                with self.subTest(target=target):
                    sentinel = target / "keep.txt"
                    sentinel.write_text("keep\n", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "sandbox|sample|project|home|workspace"):
                        experiment.prepare_sandbox(
                            samples,
                            target,
                            project_root=project,
                            user_home=home,
                        )
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
                    sentinel.unlink()

    def test_linked_sandbox_is_rejected_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples = root / "samples"
            samples.mkdir()
            target = root / "outside"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            link = root / "sandbox-link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink is unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "link|sandbox"):
                experiment.prepare_sandbox(
                    samples,
                    link,
                    project_root=root / "project",
                    user_home=root / "home",
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
            self.assertTrue(link.is_symlink())

    def test_link_or_junction_inside_marked_sandbox_blocks_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples = root / "samples"
            samples.mkdir()
            sandbox, _ = experiment.prepare_sandbox(
                samples,
                root / "experiments" / "run",
                project_root=root / "project",
                user_home=root / "home",
            )
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            link = sandbox / "outside-link"
            if os.name == "nt":
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                )
                if result.returncode != 0:
                    self.skipTest(f"junction is unavailable: {result.stderr or result.stdout}")
            else:
                link.symlink_to(outside, target_is_directory=True)

            try:
                with self.assertRaisesRegex(ValueError, "link|junction|sandbox"):
                    experiment.prepare_sandbox(
                        samples,
                        sandbox,
                        project_root=root / "project",
                        user_home=root / "home",
                    )
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
                self.assertTrue(sandbox.exists())
            finally:
                if link.exists() or link.is_symlink():
                    if link.is_symlink():
                        link.unlink()
                    else:
                        os.rmdir(link)

    def test_filesystem_root_is_rejected_without_writes(self) -> None:
        root = Path(Path.cwd().anchor)
        before = os.stat(root)

        with self.assertRaisesRegex(ValueError, "root|sandbox"):
            experiment.prepare_sandbox(Path.cwd(), root)

        after = os.stat(root)
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))


if __name__ == "__main__":
    unittest.main()
