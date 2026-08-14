from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import export_outputs  # noqa: E402
import rollback  # noqa: E402
import scan_identity  # noqa: E402


class FixedDatetime:
    @classmethod
    def now(cls):
        return cls()

    def strftime(self, value: str) -> str:
        return {
            "%Y%m%d": "20260715",
            "%Y%m%d-%H%M": "20260715-1234",
            "%Y%m%d-%H%M%S": "20260715-123456",
        }[value]


def export_config(title: str = "") -> dict[str, object]:
    return {
        "export": {
            "title": title,
            "author": "",
            "language": "zh-CN",
        }
    }


class ExportCoverageF7Tests(unittest.TestCase):
    def attestation_bundle(self) -> tuple[dict[str, object], dict[str, object]]:
        current_sha = "a" * 64
        parent_sha = "b" * 64
        decision_sha = "c" * 64
        run_id = "d" * 32
        checks = [
            {"name": name, "passed": True}
            for name in sorted(export_outputs.REQUIRED_VERIFY_CHECKS)
        ]
        runtime_identity = {
            "scan_rule_pack_sha256": scan_identity.canonical_json_sha256(
                scan_identity.build_scan_rule_pack("ads")
            ),
            "draft_rule_pack_sha256": scan_identity.canonical_json_sha256(
                scan_identity.build_draft_rule_pack()
            ),
            "profile": "meta/book_profile.json",
            "profile_present": False,
            "book_profile_sha256": scan_identity.canonical_json_sha256({}),
            "book_profile_file_sha256": None,
        }
        attestation: dict[str, object] = {
            "schema_version": 3,
            "rule_version": export_outputs.VERIFY_RULE_VERSION,
            "status": "passed",
            "checks": checks,
            "verification_run_id": run_id,
            "current_head": "versions/current.txt",
            "current_head_sha256": current_sha,
            "decision_sha256": decision_sha,
            "apply_output": "versions/applied.txt",
            "apply_output_sha256": "e" * 64,
            "layout_run_id": "f" * 32,
            "layout_config_sha256": "1" * 64,
            "parent_path": "versions/applied.txt",
            "parent_sha256": parent_sha,
            **runtime_identity,
        }
        manifest: dict[str, object] = {
            "current_head": "versions/current.txt",
            "source": {"sha256": "2" * 64},
            "stages": {
                "6_verify": {
                    "status": "passed",
                    "run_id": run_id,
                    "warnings": [],
                    "decision_sha256": decision_sha,
                    "apply_output": "versions/applied.txt",
                    "apply_output_sha256": "e" * 64,
                    "layout_run_id": "f" * 32,
                    "layout_config_sha256": "1" * 64,
                    "report": "report/verify.json",
                    "attestation": attestation,
                    **runtime_identity,
                }
            },
            "artifacts": {
                "versions/current.txt": {
                    "parent_path": "versions/applied.txt",
                    "parent_sha256": parent_sha,
                },
                "report/verify.json": {"sha256": "3" * 64},
            },
        }
        return manifest, attestation

    def require_attestation(
        self,
        manifest: dict[str, object],
        attestation: dict[str, object],
        *,
        report_override: object | None = None,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            input_path = workspace / "versions" / "current.txt"
            input_path.parent.mkdir()
            input_path.write_text("正文", encoding="utf-8")
            report_path = workspace / "report.json"
            report = (
                {"status": "passed", "attestation": attestation}
                if report_override is None
                else report_override
            )
            report_path.write_text(json.dumps(report), encoding="utf-8")

            def resolve(_workspace: Path, value: str, **_kwargs: object) -> Path:
                if value == "report/verify.json":
                    return report_path
                if value == "meta/book_profile.json":
                    return workspace / "meta/book_profile.json"
                raise AssertionError(value)

            with (
                mock.patch.object(export_outputs, "load_manifest", return_value=manifest),
                mock.patch.object(export_outputs, "sha256_file", return_value="a" * 64),
                mock.patch.object(export_outputs, "resolve_in_workspace", side_effect=resolve),
            ):
                return export_outputs.require_export_attestation(workspace, input_path)

    def test_attestation_accepts_exact_binding_and_rejects_each_public_gate(self) -> None:
        manifest, attestation = self.attestation_bundle()
        verified = self.require_attestation(manifest, attestation)
        self.assertEqual(verified["verification_run_id"], "d" * 32)
        self.assertEqual(verified["source_sha256"], "2" * 64)

        def reject(mutator, message: str, report_override: object | None = None) -> None:
            bad_manifest, bad_attestation = self.attestation_bundle()
            mutator(bad_manifest, bad_attestation)
            with self.assertRaisesRegex(ValueError, message):
                self.require_attestation(
                    bad_manifest,
                    bad_attestation,
                    report_override=report_override,
                )

        reject(lambda m, _a: m.update(current_head="versions/other.txt"), "current_head")
        reject(lambda m, _a: m["stages"].update({"6_verify": {}}), "passed verification")  # type: ignore[union-attr]
        reject(lambda _m, a: a.update(schema_version=1), "missing or invalid")
        reject(lambda _m, a: a.update(checks=[]), "incomplete or blocking")
        reject(lambda m, _a: m["stages"]["6_verify"].update(warnings=["warning"]), "warnings")  # type: ignore[index]
        reject(lambda _m, a: a.update(current_head_sha256="stale"), "stale current_head_sha256")
        reject(lambda m, _a: m["artifacts"].pop("versions/current.txt"), "not a committed artifact")  # type: ignore[union-attr]
        reject(lambda _m, a: a.update(parent_sha256="stale"), "stale parent_sha256")
        reject(lambda m, _a: m["artifacts"].pop("report/verify.json"), "report is not a committed")  # type: ignore[union-attr]
        reject(lambda _m, _a: None, "does not match", report_override={"status": "failed"})

    def test_names_paths_profiles_and_identity_cover_all_decisions(self) -> None:
        with self.assertRaises(ValueError):
            export_outputs._truncate_utf8("x", 0)
        self.assertEqual(export_outputs._truncate_utf8("abc", 3), "abc")
        self.assertEqual(export_outputs._truncate_utf8("中文", 4), "中")
        self.assertEqual(export_outputs.safe_name(" CON "), "_CON")
        self.assertEqual(export_outputs.safe_name("   "), "novel")
        self.assertNotIn("/", export_outputs.safe_name("a/b"))
        self.assertEqual(export_outputs.portable_name_key("Name. "), "name")
        self.assertFalse(export_outputs.is_close_name("", "book"))
        self.assertTrue(export_outputs.is_close_name("小说Book TXT", "book"))
        self.assertTrue(export_outputs.is_close_name("长标题一", "长标题一精校版"))
        self.assertFalse(export_outputs.is_close_name("甲", "乙"))
        self.assertTrue(
            export_outputs.protected_context(
                {"narrative_style": "同人叙事"}, "book"
            )
        )
        self.assertFalse(export_outputs.protected_context({}, "book"))
        self.assertTrue(export_outputs.title_in_text("作品标题", "开头作品标题正文"))
        self.assertFalse(export_outputs.title_in_text("短", "短"))
        self.assertFalse(export_outputs.character_support({}, "正文"))
        self.assertTrue(export_outputs.character_support({"main_characters": ["主角"]}, "主角登场"))

        cases = (
            ({}, export_config(), "clean", False),
            ({}, export_config("新标题"), "clean", True),
            (
                {
                    "title": "新标题",
                    "rename_verified": True,
                    "narrative_style": "同人叙事",
                },
                export_config(),
                "同人原名",
                False,
            ),
            ({"title": "原名小说", "rename_verified": True}, export_config(), "原名", False),
            ({"title": "新标题", "rename_verified": True}, export_config(), "下载book.txt", True),
            ({"title": "新标题", "rename_verified": True}, export_config(), "clean", True),
            ({"title": "新标题"}, export_config(), "clean", False),
        )
        for profile, config, source, renamed in cases:
            text = "新标题\n主角登场" if source == "clean" else "正文"
            with (
                self.subTest(source=source, profile=profile),
                mock.patch.object(export_outputs, "load_profile", return_value=profile),
                mock.patch.object(export_outputs, "source_stem", return_value=source),
            ):
                identity = export_outputs.resolve_export_identity(Path("workspace"), config, text)
            self.assertEqual(identity["rename_applied"], renamed)

    def test_source_stem_report_paths_and_output_directory_collision_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "fallback.txt.cleanwork"
            workspace.mkdir()
            with mock.patch.object(export_outputs, "load_manifest", return_value={}):
                self.assertEqual(export_outputs.source_stem(workspace), "fallback")
            with mock.patch.object(
                export_outputs,
                "load_manifest",
                return_value={"source": {"name": "source.name.txt"}},
            ):
                self.assertEqual(export_outputs.source_stem(workspace), "source.name")
            self.assertEqual(export_outputs.report_path(workspace / "a", workspace), "a")
            self.assertEqual(export_outputs.report_path(root, workspace), str(root))

            output = root / "output"
            with mock.patch.object(export_outputs, "datetime", FixedDatetime):
                self.assertEqual(
                    export_outputs.timestamped_output_dir(output, "Book").name,
                    "20260715-Book",
                )
                output.mkdir()
                (output / "20260715-existing").mkdir()
                self.assertEqual(
                    export_outputs.timestamped_output_dir(output, "Book").name,
                    "20260715-1234-Book",
                )
                (output / "20260715-1234-Book").mkdir()
                self.assertEqual(
                    export_outputs.timestamped_output_dir(output, "Book").name,
                    "20260715-123456-Book",
                )
                (output / "20260715-123456-Book").mkdir()
                (output / "20260715-123456-Book-2").mkdir()
                self.assertEqual(
                    export_outputs.timestamped_output_dir(output, "Book").name,
                    "20260715-123456-Book-3",
                )

            parent = root / "children"
            parent.mkdir()
            (parent / "Book").mkdir()
            self.assertEqual(export_outputs.unique_child_dir(parent, "Book").name, "Book-2")
            reserved = {export_outputs.portable_path_key(parent / "Book-2")}
            self.assertEqual(export_outputs.reserved_child_dir(parent, "Book", reserved).name, "Book-3")

    def write_test_epub(
        self,
        path: Path,
        *,
        include_required: bool = True,
        duplicate_manifest: bool = False,
        include_chapter: bool = True,
        body: bool = True,
        mimetype: bytes = b"application/epub+zip",
    ) -> None:
        item = '<item id="c1" href="Text/chapter-0001.xhtml" media-type="application/xhtml+xml"/>'
        manifest = (
            '<item id="nav" href="Text/nav.xhtml" '
            'media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="style" href="Styles/style.css" media-type="text/css"/>'
            + item
            + (item if duplicate_manifest else "")
        )
        opf = (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:language>zh-CN</dc:language></metadata>"
            f"<manifest>{manifest}</manifest><spine><itemref idref=\"c1\"/></spine></package>"
        )
        chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>正文</p></body></html>'
            if body
            else '<html xmlns="http://www.w3.org/1999/xhtml"><head/></html>'
        )
        chapter = chapter.replace(
            '<html xmlns="http://www.w3.org/1999/xhtml">',
            (
                '<html xmlns="http://www.w3.org/1999/xhtml" '
                'lang="zh-CN" xml:lang="zh-CN">'
            ),
            1,
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
            if include_required:
                archive.writestr(
                    "META-INF/container.xml",
                    (
                        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                        'media-type="application/oebps-package+xml"/></rootfiles></container>'
                    ),
                )
                archive.writestr("OEBPS/content.opf", opf)
                archive.writestr(
                    "OEBPS/Text/nav.xhtml",
                    (
                        '<html xmlns="http://www.w3.org/1999/xhtml" '
                        'lang="zh-CN" xml:lang="zh-CN"><body>'
                        '<nav xmlns:epub="http://www.idpf.org/2007/ops" '
                        'epub:type="toc"><a href="chapter-0001.xhtml">正文</a></nav>'
                        "</body></html>"
                    ),
                )
                archive.writestr("OEBPS/Styles/style.css", "")
            if include_chapter:
                archive.writestr("OEBPS/Text/chapter-0001.xhtml", chapter)

    def test_epub_validation_covers_each_package_and_semantic_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                ({"mimetype": b"bad"}, "mimetype"),
                ({"include_required": False}, "missing required"),
                ({"duplicate_manifest": True}, "manifest or spine"),
                ({"include_chapter": False}, "chapter files"),
                ({"body": False}, "no XHTML body"),
            )
            for index, (options, message) in enumerate(cases, 1):
                path = root / f"bad-{index}.epub"
                self.write_test_epub(path, **options)
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    export_outputs.validate_epub(path, "正文")
            valid = root / "valid.epub"
            self.write_test_epub(valid)
            self.assertTrue(export_outputs.validate_epub(valid, "正文")["passed"])
            with self.assertRaisesRegex(ValueError, "semantic text"):
                export_outputs.validate_epub(valid, "不同正文")
            corrupt = root / "corrupt.epub"
            corrupt.write_bytes(b"not a zip")
            with self.assertRaisesRegex(ValueError, "self-check"):
                export_outputs.validate_epub(corrupt, "正文")

    def test_markdown_xhtml_file_names_and_output_self_checks_cover_empty_and_failure(self) -> None:
        text = "作品说明\n第一章 开始\n正文"
        markdown = export_outputs.markdown_from_text(text)
        self.assertIn("## 第一章 开始", markdown)
        body = export_outputs.xhtml_for_chapter({"title": "A&B", "body": "", "kind": "body"})
        self.assertIn('class="source-body"', body)
        self.assertNotIn("<h1>", body)
        self.assertEqual(
            export_outputs.export_file_names("Book"),
            {"txt": "Book.txt", "markdown": "Book.md", "epub": "Book.epub"},
        )
        self.assertEqual(export_outputs.markdown_body_text("## Title\nBody"), "Title\nBody")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("正文", encoding="utf-8")
            plan = {
                "workspace": root,
                "input_path": source,
                "output_paths": {"txt": root / "final.txt", "markdown": root / "final.md"},
                "text": "正文",
                "title": "Book",
                "author": "A",
                "language": "zh-CN",
            }
            txt = root / "stage.txt"

            def wrong_copy(_source: Path, target: Path) -> None:
                target.write_text("wrong", encoding="utf-8")

            with (
                mock.patch.object(export_outputs.shutil, "copyfile", side_effect=wrong_copy),
                self.assertRaisesRegex(ValueError, "TXT export bytes"),
            ):
                export_outputs.write_export_outputs(plan, {"txt": txt})
            with (
                mock.patch.object(export_outputs, "markdown_from_text", return_value="wrong\n"),
                self.assertRaisesRegex(ValueError, "Markdown semantic"),
            ):
                export_outputs.write_export_outputs(plan, {"markdown": root / "stage.md"})

    def test_batch_guards_common_root_and_main_single_batch_exit(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            export_outputs.run_batch([], "auto", None)
        workspace = Path("C:/book.cleanwork")
        with (
            mock.patch.object(export_outputs, "validate_workspace", return_value=workspace),
            self.assertRaisesRegex(ValueError, "unique"),
        ):
            export_outputs.run_batch([workspace, workspace], "auto", None)
        root = export_outputs.common_output_root(
            [Path("C:/root/a.cleanwork"), Path("C:/root/sub/b.cleanwork")]
        )
        self.assertEqual(root.name, "output")

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "export_outputs.py",
                    "one.cleanwork",
                    "--format",
                    "epub",
                    "--format",
                    "txt",
                ],
            ),
            mock.patch.object(export_outputs, "run", return_value={"status": "passed"}) as run,
            mock.patch("builtins.print"),
        ):
            export_outputs.main()
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["requested_formats"], ("txt", "epub"))
        with (
            mock.patch.object(
                sys,
                "argv",
                ["export_outputs.py", "one.cleanwork", "--all-formats"],
            ),
            mock.patch.object(
                export_outputs,
                "run",
                return_value={"status": "passed"},
            ) as run_all,
            mock.patch("builtins.print"),
        ):
            export_outputs.main()
        self.assertEqual(
            run_all.call_args.kwargs["requested_formats"],
            export_outputs.ALL_FORMATS,
        )
        with (
            mock.patch.object(
                sys,
                "argv",
                ["export_outputs.py", "one.cleanwork", "two.cleanwork"],
            ),
            mock.patch.object(export_outputs, "run_batch", return_value={"status": "partial"}) as batch,
            mock.patch("builtins.print"),
            self.assertRaisesRegex(SystemExit, "1"),
        ):
            export_outputs.main()
        batch.assert_called_once()


class RollbackCoverageF7Tests(unittest.TestCase):
    def test_module_target_copy_and_chapter_helpers_cover_failures_and_boundaries(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported module"):
            rollback._module_paths("other")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            versions = workspace / "versions"
            versions.mkdir()
            self.assertEqual(
                rollback._validate_version_target(workspace, versions / "ok.txt"),
                "versions/ok.txt",
            )
            for target in (workspace / "bad.txt", versions / "bad.md"):
                with self.assertRaisesRegex(ValueError, "inside versions"):
                    rollback._validate_version_target(workspace, target)
            source = workspace / "source.txt"
            target = workspace / "target.txt"
            source.write_text("source", encoding="utf-8")
            with mock.patch.object(rollback, "sha256_file", side_effect=["a", "b"]):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    rollback._copy_and_validate(source, target)

        self.assertEqual(rollback.decision_chapter_indexes({"verdict": "keep"}), set())
        decision = {
            "verdict": "delete",
            "anchors": [{"anchor_id": "A", "chapter": {"index": 1}}],
        }
        self.assertEqual(rollback.decision_chapter_indexes(decision), {1})
        for anchor in ({}, {"chapter": {}}, {"chapter": {"index": True}}, {"chapter": {"index": 0}}):
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                rollback._chapter_index(anchor)

    def test_baseline_replay_stage_bindings_and_run_id_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "source.txt"
            decisions = workspace / "decisions.jsonl"
            output = workspace / "output.txt"
            anomalies = workspace / "anomalies.jsonl"
            for path, value in ((source, "source"), (decisions, "{}\n"), (output, "output")):
                path.write_text(value, encoding="utf-8")
            base_stage = {
                "status": "done",
                "input": "source.txt",
                "decisions": "decisions.jsonl",
                "output": "output.txt",
                "input_sha256": "sha-source",
                "decision_sha256": "sha-decisions",
                "output_sha256": "sha-output",
                "active_run_id": "run",
            }

            def call(stage: object, replay: str = "output"):
                with (
                    mock.patch.object(rollback, "collect_operations", return_value=[]),
                    mock.patch.object(rollback, "apply_operations", return_value=replay),
                    mock.patch.object(
                        rollback,
                        "sha256_file",
                        side_effect=lambda path: {
                            source: "sha-source",
                            decisions: "sha-decisions",
                            output: "sha-output",
                        }[path],
                    ),
                    mock.patch.object(
                        rollback,
                        "load_manifest",
                        return_value={"stages": {"2_ads": stage}},
                    ),
                ):
                    return rollback._validate_module_baseline(
                        workspace,
                        "ads",
                        source,
                        decisions,
                        output,
                        anomalies,
                    )

            with self.assertRaisesRegex(ValueError, "no longer replay"):
                call(base_stage, "different")
            with self.assertRaisesRegex(ValueError, "completed apply"):
                call({"status": "pending"})
            for field in ("input", "decisions", "output", "input_sha256", "decision_sha256", "output_sha256"):
                stale = dict(base_stage)
                stale[field] = "stale"
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                    call(stale)
            missing_run = dict(base_stage)
            missing_run["active_run_id"] = ""
            with self.assertRaisesRegex(ValueError, "active_run_id"):
                call(missing_run)
            self.assertEqual(call(base_stage)[-1], "run")

    def test_chapter_and_point_filters_preserve_nonmutating_and_reject_ambiguous_targets(self) -> None:
        keep = {"candidate_id": "K", "verdict": "keep"}
        cross = {
            "candidate_id": "D",
            "verdict": "delete",
            "anchors": [
                {"anchor_id": "A1", "chapter": {"index": 1}, "original": "广告一"},
                {"anchor_id": "A2", "chapter": {"index": 2}, "original": "广告二"},
            ],
        }
        for chapter in (0, True, "1"):
            with self.subTest(chapter=chapter), self.assertRaises(ValueError):
                rollback._filter_chapter([cross], chapter)  # type: ignore[arg-type]
        filtered, restored = rollback._filter_chapter([keep, cross], 1)
        self.assertEqual(restored, ["A1"])
        self.assertEqual(filtered[0], keep)
        self.assertEqual(filtered[1]["anchor_ids"], ["A2"])
        malformed = copy.deepcopy(cross)
        del malformed["anchors"][1]["original"]
        with self.assertRaisesRegex(ValueError, "original text"):
            rollback._filter_chapter([malformed], 1)
        with self.assertRaisesRegex(ValueError, "no matching"):
            rollback._filter_chapter([cross], 3)

        for decisions, candidate, message in (
            ([], "D", "exactly one"),
            ([cross, copy.deepcopy(cross)], "D", "exactly one"),
            ([keep], "K", "not a mutating"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                rollback._filter_point(decisions, candidate)
        point_filtered, point_restored = rollback._filter_point([keep, cross], "D")
        self.assertEqual(point_filtered, [keep])
        self.assertEqual(point_restored, ["A1", "A2"])

    def test_existing_targets_and_public_point_validation_fail_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            versions = workspace / "versions"
            versions.mkdir()
            (versions / "v0_original.txt").write_text("original", encoding="utf-8")
            target = versions / "rollback_v0_original.txt"
            target.write_text("existing", encoding="utf-8")
            with (
                mock.patch.object(rollback, "resolve_workspace_paths") as resolve,
                self.assertRaises(ValueError),
            ):
                rollback.rollback_point(workspace, "ads", "")
            resolve.assert_not_called()

            reads = {"source": versions / "v0_original.txt"}
            writes = {"target": target, "report": workspace / "report.json"}
            with (
                mock.patch.object(
                    rollback,
                    "resolve_workspace_paths",
                    return_value=(workspace, reads, writes),
                ),
                self.assertRaisesRegex(FileExistsError, "overwrite"),
            ):
                rollback.rollback_all(workspace, None, False)

    def test_private_targeted_level_rejects_unknown_level_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            versions = workspace / "versions"
            decisions_dir = workspace / "decisions"
            logs = workspace / "logs"
            report = workspace / "report"
            for path in (versions, decisions_dir, logs, report):
                path.mkdir()
            source = versions / "v1_preprocessed.txt"
            decisions = decisions_dir / "ads_decisions.jsonl"
            output = versions / "v2_ads_removed.txt"
            source.write_text("source", encoding="utf-8")
            decisions.write_text("", encoding="utf-8")
            output.write_text("source", encoding="utf-8")
            reads = {"source": source, "decisions": decisions}
            writes = {
                "filtered_decisions": decisions_dir / "filtered.jsonl",
                "target": output,
                "anomalies": logs / "anomalies.jsonl",
                "operations": logs / "operations.jsonl",
                "report": report / "rollback.json",
            }
            with (
                mock.patch.object(
                    rollback,
                    "resolve_workspace_paths",
                    return_value=(workspace, reads, writes),
                ),
                mock.patch.object(rollback, "resolve_in_workspace", return_value=output),
                mock.patch.object(
                    rollback,
                    "_validate_module_baseline",
                    return_value=("source", [], "a", "b", "c", "run"),
                ),
                self.assertRaisesRegex(ValueError, "unsupported targeted"),
            ):
                rollback._rollback_filtered(workspace, "ads", "other", 1, None)

    def test_main_dispatches_all_levels_and_rejects_missing_targets(self) -> None:
        calls = (
            (["rollback.py", "w", "--level", "all"], "rollback_all"),
            (["rollback.py", "w", "--level", "module", "--module", "ads"], "rollback_module"),
            (["rollback.py", "w", "--level", "chapter", "--chapter", "2"], "rollback_chapter"),
            (["rollback.py", "w", "--level", "point", "--candidate-id", "A"], "rollback_point"),
        )
        for argv, function in calls:
            with (
                self.subTest(function=function),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(rollback, function, return_value={"ok": True}) as selected,
                mock.patch("builtins.print"),
            ):
                rollback.main()
            selected.assert_called_once()
        for argv, message in (
            (["rollback.py", "w", "--level", "chapter"], "--chapter is required"),
            (["rollback.py", "w", "--level", "point"], "--candidate-id is required"),
        ):
            with mock.patch.object(sys, "argv", argv), self.assertRaisesRegex(ValueError, message):
                rollback.main()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["rollback.py", "w", "--level", "module", "--module", "titles"],
            ),
            mock.patch("sys.stderr", new=io.StringIO()),
            self.assertRaises(SystemExit) as rejected,
        ):
            rollback.main()
        self.assertEqual(rejected.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
