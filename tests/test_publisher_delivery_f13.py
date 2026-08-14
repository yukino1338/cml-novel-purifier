from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import init_job_root  # noqa: E402
import preprocess  # noqa: E402
import publish_result  # noqa: E402
import scan_titles  # noqa: E402
from support_attestation import bind_passed_attestation  # noqa: E402


class JobRootResolutionTests(unittest.TestCase):
    def test_init_job_root_creates_only_the_documented_empty_areas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "我的清洗任务"

            result = init_job_root.run(root)

            self.assertEqual(result["schema"], "cml.job-root.v1")
            self.assertEqual(Path(result["job_root"]), root.resolve())
            self.assertEqual(
                set(path.relative_to(root).as_posix() for path in root.rglob("*")),
                {
                    "待清洗_Input",
                    "小说清洗结果_Novel-Purifier",
                    ".cml-novel-purifier",
                    ".cml-novel-purifier/workspaces",
                },
            )
            self.assertTrue(Path(result["input_dir"]).is_dir())
            self.assertTrue(Path(result["result_dir"]).is_dir())
            self.assertTrue(Path(result["workspace_root"]).is_dir())

    def test_workspace_resolution_priority_is_explicit_legacy_job_then_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root / "job"
            init_job_root.run(job)
            source = job / "待清洗_Input" / "長篇 测试.txt"
            source.write_text("第一章\n正文\n", encoding="utf-8")

            explicit = root / "explicit.cleanwork"
            self.assertEqual(
                common.workspace_for_source(source, str(explicit)),
                explicit.resolve(),
            )

            selected = common.workspace_for_source(source)
            self.assertEqual(selected.parent, (job / ".cml-novel-purifier/workspaces").resolve())
            self.assertTrue(selected.name.endswith(".cleanwork"))
            self.assertIn("長篇 测试.txt--", selected.name)
            self.assertIn(common.source_delivery_id(source), selected.name)
            self.assertEqual(common.portable_path_segment("CON.txt"), "_CON.txt")
            self.assertEqual(common.portable_path_segment("长篇:测试?.txt"), "长篇_测试_.txt")

            legacy = source.with_name(source.name + ".cleanwork")
            preprocess.run(source, str(legacy), encoding="utf-8")
            self.assertEqual(common.workspace_for_source(source), legacy.resolve())
            self.assertEqual(
                publish_result.default_delivery_root(common.load_manifest(legacy)),
                (job / "小说清洗结果_Novel-Purifier").resolve(),
            )

            standalone = root / "standalone.txt"
            standalone.write_text("正文\n", encoding="utf-8")
            hidden = common.workspace_for_source(standalone)
            self.assertEqual(
                hidden.parent,
                (root / ".cml-novel-purifier/workspaces").resolve(),
            )

    def test_new_public_root_artifacts_require_an_external_workspace_or_job_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            public_root = Path(directory) / "installed-skill"
            public_root.mkdir()
            source = public_root / "novel.txt"
            source.write_text("正文\n", encoding="utf-8")
            external = Path(directory) / "external" / "novel.cleanwork"

            with mock.patch.object(common, "SKILL_PUBLIC_ROOT", public_root.resolve()):
                with self.assertRaisesRegex(ValueError, "Skill public root"):
                    common.workspace_for_source(source)
                self.assertEqual(
                    common.workspace_for_source(source, str(external)),
                    external.resolve(),
                )

    def test_existing_empty_public_workspace_cannot_bypass_the_public_root_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_root = root / "installed-skill"
            public_root.mkdir()
            empty_workspace = public_root / "existing-empty.cleanwork"
            empty_workspace.mkdir()
            source = root / "outside.txt"
            source.write_text("第一章 起点\n人物甲继续前行。\n", encoding="utf-8")

            with mock.patch.object(common, "SKILL_PUBLIC_ROOT", public_root.resolve()):
                with self.assertRaisesRegex(common.WorkspacePathError, "Skill public root"):
                    common.workspace_for_source(source, str(empty_workspace))

            self.assertEqual(list(empty_workspace.iterdir()), [])

    def test_public_root_accepts_only_the_matching_legacy_workspace(self) -> None:
        """A complete but arbitrarily named workspace is still a new public artifact."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_root = root / "installed-skill"
            public_root.mkdir()
            source = root / "outside.txt"
            source.write_text("chapter one\nbody\n", encoding="utf-8")
            non_legacy = public_root / "arbitrary.cleanwork"
            preprocess.run(source, str(non_legacy), encoding="utf-8")

            with mock.patch.object(common, "SKILL_PUBLIC_ROOT", public_root.resolve()):
                with self.assertRaisesRegex(common.WorkspacePathError, "Skill public root"):
                    common.workspace_for_source(source, str(non_legacy))

    def test_named_input_folder_never_nests_workspace_or_results_when_job_areas_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / common.JOB_INPUT_DIR_NAME
            input_dir.mkdir()
            source = input_dir / "待处理.txt"
            source.write_text("第一章 起点\n人物甲继续前行。\n", encoding="utf-8")

            workspace = common.workspace_for_source(source)
            self.assertEqual(
                workspace.parent,
                (root / common.JOB_INTERNAL_DIR_NAME / common.JOB_WORKSPACES_DIR_NAME).resolve(),
            )
            manifest = {
                "source": {
                    "path": str(source.resolve()),
                    "name": source.name,
                    "sha256": common.sha256_file(source),
                    "size_bytes": source.stat().st_size,
                }
            }
            self.assertEqual(
                publish_result.default_delivery_root(manifest),
                (root / common.JOB_RESULT_DIR_NAME).resolve(),
            )

    def test_init_rejects_a_reserved_area_as_the_job_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / common.JOB_INPUT_DIR_NAME
            with self.assertRaisesRegex(ValueError, "reserved job area"):
                init_job_root.run(root)


class PublisherTests(unittest.TestCase):
    def make_workspace(self, root: Path, name: str = "匿名小说.txt") -> tuple[Path, Path]:
        source = root / name
        source.write_text("第一章 起点\n人物甲继续前行。\n", encoding="utf-8")
        workspace = preprocess.run(
            source,
            str(root / "workspaces" / f"{name}.cleanwork"),
            encoding="utf-8",
        )
        common.write_json(workspace / "meta/book_profile.json", {})
        bind_passed_attestation(workspace)
        return source, workspace

    @staticmethod
    def load_result(report: dict[str, object]) -> dict[str, object]:
        return json.loads(Path(str(report["result"])).read_text(encoding="utf-8"))

    def test_completed_default_is_one_atomic_txt_delivery_with_fixed_receipt_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, workspace = self.make_workspace(root)
            delivery_root = root / "小说清洗结果_Novel-Purifier"
            source_before = source.read_bytes()
            v0_before = (workspace / "versions/v0_original.txt").read_bytes()

            report = publish_result.run(workspace, delivery_root=delivery_root)

            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["exit_code"], 0)
            self.assertEqual(report["formats"], ["txt"])
            for key in ("review", "primary_output", "delivery_dir", "result"):
                self.assertTrue(Path(str(report[key])).is_absolute())
            delivery_dir = Path(str(report["delivery_dir"]))
            self.assertEqual(
                {item.name for item in delivery_dir.iterdir()},
                {
                    "01_查看结果_Review.html",
                    "02_清洗后_Cleaned.txt",
                    "03_处理摘要_Result.json",
                },
            )
            self.assertEqual(
                Path(str(report["primary_output"])).read_bytes(),
                (workspace / common.load_manifest(workspace)["current_head"]).read_bytes(),
            )
            review_html = Path(str(report["review"])).read_text(encoding="utf-8")
            self.assertIn("高级审计：内部日志路径", review_html)
            self.assertIn('id="cml-delivery-binding"', review_html)
            self.assertIn(result_id := common.source_delivery_id(source), review_html)
            self.assertIn(json.loads(Path(str(report["result"])).read_text(encoding="utf-8"))["delivery_id"], review_html)
            self.assertIn(str(workspace / "logs/operations.jsonl"), review_html)
            result = self.load_result(report)
            self.assertEqual(result["schema"], "cml.result.v1")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["source_id"], latest_source_id := result_id)
            self.assertEqual(result["delivery"]["source_id"], latest_source_id)
            self.assertEqual(result["delivery"]["delivery_id"], result["delivery_id"])
            self.assertEqual(result["delivery"]["produced_formats"], ["txt"])
            self.assertEqual(
                result["delivery"]["artifacts"]["outputs"]["txt"]["sha256"],
                common.sha256_file(Path(str(report["primary_output"]))),
            )
            self.assertEqual(
                result["delivery"]["artifacts"]["review"]["sha256"],
                common.sha256_file(Path(str(report["review"]))),
            )
            self.assertEqual(
                result["delivery"]["artifacts"]["review"]["delivery_id"],
                result["delivery_id"],
            )
            self.assertEqual(
                result["delivery"]["artifacts"]["outputs"]["txt"]["source_id"],
                latest_source_id,
            )
            self.assertTrue(result["source"]["source_matches_v0"])
            self.assertTrue(result["source"]["v0_unchanged"])
            self.assertNotIn("context", json.dumps(result, ensure_ascii=False).lower())
            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual((workspace / "versions/v0_original.txt").read_bytes(), v0_before)

            latest_path = delivery_dir.parent / "latest.json"
            start_path = delivery_dir.parent / "00_从这里开始_Start-Here.html"
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            self.assertEqual(latest["schema"], "cml.latest.v1")
            self.assertEqual(latest["latest_attempt"]["status"], "completed")
            self.assertEqual(latest["latest_success"], latest["latest_attempt"])
            start_html = start_path.read_text(encoding="utf-8")
            self.assertIn(str(Path(str(report["review"]))), start_html)
            self.assertIn("打开复核页", start_html)
            self.assertIn("打开清洗后文件", start_html)
            self.assertIn("打开结果目录", start_html)
            self.assertNotIn(f'href="{report["review"]}"', start_html)
            self.assertTrue(latest["latest_attempt"]["links"]["review"])
            self.assertEqual(latest["latest_attempt"]["source_id"], latest_source_id)
            self.assertEqual(
                latest["latest_attempt"]["artifacts"]["result"]["sha256"],
                common.sha256_file(Path(str(report["result"]))),
            )
            self.assertIn(latest["latest_attempt"]["delivery_id"], start_html)
            self.assertIn(latest["latest_attempt"]["created_at"], start_html)
            self.assertIn("与最新尝试相同", start_html)

    def test_blocked_publishes_review_and_result_without_reading_file_and_preserves_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.make_workspace(root)
            delivery_root = root / "results"
            completed = publish_result.run(workspace, delivery_root=delivery_root)
            completed_dir = Path(str(completed["delivery_dir"]))
            manifest = common.load_manifest(workspace)
            manifest["stages"]["6_verify"] = {
                "status": "blocked",
                "blocked_reason": "residual_candidates",
            }
            common.save_manifest(workspace, manifest)

            blocked = publish_result.run(workspace, delivery_root=delivery_root)

            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["exit_code"], 2)
            self.assertIsNone(blocked["primary_output"])
            blocked_dir = Path(str(blocked["delivery_dir"]))
            self.assertNotEqual(blocked_dir, completed_dir)
            self.assertEqual(
                {item.name for item in blocked_dir.iterdir()},
                {"01_查看结果_Review.html", "03_处理摘要_Result.json"},
            )
            latest = json.loads((blocked_dir.parent / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["latest_attempt"]["status"], "blocked")
            self.assertEqual(
                Path(latest["latest_success"]["delivery_dir"]),
                completed_dir,
            )
            result = self.load_result(blocked)
            self.assertTrue(result["blockers"])
            self.assertEqual(len(result["next_actions"]), 1)

    def test_tampered_old_success_is_not_republished_as_a_valid_recent_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.make_workspace(root)
            delivery_root = root / "results"
            first = publish_result.run(workspace, delivery_root=delivery_root)
            old_output = Path(str(first["primary_output"]))
            old_output.write_text("tampered old delivery", encoding="utf-8")
            book_root = Path(str(first["delivery_dir"])).parent
            latest_before = (book_root / publish_result.LATEST_NAME).read_bytes()
            directories_before = {item.name for item in book_root.iterdir() if item.is_dir()}
            manifest = common.load_manifest(workspace)
            manifest["stages"]["6_verify"] = {
                "status": "blocked",
                "blocked_reason": "new review required",
            }
            common.save_manifest(workspace, manifest)

            with self.assertRaisesRegex(ValueError, "integrity"):
                publish_result.run(workspace, delivery_root=delivery_root)

            self.assertEqual((book_root / publish_result.LATEST_NAME).read_bytes(), latest_before)
            self.assertEqual(
                {item.name for item in book_root.iterdir() if item.is_dir()},
                directories_before,
            )

    def test_unsafe_or_unbound_latest_links_are_rejected_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.make_workspace(root)
            first = publish_result.run(workspace, delivery_root=root / "results")
            book_root = Path(str(first["delivery_dir"])).parent
            latest_path = book_root / publish_result.LATEST_NAME
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            latest["latest_success"]["links"]["review"] = "javascript:alert(1)"
            common.write_json(latest_path, latest)

            with self.assertRaisesRegex(ValueError, "latest.*link"):
                publish_result.run(workspace, delivery_root=root / "results")

    def test_latest_rejects_cross_source_and_result_digest_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.make_workspace(root)
            first = publish_result.run(workspace, delivery_root=root / "results")
            book_root = Path(str(first["delivery_dir"])).parent
            latest_path = book_root / publish_result.LATEST_NAME
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            latest["latest_success"]["source_id"] = "different-source"
            common.write_json(latest_path, latest)

            with self.assertRaisesRegex(ValueError, "source"):
                publish_result.run(workspace, delivery_root=root / "results")

            latest["latest_success"]["source_id"] = common.source_delivery_id(
                Path(common.load_manifest(workspace)["source"]["path"])
            )
            result_path = Path(str(first["result"]))
            result_path.write_text('{"schema":"forged"}', encoding="utf-8")
            common.write_json(latest_path, latest)
            with self.assertRaisesRegex(ValueError, "integrity"):
                publish_result.run(workspace, delivery_root=root / "results")

    def test_attestation_rejection_cannot_publish_contradictory_passed_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.make_workspace(root)
            manifest = common.load_manifest(workspace)
            manifest["stages"]["6_verify"]["attestation"]["rule_version"] = "stale"
            common.save_manifest(workspace, manifest)

            report = publish_result.run(workspace, delivery_root=root / "results")
            result = self.load_result(report)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["verification"]["declared_status"], "passed")
            self.assertEqual(result["verification"]["publisher_gate_status"], "blocked")
            self.assertEqual(result["verification"]["status"], "blocked")
            self.assertEqual(result["blockers"][0]["code"], "export_attestation_rejected")

    def test_required_review_render_failure_is_not_reported_as_a_reliable_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.make_workspace(root)
            manifest = common.load_manifest(workspace)
            manifest["stages"]["6_verify"] = {"status": "pending"}
            common.save_manifest(workspace, manifest)

            with (
                mock.patch.object(
                    publish_result.build_review_html,
                    "workspace_review",
                    side_effect=ValueError("injected review failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "interactive review"),
            ):
                publish_result.run(workspace, delivery_root=root / "results")

            self.assertFalse((root / "results").exists())

    def test_requested_formats_are_fixed_named_and_unrequested_epub_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.make_workspace(root)
            with mock.patch.object(
                publish_result.export_outputs,
                "write_epub",
                side_effect=AssertionError("unrequested EPUB path was reached"),
            ):
                report = publish_result.run(
                    workspace,
                    delivery_root=root / "results",
                    requested_formats=("markdown", "txt"),
                )
            self.assertEqual(report["formats"], ["txt", "markdown"])
            names = {item.name for item in Path(str(report["delivery_dir"])).iterdir()}
            self.assertIn("02_清洗后_Cleaned.txt", names)
            self.assertIn("02_清洗后_Cleaned.md", names)
            self.assertNotIn("02_清洗后_Cleaned.epub", names)

    def test_failure_while_updating_latest_rolls_back_bundle_and_old_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.make_workspace(root)
            delivery_root = root / "results"
            first = publish_result.run(workspace, delivery_root=delivery_root)
            book_root = Path(str(first["delivery_dir"])).parent
            before_latest = (book_root / "latest.json").read_bytes()
            before_start = (book_root / "00_从这里开始_Start-Here.html").read_bytes()
            before_dirs = {item.name for item in book_root.iterdir() if item.is_dir()}
            real_replace = os.replace
            latest_target = (book_root / "latest.json").resolve()
            failed = False

            def fail_latest(source: object, target: object) -> None:
                nonlocal failed
                if Path(target).resolve() == latest_target and not failed:
                    failed = True
                    raise OSError("injected latest publish failure")
                real_replace(source, target)

            with mock.patch.object(common.os, "replace", side_effect=fail_latest):
                with self.assertRaisesRegex(OSError, "injected latest"):
                    publish_result.run(workspace, delivery_root=delivery_root)

            self.assertEqual((book_root / "latest.json").read_bytes(), before_latest)
            self.assertEqual(
                (book_root / "00_从这里开始_Start-Here.html").read_bytes(),
                before_start,
            )
            self.assertEqual(
                {item.name for item in book_root.iterdir() if item.is_dir()},
                before_dirs,
            )

    def test_report_only_is_a_published_terminal_without_cleaned_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "仅检查标题.txt"
            source.write_text("第一章 起点\n正文。\n", encoding="utf-8")
            workspace = preprocess.run(source, str(root / "report.cleanwork"), "utf-8")
            scan_titles.run(workspace, "auto", "candidates/titles.jsonl")

            report = publish_result.run(workspace, delivery_root=root / "results")

            self.assertEqual(report["status"], "report_only")
            self.assertEqual(report["exit_code"], 2)
            self.assertIsNone(report["primary_output"])
            self.assertEqual(report["formats"], [])
            result = self.load_result(report)
            self.assertEqual(result["delivery"]["requested_formats"], [])
            self.assertEqual(result["delivery"]["produced_formats"], [])
            self.assertEqual(result["next_actions"], [])

    def test_preprocess_blocker_receipt_has_plain_reason_and_one_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.make_workspace(root)
            manifest = common.load_manifest(workspace)
            manifest["stages"]["0_preprocess"] = {
                "status": "blocked",
                "blocked_reason": "low_text_quality",
            }
            common.save_manifest(workspace, manifest)

            receipt = publish_result.run(workspace, delivery_root=root / "results")

            self.assertEqual(receipt["status"], "blocked")
            self.assertEqual(receipt["reason"]["code"], "preprocess_blocked")
            self.assertIn("编码", receipt["reason"]["message"])
            self.assertIn("preprocess.py", receipt["next_action"])
            self.assertNotEqual(receipt["next_action"], "先处理：low_text_quality")

    def test_portable_book_roots_do_not_collide_for_case_unicode_or_long_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_root = root / "results"
            names = ["Book.txt", "book.txt", ("长" * 50) + ".txt"]
            book_roots: list[Path] = []
            for index, name in enumerate(names):
                source_root = root / f"source-{index}"
                source_root.mkdir()
                _, workspace = self.make_workspace(source_root, name)
                report = publish_result.run(workspace, delivery_root=delivery_root)
                book_roots.append(Path(str(report["delivery_dir"])).parent)
            keys = {path.name.casefold() for path in book_roots}
            self.assertEqual(len(keys), len(book_roots))
            self.assertTrue(all(len(path.name.encode("utf-8")) <= 96 for path in book_roots))

    def test_real_cli_publishes_noncomplete_bundle_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace = self.make_workspace(root)
            manifest = common.load_manifest(workspace)
            manifest["stages"]["6_verify"] = {
                "status": "incomplete",
                "blocked_reason": "residual_scan_skipped",
            }
            common.save_manifest(workspace, manifest)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/publish_result.py"),
                    str(workspace),
                    "--delivery-root",
                    str(root / "results"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            receipt = json.loads(completed.stdout)
            self.assertEqual(receipt["status"], "incomplete")
            self.assertTrue(Path(receipt["review"]).is_file())
            self.assertTrue(Path(receipt["result"]).is_file())
            self.assertIsNone(receipt["primary_output"])
            self.assertEqual(receipt["reason"]["code"], "verification_incomplete")
            self.assertIn("完整 verify", receipt["next_action"])

    def test_cli_stdout_is_small_terminal_json_and_exit_codes_are_fixed(self) -> None:
        values = [
            (
                {
                    "status": "completed",
                    "exit_code": 0,
                    "review": "C:\\小说清洗结果_Novel-Purifier\\复核页.html",
                },
                0,
            ),
            ({"status": "needs_review", "exit_code": 2}, 2),
        ]
        for terminal, expected in values:
            with self.subTest(status=terminal["status"]):
                output = io.StringIO()
                argv = ["publish_result.py", "book.cleanwork", "--delivery-root", "out"]
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(publish_result, "run", return_value=terminal),
                    redirect_stdout(output),
                    self.assertRaises(SystemExit) as raised,
                ):
                    publish_result.main()
                self.assertEqual(raised.exception.code, expected)
                payload = json.loads(output.getvalue())
                self.assertEqual(payload, terminal)
                self.assertLess(len(output.getvalue()), 4096)
                self.assertTrue(output.getvalue().isascii())

        output = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["publish_result.py", "book.cleanwork", "--delivery-root", "out"],
            ),
            mock.patch.object(publish_result, "run", side_effect=ValueError("bad publisher")),
            redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            publish_result.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "publisher_failed")

    def test_skill_contract_requires_publisher_and_the_fixed_proactive_receipt(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        agent = yaml.safe_load(
            (ROOT / "agents/openai.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(set(agent), {"interface"})
        self.assertEqual(
            set(agent["interface"]),
            {"display_name", "short_description", "default_prompt"},
        )
        agent_prompt = agent["interface"]["default_prompt"]
        for text in (
            "任何终态",
            "python scripts/publish_result.py",
            "状态：completed / needs_review / blocked / incomplete / report_only",
            "复核页：<absolute review path>",
            "清洗后文件：<absolute primary path 或“未生成”>",
            "结果目录：<absolute delivery path>",
            "实际格式：<txt / markdown / epub / 无>",
            "原文与 v0：<unchanged / mismatch>",
            "增加 Markdown 或 EPUB 时仍传 --format txt",
            "只要 Markdown",
        ):
            self.assertIn(text, skill)
        self.assertIn("实际格式", agent_prompt)
        self.assertIn("source/v0", agent_prompt)


if __name__ == "__main__":
    unittest.main()
