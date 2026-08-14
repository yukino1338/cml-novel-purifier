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
import preprocess  # noqa: E402


class FailingBinaryFile:
    def __init__(self, file: object, boundary: str) -> None:
        self.file = file
        self.boundary = boundary

    def __enter__(self) -> "FailingBinaryFile":
        self.file.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self.file.__exit__(*args)

    def write(self, data: bytes) -> int:
        if self.boundary == "write":
            raise OSError("injected write failure")
        return self.file.write(data)

    def flush(self) -> None:
        if self.boundary == "flush":
            raise OSError("injected flush failure")
        self.file.flush()

    def fileno(self) -> int:
        return self.file.fileno()


class AtomicWriteF1Tests(unittest.TestCase):
    def assert_no_temp_files(self, path: Path) -> None:
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def assert_preserves_old_bytes(self, path: Path, call: object) -> None:
        before = path.read_bytes()
        with self.assertRaises(OSError):
            call()
        self.assertEqual(path.read_bytes(), before)
        self.assert_no_temp_files(path)

    def test_atomic_writer_preserves_existing_text_at_every_boundary(self) -> None:
        for boundary in ("open", "write", "flush", "fsync", "replace"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "document.txt"
                path.write_text("旧正文\n", encoding="utf-8")

                if boundary == "open":
                    patch = mock.patch(
                        "common.tempfile.mkstemp",
                        side_effect=OSError("injected open failure"),
                    )
                elif boundary in {"write", "flush"}:
                    real_fdopen = common.os.fdopen

                    def fail_fdopen(fd: int, *args: object, **kwargs: object) -> FailingBinaryFile:
                        return FailingBinaryFile(real_fdopen(fd, *args, **kwargs), boundary)

                    patch = mock.patch.object(common.os, "fdopen", side_effect=fail_fdopen)
                elif boundary == "fsync":
                    patch = mock.patch.object(
                        common.os,
                        "fsync",
                        side_effect=OSError("injected fsync failure"),
                    )
                else:
                    patch = mock.patch.object(
                        common.os,
                        "replace",
                        side_effect=OSError("injected replace failure"),
                    )

                with patch:
                    self.assert_preserves_old_bytes(path, lambda: common.write_utf8(path, "新正文\n"))

    def test_all_common_serializers_preserve_old_bytes_when_replace_fails(self) -> None:
        cases = {
            "text": lambda path: common.write_utf8(path, "新正文\n"),
            "json": lambda path: common.write_json(path, {"status": "new"}),
            "jsonl": lambda path: common.write_jsonl(path, [{"status": "new"}]),
            "append-jsonl": lambda path: common.append_jsonl(path, {"status": "new"}),
        }
        for name, write in cases.items():
            with self.subTest(writer=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "artifact.jsonl"
                path.write_bytes(b'{"status":"old"}\n')
                with mock.patch.object(
                    common.os,
                    "replace",
                    side_effect=OSError("injected replace failure"),
                ):
                    self.assert_preserves_old_bytes(path, lambda: write(path))

    def test_atomic_jsonl_append_keeps_existing_records_and_adds_one_complete_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operations.jsonl"
            path.write_bytes(b'{"event":"old"}\n')

            common.append_jsonl(path, {"event": "new"})

            self.assertEqual(
                [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()],
                [{"event": "old"}, {"event": "new"}],
            )
            self.assert_no_temp_files(path)

    def test_temporary_file_is_created_in_the_target_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "artifact.txt"
            observed: list[Path] = []
            real_mkstemp = common.tempfile.mkstemp

            def capture_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
                observed.append(Path(kwargs["dir"]))
                return real_mkstemp(*args, **kwargs)

            with mock.patch.object(common.tempfile, "mkstemp", side_effect=capture_mkstemp):
                common.write_utf8(path, "新正文\n")

            self.assertEqual(observed, [path.parent])
            self.assertEqual(path.read_text(encoding="utf-8"), "新正文\n")
            self.assert_no_temp_files(path)

    def test_posix_parent_directory_is_synced_after_replace(self) -> None:
        path = Path("C:/temporary") / "artifact.txt"
        with (
            mock.patch.object(common.os, "name", "posix"),
            mock.patch.object(common.os, "open", return_value=37) as open_dir,
            mock.patch.object(common.os, "fsync") as fsync,
            mock.patch.object(common.os, "close") as close,
        ):
            common._fsync_parent_directory(path)

        open_dir.assert_called_once_with(path.parent, common.os.O_RDONLY)
        fsync.assert_called_once_with(37)
        close.assert_called_once_with(37)

    def test_json_and_jsonl_serialization_failures_preserve_old_bytes(self) -> None:
        cases = {
            "json": lambda path: common.write_json(path, {"invalid": {"not-json"}}),
            "jsonl": lambda path: common.write_jsonl(
                path,
                [{"event": "valid"}, {"invalid": {"not-json"}}],
            ),
        }
        for name, write in cases.items():
            with self.subTest(writer=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "artifact.json"
                path.write_bytes(b'{"status":"old"}\n')
                before = path.read_bytes()

                with self.assertRaises(TypeError):
                    write(path)

                self.assertEqual(path.read_bytes(), before)
                self.assert_no_temp_files(path)

    def test_manifest_replace_failure_keeps_pending_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_text("第一章 示例\n正文甲。\n", encoding="utf-8")
            workspace = root / "sample-a.txt.cleanwork"
            common.init_workspace_from_source(source, workspace)
            manifest_path = workspace / "manifest.json"
            before = manifest_path.read_bytes()
            real_replace = common.os.replace

            def fail_manifest_replace(temp: object, target: object) -> None:
                if Path(target).name == "manifest.json":
                    raise OSError("injected manifest replace failure")
                real_replace(temp, target)

            with mock.patch.object(common.os, "replace", side_effect=fail_manifest_replace):
                with self.assertRaises(OSError):
                    preprocess.run(source, str(workspace))

            self.assertEqual(manifest_path.read_bytes(), before)
            manifest = json.loads(before.decode("utf-8"))
            self.assertEqual(manifest["stages"]["0_preprocess"]["status"], "pending")
            self.assertFalse((workspace / "versions/v1_preprocessed.txt").exists())
            self.assertFalse((workspace / "report/preprocess_report.json").exists())
            self.assert_no_temp_files(manifest_path)

    def test_output_replace_failure_never_marks_preprocess_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-a.txt"
            source.write_text("第一章 示例\n正文甲。\n", encoding="utf-8")
            workspace = root / "sample-a.txt.cleanwork"
            common.init_workspace_from_source(source, workspace)
            v1 = workspace / "versions/v1_preprocessed.txt"
            real_replace = common.os.replace

            def fail_v1_replace(temp: object, target: object) -> None:
                if Path(target) == v1:
                    raise OSError("injected v1 replace failure")
                real_replace(temp, target)

            with mock.patch.object(common.os, "replace", side_effect=fail_v1_replace):
                with self.assertRaises(OSError):
                    preprocess.run(source, str(workspace))

            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stages"]["0_preprocess"]["status"], "pending")
            self.assertFalse(v1.exists())
            self.assertFalse((workspace / "report/preprocess_report.json").exists())
            self.assert_no_temp_files(v1)


if __name__ == "__main__":
    unittest.main()
