from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dry_run  # noqa: E402
import experiment  # noqa: E402


class ExperimentCoverageF7Tests(unittest.TestCase):
    def test_path_run_id_and_file_helpers_cover_true_false_and_missing(self) -> None:
        self.assertTrue(experiment._is_relative_to(ROOT / "tests", ROOT))
        self.assertFalse(experiment._is_relative_to(ROOT, ROOT / "tests"))
        self.assertFalse(
            experiment._is_relative_to(experiment.DEFAULT_SANDBOX.resolve(), ROOT.resolve())
        )
        self.assertFalse(experiment._is_link_or_junction(ROOT / "missing-f7"))
        self.assertTrue(experiment._valid_run_id("a" * 32))
        self.assertFalse(experiment._valid_run_id("A" * 32))
        self.assertFalse(experiment._valid_run_id(1))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "data.json"
            self.assertEqual(experiment.read_json(file), {})
            file.write_text('{"ok":true}', encoding="utf-8")
            self.assertEqual(experiment.read_json(file), {"ok": True})
            plain = root / "plain.txt"
            plain.write_text("x", encoding="utf-8")
            self.assertFalse(experiment._is_link_or_junction(plain))

    def test_prepare_sandbox_rejects_missing_sample_and_file_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "sample directory"):
                experiment.prepare_sandbox(root / "missing", root / "sandboxes" / "run")
            samples = root / "samples"
            samples.mkdir()
            target = root / "sandboxes" / "run"
            target.parent.mkdir()
            target.write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not a directory"):
                experiment.prepare_sandbox(
                    samples,
                    target,
                    project_root=root / "project",
                    user_home=root / "home",
                )

    def test_run_cmd_discovery_and_copy_names_cover_sampling_boundaries(self) -> None:
        code, elapsed, stdout, stderr = experiment.run_cmd(
            ["-c", "print('ok')"],
            ROOT,
            10,
        )
        self.assertEqual(code, 0)
        self.assertGreaterEqual(elapsed, 0)
        self.assertEqual(stdout.strip(), "ok")
        self.assertEqual(stderr, "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, size in enumerate((1, 2, 3, 4, 5), 1):
                (root / f"{index}.txt").write_bytes(b"x" * size)
            (root / "skip.md").write_text("x", encoding="utf-8")
            self.assertEqual(len(experiment.discover_samples(root, 0, 1, 5)), 5)
            self.assertEqual(len(experiment.discover_samples(root, 10, 2, 4)), 3)
            self.assertEqual(experiment.discover_samples(root, 1, 1, 5)[0].name, "1.txt")
            selected = experiment.discover_samples(root, 3, 1, 5)
            self.assertEqual([path.name for path in selected], ["1.txt", "3.txt", "5.txt"])
        self.assertEqual(experiment.unique_copy_name(Path("book"), 2), "sample-02-book.txt")
        self.assertEqual(experiment.unique_copy_name(Path("book.md"), 3), "sample-03-book.md")

    def test_run_sample_covers_success_nonzero_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("正文", encoding="utf-8")
            sandbox = root / "sandbox"
            sandbox.mkdir()
            with mock.patch.object(
                experiment,
                "run_cmd",
                return_value=(0, 0.125, "out", "err"),
            ) as runner:
                success = experiment.run_sample(source, 1, sandbox, 5, 10)
            expected_workspace = experiment.workspace_for_source(
                sandbox / "sample-01-source.txt"
            )
            self.assertTrue(success["ok"])
            self.assertEqual(
                [item["name"] for item in success["commands"]],
                [
                    "preprocess",
                    "parse_structure",
                    "scan_ads",
                    "make_ad_decisions",
                    "scan_titles",
                    "scan_blocked",
                ],
            )
            self.assertEqual(
                [call.args[0] for call in runner.call_args_list],
                [
                    ["scripts/preprocess.py", str(sandbox / "sample-01-source.txt")],
                    [
                        "scripts/parse_structure.py",
                        str(expected_workspace),
                    ],
                    [
                        "scripts/scan_ads.py",
                        str(expected_workspace),
                        "--max-candidates",
                        "10",
                    ],
                    [
                        "scripts/make_ad_decisions.py",
                        str(expected_workspace),
                    ],
                    [
                        "scripts/scan_titles.py",
                        str(expected_workspace),
                    ],
                    [
                        "scripts/scan_blocked.py",
                        str(expected_workspace),
                        "--max-candidates",
                        "10",
                    ],
                ],
            )
            self.assertNotIn("layout", success["reports"])
            self.assertNotIn("export", success["reports"])

            with mock.patch.object(
                experiment,
                "run_cmd",
                side_effect=[(0, 0.1, "", ""), (2, 0.2, "bad", "failed")],
            ):
                failed = experiment.run_sample(source, 2, sandbox, 5, 10)
            self.assertFalse(failed["ok"])
            self.assertEqual(len(failed["commands"]), 2)

            timeout = subprocess.TimeoutExpired("cmd", 5, output="stdout", stderr="stderr")
            with mock.patch.object(experiment, "run_cmd", side_effect=timeout):
                timed_out = experiment.run_sample(source, 3, sandbox, 5, 10)
            self.assertFalse(timed_out["ok"])
            self.assertEqual(timed_out["commands"][0]["code"], "timeout")
            self.assertEqual(timed_out["commands"][0]["stdout_tail"], "stdout")

            binary_timeout = subprocess.TimeoutExpired("cmd", 5, output=b"x", stderr=b"y")
            with mock.patch.object(experiment, "run_cmd", side_effect=binary_timeout):
                binary = experiment.run_sample(source, 4, sandbox, 5, 10)
            self.assertEqual(binary["commands"][0]["stdout_tail"], "")

    def test_run_sample_real_report_only_pipeline_uses_complete_pages(self) -> None:
        text = "\n".join(
            [
                "第一章 记录",
                "观测员甲记录了匿名场景中的第一次设备校验。",
                "请访问 https://reader.example.com/update 获取后续内容。",
                "观测员乙继续核对了另一组匿名数据。",
                "第二章 校验",
                "下载提示：请访问 https://files.example.org/package 获取资料。",
                "观测员丙将最后的校验结果录入匿名报告。",
                "",
            ]
        )
        source_bytes = text.encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "anonymous-evaluation.txt"
            source.write_bytes(source_bytes)
            sandbox = root / "sandbox"
            sandbox.mkdir()

            result = experiment.run_sample(source, 1, sandbox, 60, 1)

            self.assertTrue(result["ok"], result["commands"])
            self.assertEqual(
                [item["name"] for item in result["commands"]],
                [
                    "preprocess",
                    "parse_structure",
                    "scan_ads",
                    "make_ad_decisions",
                    "scan_titles",
                    "scan_blocked",
                ],
            )
            self.assertTrue(all(item["code"] == 0 for item in result["commands"]))
            self.assertEqual(source.read_bytes(), source_bytes)

            workspace = Path(result["workspace"])
            reports = workspace / "report"
            report_paths = {
                "ads": reports / "ads_scan_report.json",
                "ad_decisions": reports / "ad_decision_draft_report.json",
                "titles": reports / "titles_scan_report.json",
                "blocked": reports / "blocked_scan_report.json",
            }
            for key, path in report_paths.items():
                with self.subTest(report=key):
                    self.assertTrue(path.is_file())
                    self.assertEqual(
                        json.loads(path.read_text(encoding="utf-8")),
                        result["reports"][key],
                    )

            ads = result["reports"]["ads"]
            draft = result["reports"]["ad_decisions"]
            pages = ads["pages"]["manifest"]
            self.assertGreaterEqual(len(pages), 2)
            self.assertEqual(
                sum(int(page["record_count"]) for page in pages),
                ads["summary"]["total_candidate_count"],
            )
            self.assertEqual(
                [Path(value).as_posix() for value in draft["inputs"]],
                [str(page["file"]) for page in pages],
            )
            self.assertEqual(draft["candidate_count"], ads["summary"]["total_candidate_count"])
            self.assertEqual(draft["decision_count"], ads["summary"]["total_candidate_count"])
            for page in pages:
                self.assertTrue((workspace / page["file"]).is_file())
            self.assertTrue((workspace / result["reports"]["titles"]["output"]).is_file())
            self.assertTrue((workspace / result["reports"]["blocked"]["output"]).is_file())

    def test_summary_recommendations_and_markdown_cover_all_risk_branches(self) -> None:
        results = [
            {
                "source": "C:/samples/high.txt",
                "size_bytes": 123,
                "ok": False,
                "reports": {
                    "ads": {
                        "summary": {
                            "candidate_count": 60,
                            "total_candidate_count": 70,
                            "page_count": 2,
                            "strong_signal_deferred_count": 3,
                            "max_candidates_reached": True,
                        }
                    },
                    "ad_decisions": {"delete_count": 4, "uncertain_count": 5},
                    "titles": {"summary": {"candidate_count": 6}},
                    "blocked": {"summary": {"candidate_count": 500}},
                    "structure": {
                        "chapter_count": 7,
                        "structure_confidence": {"level": "low"},
                        "fallback_chunking": {"enabled": True},
                    },
                },
            },
            {"source": "empty.txt", "size_bytes": 0, "ok": True, "reports": {}},
        ]
        summary = experiment.summarize(results)
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["ads_total_candidate_total"], 70)
        self.assertEqual(summary["chapter_count_min"], 0)
        recs = experiment.recommendations(summary, results)
        self.assertGreaterEqual(len(recs), 6)
        self.assertTrue(any("high-ad" in item for item in recs))

        empty_summary = experiment.summarize([])
        default = experiment.recommendations(empty_summary, [])
        self.assertEqual(len(default), 1)
        self.assertIn("deterministic report-only evaluation stages", default[0])

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.md"
            experiment.write_markdown(
                output,
                {"summary": summary, "recommendations": recs, "results": results},
            )
            rendered = output.read_text(encoding="utf-8")
        self.assertIn("Experiment Report", rendered)
        self.assertIn("high.txt", rendered)
        self.assertIn("structure=low", rendered)

    def test_main_builds_report_from_discovered_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "sample.txt"
            sample.write_text("正文", encoding="utf-8")
            sandbox = root / "sandbox"
            result = {"source": str(sample), "ok": True, "size_bytes": 2, "reports": {}}
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    ["experiment.py", str(root), "--sandbox", str(sandbox)],
                ),
                mock.patch.object(experiment, "prepare_sandbox", return_value=(sandbox, "a" * 32)),
                mock.patch.object(experiment, "discover_samples", return_value=[sample]),
                mock.patch.object(experiment, "run_sample", return_value=result),
                mock.patch.object(experiment, "write_json") as write_json,
                mock.patch.object(experiment, "write_markdown") as write_markdown,
                mock.patch("builtins.print"),
            ):
                experiment.main()
        write_json.assert_called_once()
        write_markdown.assert_called_once()


class DryRunCoverageF7Tests(unittest.TestCase):
    def test_mutation_and_module_summary_cover_complete_pending_and_anchor_boundaries(self) -> None:
        config = dry_run.MODULES["ads"]
        self.assertTrue(dry_run.is_mutating({"verdict": "delete"}, config))
        self.assertTrue(dry_run.is_mutating({"action": "delete"}, config))
        self.assertFalse(dry_run.is_mutating({"action": "replace"}, config))
        self.assertFalse(dry_run.is_mutating({"verdict": "keep"}, config))
        candidates = [
            {"anchors": [{}, {}], "anchors_truncated": True},
            {"anchors": "invalid"},
        ]
        decisions = [
            {"verdict": "delete"},
            {"verdict": "uncertain"},
            {"action": "keep_original"},
        ]
        complete = dry_run.module_summary(candidates, decisions, "ads", config, True, True)
        self.assertEqual(complete["anchor_count"], 2)
        self.assertEqual(complete["truncated_candidate_count"], 1)
        self.assertEqual(complete["manual_review_count"], 2)
        self.assertEqual(complete["status"], "complete")
        pending = dry_run.module_summary([], [], "ads", config, True, False)
        self.assertEqual(pending["status"], "pending")
        titles = dry_run.module_summary([], [], "titles", dry_run.MODULES["titles"], True, False)
        self.assertEqual(titles["status"], "complete")
        self.assertTrue(titles["report_only"])
        self.assertNotIn("decisions", dry_run.MODULES["titles"])
        self.assertNotIn("mutating_actions", dry_run.MODULES["titles"])
        self.assertNotIn("decision_file", titles)
        suppressed_titles = dry_run.module_summary(
            [],
            [],
            "titles",
            dry_run.MODULES["titles"],
            True,
            False,
            scan_report={"summary": {"suppressed_report_only_count": 1}},
        )
        self.assertEqual(suppressed_titles["status"], "pending")
        capped_blocked = dry_run.module_summary(
            [],
            [],
            "blocked",
            dry_run.MODULES["blocked"],
            True,
            False,
            scan_report={"summary": {"max_candidates_reached": True}},
        )
        self.assertEqual(capped_blocked["status"], "pending")

    def test_markdown_stage_and_committed_path_cover_shapes_and_hash_failure(self) -> None:
        report = {
            "status": "complete",
            "modules": {
                "ads": {
                    "candidate_count": 1,
                    "decision_count": 1,
                    "anchor_count": 2,
                    "truncated_candidate_count": 0,
                    "estimated_mutating_decision_count": 1,
                    "manual_review_count": 0,
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "dry.md"
            dry_run.write_markdown(output, report)
            self.assertIn("Dry Run Report", output.read_text(encoding="utf-8"))
            artifact = root / "artifact.json"
            artifact.write_text("{}", encoding="utf-8")
            manifest = {"artifacts": {"report/x.json": {"sha256": "good"}}}
            with (
                mock.patch.object(dry_run, "resolve_in_workspace", return_value=artifact),
                mock.patch.object(dry_run, "sha256_file", return_value="good"),
            ):
                self.assertEqual(dry_run._committed_path(root, manifest, "report\\x.json"), artifact)
            with (
                mock.patch.object(dry_run, "resolve_in_workspace", return_value=artifact),
                mock.patch.object(dry_run, "sha256_file", return_value="bad"),
                self.assertRaisesRegex(ValueError, "not a current committed artifact"),
            ):
                dry_run._committed_path(root, manifest, "report/x.json")
        self.assertEqual(dry_run._stage({}, "ads"), {})
        self.assertEqual(dry_run._stage({"stages": {"2_ads": "bad"}}, "ads"), {})

    def test_load_current_scan_covers_inactive_identity_json_and_module_paths(self) -> None:
        workspace = Path("C:/book.cleanwork")
        with mock.patch.object(dry_run, "_stage", return_value={"status": "pending"}):
            self.assertEqual(dry_run.load_current_scan(workspace, {}, "ads"), ([], {}, False))
        with (
            mock.patch.object(dry_run, "_stage", return_value={"status": "candidates_ready"}),
            self.assertRaisesRegex(ValueError, "no scan identity"),
        ):
            dry_run.load_current_scan(workspace, {}, "ads")

        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            active = {"status": "candidates_ready", "scan_id": "scan"}
            report_path.write_text("bad", encoding="utf-8")
            with (
                mock.patch.object(dry_run, "_stage", return_value=active),
                mock.patch.object(dry_run, "_committed_path", return_value=report_path),
                self.assertRaisesRegex(ValueError, "not valid UTF-8 JSON"),
            ):
                dry_run.load_current_scan(workspace, {}, "titles")
            report_path.write_text("[]", encoding="utf-8")
            with (
                mock.patch.object(dry_run, "_stage", return_value=active),
                mock.patch.object(dry_run, "_committed_path", return_value=report_path),
                self.assertRaisesRegex(ValueError, "must be an object"),
            ):
                dry_run.load_current_scan(workspace, {}, "titles")
            report_path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(dry_run, "_stage", return_value=active),
                mock.patch.object(dry_run, "_committed_path", return_value=report_path),
                self.assertRaisesRegex(ValueError, "no candidate output"),
            ):
                dry_run.load_current_scan(workspace, {}, "titles")

            report = {"output": "candidates/titles.jsonl"}
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with (
                mock.patch.object(dry_run, "_stage", return_value=active),
                mock.patch.object(
                    dry_run,
                    "_committed_path",
                    side_effect=[report_path, Path(directory) / "titles.jsonl"],
                ),
                mock.patch.object(dry_run, "load_jsonl", return_value=[{"candidate_id": "T"}]),
                mock.patch.object(dry_run.scan_identity, "validate_scan_identity") as validate,
            ):
                candidates, loaded, current = dry_run.load_current_scan(workspace, {}, "titles")
            self.assertTrue(current)
            self.assertEqual(candidates[0]["candidate_id"], "T")
            validate.assert_called_once()

            ads_report = {"module": "ads"}
            report_path.write_text(json.dumps(ads_report), encoding="utf-8")
            with (
                mock.patch.object(dry_run, "_stage", return_value=active),
                mock.patch.object(dry_run, "_committed_path", return_value=report_path),
                mock.patch.object(
                    dry_run.scan_identity,
                    "load_validated_pages",
                    return_value=[{"candidate_id": "A"}],
                ),
            ):
                candidates, _, current = dry_run.load_current_scan(workspace, {}, "ads")
            self.assertTrue(current)
            self.assertEqual(candidates[0]["candidate_id"], "A")

    def test_load_current_decisions_covers_status_path_ids_coverage_and_identity(self) -> None:
        workspace = Path("C:/book.cleanwork")
        candidates = [{"candidate_id": "A", "candidate_fingerprint": "fp"}]
        report = {"scan_id": "scan"}
        for module in ("titles", "blocked"):
            with self.subTest(module=module), self.assertRaisesRegex(
                ValueError, "only executable for ads"
            ):
                dry_run.load_current_decisions(workspace, {}, module, candidates, report)
        with mock.patch.object(dry_run, "_stage", return_value={"status": "candidates_ready"}):
            self.assertEqual(
                dry_run.load_current_decisions(workspace, {}, "ads", candidates, report),
                ([], False),
            )
        with mock.patch.object(dry_run, "_stage", return_value={"status": "done"}):
            self.assertEqual(
                dry_run.load_current_decisions(workspace, {}, "ads", candidates, report),
                ([], False),
            )
        wrong = {"status": "done", "decisions": "decisions/other.jsonl"}
        with (
            mock.patch.object(dry_run, "_stage", return_value=wrong),
            self.assertRaisesRegex(ValueError, "canonical path"),
        ):
            dry_run.load_current_decisions(workspace, {}, "ads", candidates, report)

        valid_stage = {
            "status": "done",
            "decisions": "decisions/ads_decisions.jsonl",
            "decision_sha256": "sha",
        }
        path = Path("C:/decisions.jsonl")
        with (
            mock.patch.object(dry_run, "_stage", return_value=valid_stage),
            mock.patch.object(dry_run, "_committed_path", return_value=path),
            mock.patch.object(dry_run, "sha256_file", return_value="stale"),
            self.assertRaisesRegex(ValueError, "hash is stale"),
        ):
            dry_run.load_current_decisions(workspace, {}, "ads", candidates, report)

        def run_decisions(decisions: list[dict[str, object]], module: str = "ads"):
            stage = {
                "status": "formal_decisions_ready",
                "formal_decisions": dry_run.MODULES[module]["decisions"],
            }
            with (
                mock.patch.object(dry_run, "_stage", return_value=stage),
                mock.patch.object(dry_run, "_committed_path", return_value=path),
                mock.patch.object(dry_run, "load_jsonl", return_value=decisions),
            ):
                return dry_run.load_current_decisions(workspace, {}, module, candidates, report)

        for decisions in (
            [{"candidate_id": ""}],
            [{"candidate_id": "A"}, {"candidate_id": "A"}],
        ):
            with self.assertRaisesRegex(ValueError, "invalid or duplicate"):
                run_decisions(decisions)
        with self.assertRaisesRegex(ValueError, "complete candidate set"):
            run_decisions([])
        with self.assertRaisesRegex(ValueError, "identity is stale"):
            run_decisions(
                [{"candidate_id": "A", "scan_id": "wrong", "candidate_fingerprint": "fp"}]
            )
        decisions = [{"candidate_id": "A", "scan_id": "scan", "candidate_fingerprint": "fp"}]
        loaded, current = run_decisions(decisions)
        self.assertTrue(current)
        self.assertEqual(loaded, decisions)

    def test_main_exits_only_for_pending_report(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["dry_run.py", "book.cleanwork"]),
            mock.patch.object(dry_run, "run", return_value={"status": "complete"}),
            mock.patch("builtins.print"),
        ):
            dry_run.main()
        with (
            mock.patch.object(sys, "argv", ["dry_run.py", "book.cleanwork"]),
            mock.patch.object(dry_run, "run", return_value={"status": "pending"}),
            mock.patch("builtins.print"),
            self.assertRaisesRegex(SystemExit, "1"),
        ):
            dry_run.main()


if __name__ == "__main__":
    unittest.main()
