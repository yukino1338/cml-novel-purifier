from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, Callable
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import apply_decisions  # noqa: E402
import check_release  # noqa: E402
import experiment  # noqa: E402
import export_outputs  # noqa: E402
import normalize_layout  # noqa: E402
import rollback  # noqa: E402
import verify  # noqa: E402
from support_attestation import bind_passed_attestation  # noqa: E402
from tests.support_formal_ads import formalize_ads  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def repository_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if check_release.is_local_release_path(path, ROOT):
            continue
        snapshot[relative.as_posix()] = sha256_bytes(path.read_bytes())
    return snapshot


def make_workspace(root: Path, manifest: dict[str, Any]) -> Path:
    workspace = root / "sample-a.txt.cleanwork"
    for name in ("versions", "meta", "candidates", "decisions", "logs", "report"):
        (workspace / name).mkdir(parents=True, exist_ok=True)
    source = root / "sample-a.txt"
    source.write_text("第一章 起点\n匿名原始快照。\n", encoding="utf-8")
    v0 = workspace / "versions/v0_original.txt"
    v0.write_bytes(source.read_bytes())
    manifest = dict(manifest)
    manifest.pop("schema_version", None)
    manifest.pop("current_head", None)
    manifest.pop("artifacts", None)
    manifest["source"] = {
        "path": str(source.resolve()),
        "name": source.name,
        "size_bytes": source.stat().st_size,
        "sha256": sha256_bytes(source.read_bytes()),
    }
    manifest["v0"] = {
        "path": "versions/v0_original.txt",
        "size_bytes": v0.stat().st_size,
        "sha256": sha256_bytes(v0.read_bytes()),
    }
    manifest["workspace"] = str(workspace.resolve())
    manifest["schema_version"] = 2
    manifest["current_head"] = "versions/v0_original.txt"
    manifest["artifacts"] = {
        "versions/v0_original.txt": {
            "path": "versions/v0_original.txt",
            "sha256": sha256_bytes(v0.read_bytes()),
            "size_bytes": v0.stat().st_size,
            "parent_path": None,
            "parent_sha256": None,
            "run_id": "0" * 32,
            "stage": "source_snapshot",
            "config_sha256": None,
            "decision_sha256": None,
        }
    }
    inactive = {"pending", "blocked", "incomplete", "failed", "skipped"}
    stages = manifest.get("stages", {})
    if isinstance(stages, dict):
        for stage_name, stage_data in stages.items():
            if not isinstance(stage_data, dict) or stage_data.get("status") in inactive:
                continue
            run_id = hashlib.sha256(stage_name.encode("utf-8")).hexdigest()[:32]
            relative = f"report/_fixture_{stage_name}.json"
            artifact = workspace / relative
            write_json(artifact, {"stage": stage_name, "run_id": run_id})
            stage_data["run_id"] = run_id
            stage_data["artifacts"] = [relative]
            manifest["artifacts"][relative] = {
                "path": relative,
                "sha256": sha256_bytes(artifact.read_bytes()),
                "size_bytes": artifact.stat().st_size,
                "parent_path": None,
                "parent_sha256": None,
                "run_id": run_id,
                "stage": stage_name,
                "config_sha256": None,
                "decision_sha256": None,
            }
    write_json(workspace / "manifest.json", manifest)
    return workspace


def bind_current_head(workspace: Path, relative: str) -> None:
    manifest = read_json(workspace / "manifest.json")
    target = workspace / relative
    parent_path = "versions/v0_original.txt"
    parent = manifest["artifacts"][parent_path]
    run_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:32]
    manifest["artifacts"][relative] = {
        "path": relative,
        "sha256": sha256_bytes(target.read_bytes()),
        "size_bytes": target.stat().st_size,
        "parent_path": parent_path,
        "parent_sha256": parent["sha256"],
        "run_id": run_id,
        "stage": "fixture",
        "config_sha256": None,
        "decision_sha256": None,
    }
    manifest["current_head"] = relative
    write_json(workspace / "manifest.json", manifest)


def capture_rejection(call: Callable[[], Any], *message_tokens: str) -> tuple[Any, Exception | None]:
    try:
        return call(), None
    except (ValueError, RuntimeError, PermissionError) as exc:
        message = str(exc).casefold()
        if not any(token.casefold() in message for token in message_tokens):
            raise
        return None, exc


def stage_status(manifest: dict[str, Any], stage: str) -> str:
    stages = manifest.get("stages")
    if not isinstance(stages, dict):
        return ""
    value = stages.get(stage)
    return str(value.get("status", "")) if isinstance(value, dict) else ""


def portable_path(value: object) -> str:
    return str(value or "").replace("\\", "/")


class P0RegressionRedTests(unittest.TestCase):
    """Permanent regression contracts for the known P0 defect classes.

    Every case states a safe contract that must pass in each release candidate.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.repository_before = repository_snapshot()

    @classmethod
    def tearDownClass(cls) -> None:
        after = repository_snapshot()
        if after != cls.repository_before:
            before_keys = set(cls.repository_before)
            after_keys = set(after)
            changed = sorted(
                key for key in before_keys & after_keys if cls.repository_before[key] != after[key]
            )
            raise AssertionError(
                "F0-02 tests changed repository files: "
                f"added={sorted(after_keys - before_keys)}, "
                f"removed={sorted(before_keys - after_keys)}, changed={changed}"
            )

    def assert_contract(self, defect_id: str, checks: list[tuple[str, bool]]) -> None:
        required_groups = {"text", "logs", "report", "manifest"}
        groups = {label.split(".", 1)[0] for label, _ in checks}
        missing_groups = sorted(required_groups - groups)
        violations = [label for label, passed in checks if not passed]
        if missing_groups:
            violations.append("test.missing-artifact-groups=" + ",".join(missing_groups))
        if violations:
            self.fail(f"{defect_id} contract violations: " + ", ".join(violations))

    def test_p0_01_apply_is_atomic_when_any_anchor_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = make_workspace(
                root,
                {
                    "schema_version": 1,
                    "current_head": "versions/v1_preprocessed.txt",
                    "stages": {
                        "2_ads": {"status": "pending"},
                        "5_layout": {"status": "done"},
                        "6_verify": {"status": "done"},
                        "7_export": {"status": "done"},
                    },
                },
            )
            source = "第一章 起点\n正文甲。\n访问 https://reader.example/path\n正文乙。\n"
            source_path = workspace / "versions/v1_preprocessed.txt"
            output_path = workspace / "versions/v2_ads_removed.txt"
            operations_path = workspace / "logs/operations.jsonl"
            previous_report_path = workspace / "report/apply_report.json"
            source_path.write_text(source, encoding="utf-8")
            bind_current_head(workspace, "versions/v1_preprocessed.txt")
            output_path.write_text("上一轮完整输出。\n", encoding="utf-8")
            write_jsonl(operations_path, [{"run_id": "old-run", "candidate_id": "AD-OLD"}])
            write_json(previous_report_path, {"status": "passed", "run_id": "old-run"})
            formalize_ads(
                workspace,
                [
                    {
                        "candidate_id": "AD-0001",
                        "anchors": [
                            {
                                "offset": source.index("https://reader.example/path"),
                                "original": "https://reader.example/path",
                            },
                            {
                                "offset": source.index("正文乙"),
                                "original": "正文乙",
                            },
                        ],
                    }
                ],
                verdict="delete",
                action="delete",
            )
            # The scan and formal artifacts start valid.  Simulate the later
            # input drift that apply must reject before it can create a partial
            # output or append an operation log.
            source_path.write_text(
                source.replace("正文乙。", "正文丙。"), encoding="utf-8"
            )
            bind_current_head(workspace, "versions/v1_preprocessed.txt")
            source_before = source_path.read_bytes()
            output_before = output_path.read_bytes()
            operations_before = operations_path.read_bytes()
            report_before = previous_report_path.read_bytes()
            manifest_before = (workspace / "manifest.json").read_bytes()

            _, error = capture_rejection(
                lambda: apply_decisions.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "decisions/ads_decisions.jsonl",
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                ),
                "anchor",
                "锚点",
                "mismatch",
                "失配",
                "provenance",
                "stale",
                "scan",
            )

            self.assert_contract(
                "P0-MULTI-ANCHOR-ATOMICITY",
                [
                    ("text.source-preserved", source_path.read_bytes() == source_before),
                    ("text.old-output-preserved", output_path.read_bytes() == output_before),
                    ("logs.no-operation-committed", operations_path.read_bytes() == operations_before),
                    ("report.previous-active-report-preserved", previous_report_path.read_bytes() == report_before),
                    ("manifest.bytes-preserved", (workspace / "manifest.json").read_bytes() == manifest_before),
                    ("manifest.execution-blocked", error is not None),
                ],
            )

    def test_p0_09_apply_rejects_uncompiled_forged_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = make_workspace(
                root,
                {
                    "schema_version": 2,
                    "current_head": "versions/v1_preprocessed.txt",
                    "stages": {"2_ads": {"status": "pending"}},
                },
            )
            text = "第一章 起点\n正文甲。\n访问 https://reader.example/path 获取更新。\n正文乙。\n"
            input_path = workspace / "versions/v1_preprocessed.txt"
            decisions_path = workspace / "decisions/ads_decisions.jsonl"
            output_path = workspace / "versions/v2_ads_removed.txt"
            operations_path = workspace / "logs/operations.jsonl"
            report_path = workspace / "report/apply_report.json"
            input_path.write_text(text, encoding="utf-8")
            bind_current_head(workspace, "versions/v1_preprocessed.txt")
            original = "https://reader.example/path"
            write_jsonl(
                decisions_path,
                [
                    {
                        "scan_id": "a" * 64,
                        "candidate_id": "AD-FORGED",
                        "candidate_fingerprint": "b" * 64,
                        "verdict": "delete",
                        "action": "delete",
                        "anchors_truncated": False,
                        "anchors": [
                            {
                                "anchor_id": "AN-FORGED",
                                "offset": text.index(original),
                                "end": text.index(original) + len(original),
                                "original": original,
                                "splice_strategy": "exact",
                            }
                        ],
                    }
                ],
            )
            manifest_before = (workspace / "manifest.json").read_bytes()

            with self.assertRaisesRegex(ValueError, "formal|provenance|compiled"):
                apply_decisions.run(
                    workspace,
                    "ads",
                    "versions/v1_preprocessed.txt",
                    "decisions/ads_decisions.jsonl",
                    "versions/v2_ads_removed.txt",
                    "2_ads",
                )

            self.assertFalse(output_path.exists())
            self.assertFalse(operations_path.exists())
            self.assertFalse(report_path.exists())
            self.assertEqual((workspace / "manifest.json").read_bytes(), manifest_before)

    def test_p0_02_verify_rejects_operation_from_historical_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = make_workspace(
                root,
                {
                    "schema_version": 2,
                    "current_head": "versions/v2_ads_removed.txt",
                    "stages": {
                        "2_ads": {"status": "done", "active_run_id": "apply-new"},
                        "6_verify": {"status": "pending"},
                    },
                },
            )
            before = "第一章 起点\n广告甲\n正文甲。\n广告乙\n正文乙。\n"
            after = before
            before_path = workspace / "versions/v1_preprocessed.txt"
            after_path = workspace / "versions/v2_ads_removed.txt"
            decisions_path = workspace / "decisions/ads_decisions.jsonl"
            operations_path = workspace / "logs/operations.jsonl"
            before_path.write_text(before, encoding="utf-8")
            after_path.write_text(after, encoding="utf-8")
            bind_current_head(workspace, "versions/v2_ads_removed.txt")
            write_jsonl(
                decisions_path,
                [
                    {
                        "candidate_id": "AD-0001",
                        "candidate_fingerprint": "candidate-current",
                        "verdict": "delete",
                        "anchors": [
                            {"anchor_id": "anchor-a", "original": "广告甲"},
                            {"anchor_id": "anchor-b", "original": "广告乙"},
                        ],
                    }
                ],
            )
            write_jsonl(
                operations_path,
                [
                    {
                        "run_id": "apply-old",
                        "module": "ads",
                        "candidate_id": "AD-0001",
                        "anchor_id": "anchor-a",
                        "output": "versions/v2_ads_removed.txt",
                    }
                ],
            )
            before_bytes = before_path.read_bytes()
            after_bytes = after_path.read_bytes()
            operations_before = operations_path.read_bytes()

            report = verify.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "versions/v2_ads_removed.txt",
                "decisions/ads_decisions.jsonl",
                True,
            )
            manifest = read_json(workspace / "manifest.json")
            accounting = report.get("decision_accounting", {}) if isinstance(report, dict) else {}
            report_status = str(report.get("status", "")) if isinstance(report, dict) else ""
            if not report_status and isinstance(report.get("attestation"), dict):
                report_status = str(report["attestation"].get("status", ""))
            missing_anchors = accounting.get("missing_operation_anchor_ids", []) if isinstance(accounting, dict) else []

            self.assert_contract(
                "P0-HISTORICAL-LOG-FALSE-PASS",
                [
                    ("text.before-preserved", before_path.read_bytes() == before_bytes),
                    ("text.after-preserved", after_path.read_bytes() == after_bytes),
                    ("logs.history-preserved-read-only", operations_path.read_bytes() == operations_before),
                    ("report.not-passed", report_status in {"blocked", "incomplete"}),
                    ("report.missing-anchor-identified", "anchor-b" in missing_anchors),
                    ("manifest.verify-not-done", stage_status(manifest, "6_verify") in {"blocked", "incomplete"}),
                ],
            )

    def test_p0_03_export_without_current_attestation_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = make_workspace(
                root,
                {
                    "schema_version": 2,
                    "source": {"name": "sample-a.txt"},
                    "current_head": "versions/v5_layout_final.txt",
                    "stages": {
                        "6_verify": {"status": "pending"},
                        "7_export": {"status": "pending"},
                    },
                },
            )
            input_path = workspace / "versions/v5_layout_final.txt"
            input_path.write_text("第一章 起点\n正文甲。\n", encoding="utf-8")
            bind_current_head(workspace, "versions/v5_layout_final.txt")
            operations_path = workspace / "logs/operations.jsonl"
            anomalies_path = workspace / "logs/anomalies.jsonl"
            write_jsonl(operations_path, [{"run_id": "apply-old", "candidate_id": "AD-OLD"}])
            write_jsonl(anomalies_path, [{"run_id": "apply-old", "message": "历史记录"}])
            config_path = root / "export-config.json"
            write_json(
                config_path,
                {
                    "export": {
                        "title": "匿名样本",
                        "author": "匿名",
                    }
                },
            )
            output_root = root / "exports"
            input_before = input_path.read_bytes()
            operations_before = operations_path.read_bytes()
            anomalies_before = anomalies_path.read_bytes()

            _, error = capture_rejection(
                lambda: export_outputs.run(workspace, "auto", config_path, output_root),
                "verify",
                "verification",
                "attestation",
                "验证",
                "凭证",
            )
            report = read_json(workspace / "report/export_report.json")
            manifest = read_json(workspace / "manifest.json")
            output_files = list(output_root.rglob("*")) if output_root.exists() else []
            blocked = error is not None or stage_status(manifest, "7_export") in {"blocked", "incomplete"}

            self.assert_contract(
                "P0-EXPORT-WITHOUT-ATTESTATION",
                [
                    ("text.input-preserved", input_path.read_bytes() == input_before),
                    ("text.no-exported-content", not any(path.is_file() for path in output_files)),
                    ("logs.operations-preserved", operations_path.read_bytes() == operations_before),
                    ("logs.anomalies-preserved", anomalies_path.read_bytes() == anomalies_before),
                    ("report.no-successful-outputs", not bool(report.get("outputs"))),
                    ("manifest.export-not-done", stage_status(manifest, "7_export") != "done"),
                    ("manifest.export-blocked", blocked),
                ],
            )

    def test_p0_04_rollback_invalidates_all_downstream_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = make_workspace(
                root,
                {
                    "schema_version": 2,
                    "source": {"name": "sample-a.txt"},
                    "current_head": "versions/v5_layout_final.txt",
                    "stages": {
                        "2_ads": {"status": "done"},
                        "5_layout": {"status": "done"},
                        "6_verify": {"status": "done"},
                        "7_export": {"status": "done"},
                        "review": {"status": "done"},
                    },
                },
            )
            original = "第一章 起点\n正文甲。\n"
            cleaned = "第一章 起点\n"
            input_path = workspace / "versions/v1_preprocessed.txt"
            output_path = workspace / "versions/v2_ads_removed.txt"
            input_path.write_text(original, encoding="utf-8")
            bind_current_head(workspace, "versions/v1_preprocessed.txt")
            removed = original[len(cleaned) :]
            formalize_ads(
                workspace,
                [
                    {
                        "candidate_id": "AD-0001",
                        "offset": len(cleaned),
                        "end": len(original),
                        "original": removed,
                        "prefix": cleaned[-8:],
                        "suffix": "",
                    }
                ],
                verdict="delete",
                action="delete",
            )
            apply_decisions.run(
                workspace,
                "ads",
                "versions/v1_preprocessed.txt",
                "decisions/ads_decisions.jsonl",
                "versions/v2_ads_removed.txt",
                "2_ads",
            )
            normalize_layout.run(
                workspace,
                "versions/v2_ads_removed.txt",
                "versions/v5_layout_final.txt",
                None,
            )
            operations_path = workspace / "logs/operations.jsonl"
            write_json(workspace / "report/verify_report.json", {"status": "passed", "run_id": "verify-old"})
            write_json(workspace / "report/export_report.json", {"status": "passed", "run_id": "export-old"})
            input_before = input_path.read_bytes()
            operations_before = operations_path.read_bytes()

            report = rollback.rollback_module(workspace, "ads", None, True)
            manifest = read_json(workspace / "manifest.json")
            invalidated = report.get("invalidated_stages", []) if isinstance(report, dict) else []
            invalidated = set(invalidated) if isinstance(invalidated, list) else set()
            downstream = {"5_layout", "6_verify", "7_export", "review"}

            self.assert_contract(
                "P0-ROLLBACK-STATE-INVALIDATION",
                [
                    ("text.rollback-restored", output_path.read_text(encoding="utf-8") == original),
                    ("text.rollback-source-preserved", input_path.read_bytes() == input_before),
                    ("logs.history-preserved", operations_path.read_bytes().startswith(operations_before)),
                    (
                        "report.rollback-identifies-output",
                        portable_path(report.get("output")) == "versions/v2_ads_removed.txt",
                    ),
                    ("report.invalidations-recorded", downstream <= invalidated),
                    (
                        "manifest.downstream-pending",
                        all(stage_status(manifest, stage) == "pending" for stage in downstream),
                    ),
                    ("manifest.current-head-updated", manifest.get("current_head") == "versions/v2_ads_removed.txt"),
                ],
            )

    def test_p0_05_layout_preserves_semantic_ascii_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = make_workspace(
                root,
                {
                    "schema_version": 1,
                    "stages": {"5_layout": {"status": "pending"}},
                },
            )
            tokens = [
                "1.25",
                "v2.3.4",
                "08:30",
                "contact@example.com",
                "https://reader.example/a?x=1.25",
                "reader.example",
                r"C:\temp\sample-a.txt",
                "value.method(arg=1.25)",
            ]
            source = "第一章 起点\n" + " | ".join(tokens) + "\n正文甲。\n"
            input_path = workspace / "versions/v2_ads_removed.txt"
            output_path = workspace / "versions/v5_layout_final.txt"
            input_path.write_text(source, encoding="utf-8")
            bind_current_head(workspace, "versions/v2_ads_removed.txt")
            operations_path = workspace / "logs/operations.jsonl"
            write_jsonl(operations_path, [{"run_id": "apply-current", "candidate_id": "AD-0001"}])
            input_before = input_path.read_bytes()
            logs_before = operations_path.read_bytes()

            report = normalize_layout.run(
                workspace,
                "versions/v2_ads_removed.txt",
                "versions/v5_layout_final.txt",
                None,
            )
            manifest = read_json(workspace / "manifest.json")
            output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            text_safe = all(token in output for token in tokens)

            self.assert_contract(
                "P0-LAYOUT-SEMANTIC-PUNCTUATION",
                [
                    ("text.input-preserved", input_path.read_bytes() == input_before),
                    ("text.protected-tokens-preserved", text_safe),
                    ("logs.operations-preserved", operations_path.read_bytes() == logs_before),
                    (
                        "report.references-real-output",
                        portable_path(report.get("output")) == "versions/v5_layout_final.txt",
                    ),
                    ("manifest.done-only-for-safe-text", stage_status(manifest, "5_layout") != "done" or text_safe),
                ],
            )

    def test_p0_06_epub_preserves_front_matter_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = make_workspace(
                root,
                {
                    "schema_version": 2,
                    "source": {"name": "sample-a.txt"},
                    "current_head": "versions/v5_layout_final.txt",
                    "stages": {
                        "6_verify": {"status": "passed", "attestation": "verify-current"},
                        "7_export": {"status": "pending"},
                    },
                },
            )
            front_markers = ("匿名前置信息甲", "匿名前置信息乙")
            source = (
                f"{front_markers[0]}\n{front_markers[1]}\n\n"
                "第一章 起点\n正文甲。\n\n第二章 延续\n正文乙。\n"
            )
            input_path = workspace / "versions/v5_layout_final.txt"
            input_path.write_text(source, encoding="utf-8")
            bind_current_head(workspace, "versions/v5_layout_final.txt")
            bind_passed_attestation(workspace)
            operations_path = workspace / "logs/operations.jsonl"
            write_jsonl(operations_path, [{"run_id": "apply-current", "candidate_id": "AD-0001"}])
            config_path = root / "epub-config.json"
            write_json(
                config_path,
                {
                    "export": {
                        "title": "匿名样本",
                        "author": "匿名",
                    }
                },
            )
            output_root = root / "exports"
            input_before = input_path.read_bytes()
            logs_before = operations_path.read_bytes()

            report = export_outputs.run(
                workspace,
                "auto",
                config_path,
                output_root,
                requested_formats=export_outputs.ALL_FORMATS,
            )
            epub_paths = list(output_root.rglob("*.epub")) if output_root.exists() else []
            epub_text = ""
            if len(epub_paths) == 1:
                with zipfile.ZipFile(epub_paths[0], "r") as archive:
                    chapter_names = [
                        name
                        for name in archive.namelist()
                        if name.startswith("OEBPS/Text/chapter-") and name.endswith(".xhtml")
                    ]
                    epub_text = "\n".join(
                        archive.read(name).decode("utf-8") for name in sorted(chapter_names)
                    )
            content_ok = all(epub_text.count(marker) == 1 for marker in front_markers)
            manifest = read_json(workspace / "manifest.json")

            self.assert_contract(
                "P0-EPUB-FRONT-MATTER",
                [
                    ("text.input-preserved", input_path.read_bytes() == input_before),
                    ("text.front-matter-present-once", content_ok),
                    ("logs.operations-preserved", operations_path.read_bytes() == logs_before),
                    ("report.output-claimed-only-if-complete", not bool(report.get("outputs")) or content_ok),
                    ("manifest.done-only-if-complete", stage_status(manifest, "7_export") != "done" or content_ok),
                ],
            )

    def test_p0_07_removed_move_to_end_strategy_fails_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = make_workspace(
                root,
                {
                    "schema_version": 1,
                    "stages": {"5_layout": {"status": "pending"}},
                },
            )
            source = "第一章 起点\n正文甲。\n作者的话：匿名附记甲。\n正文乙。\n"
            input_path = workspace / "versions/v2_ads_removed.txt"
            output_path = workspace / "versions/v5_layout_final.txt"
            report_path = workspace / "report/layout_report.json"
            operations_path = workspace / "logs/operations.jsonl"
            input_path.write_text(source, encoding="utf-8")
            bind_current_head(workspace, "versions/v2_ads_removed.txt")
            output_path.write_text("上一轮完整排版。\n", encoding="utf-8")
            write_json(report_path, {"status": "passed", "run_id": "layout-old"})
            write_jsonl(operations_path, [{"run_id": "apply-current", "candidate_id": "AD-0001"}])
            config_path = root / "layout-config.json"
            write_json(
                config_path,
                {
                    "layout": {
                        "indent": "none",
                        "fullwidth_punctuation": False,
                        "author_note_strategy": "move_to_end",
                    }
                },
            )
            input_before = input_path.read_bytes()
            output_before = output_path.read_bytes()
            report_before = report_path.read_bytes()
            logs_before = operations_path.read_bytes()
            manifest_before = (workspace / "manifest.json").read_bytes()

            _, error = capture_rejection(
                lambda: normalize_layout.run(
                    workspace,
                    "versions/v2_ads_removed.txt",
                    "versions/v5_layout_final.txt",
                    config_path,
                ),
                "author_note_strategy",
                "move_to_end",
                "附记",
                "strategy",
            )

            self.assert_contract(
                "P0-AUTHOR-NOTE-DUPLICATION",
                [
                    ("text.input-preserved", input_path.read_bytes() == input_before),
                    ("text.old-output-preserved", output_path.read_bytes() == output_before),
                    ("logs.operations-preserved", operations_path.read_bytes() == logs_before),
                    ("report.previous-active-report-preserved", report_path.read_bytes() == report_before),
                    ("manifest.bytes-preserved", (workspace / "manifest.json").read_bytes() == manifest_before),
                    ("manifest.unsupported-option-rejected", error is not None),
                ],
            )

    def test_p0_08_experiment_refuses_sample_directory_as_cleanup_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_dir = root / "samples"
            sample_dir.mkdir()
            source_path = sample_dir / "sample-a.txt"
            logs_path = sample_dir / "logs/audit.jsonl"
            report_path = sample_dir / "report/baseline.json"
            manifest_path = sample_dir / "manifest.json"
            source_path.write_text("第一章 起点\n正文甲。\n", encoding="utf-8")
            write_jsonl(logs_path, [{"status": "sentinel"}])
            write_json(report_path, {"status": "sentinel"})
            write_json(manifest_path, {"status": "sentinel"})
            source_before = source_path.read_bytes()
            logs_before = logs_path.read_bytes()
            report_before = report_path.read_bytes()
            manifest_before = manifest_path.read_bytes()

            argv = [
                "experiment.py",
                str(sample_dir),
                "--sandbox",
                str(sample_dir),
            ]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                _, error = capture_rejection(
                    experiment.main,
                    "sandbox",
                    "sample",
                    "沙盒",
                    "样本",
                    "overlap",
                    "marker",
                )

            self.assert_contract(
                "P0-EXPERIMENT-SANDBOX-BOUNDARY",
                [
                    ("text.sample-preserved", read_bytes(source_path) == source_before),
                    ("logs.sentinel-preserved", read_bytes(logs_path) == logs_before),
                    ("report.sentinel-preserved", read_bytes(report_path) == report_before),
                    ("report.no-experiment-report-created", not (sample_dir / "experiment_report.json").exists()),
                    ("manifest.sentinel-preserved", read_bytes(manifest_path) == manifest_before),
                    ("manifest.dangerous-target-rejected", error is not None),
                ],
            )


if __name__ == "__main__":
    unittest.main()
