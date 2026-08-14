from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import shlex
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from ad_rules import SIGNAL_LABELS, format_family_label
from common import (
    ExternalDeliveryTransaction,
    WorkspaceTransaction,
    load_jsonl,
    load_jsonl_for_run,
    load_manifest,
    resolve_current_head,
    resolve_external_output_dir,
    resolve_external_output_paths,
    resolve_in_workspace,
    resolve_workspace_paths,
    sha256_file,
    validate_workspace,
    workspace_transaction_lock,
    write_json,
    write_utf8,
)
from export_outputs import require_export_attestation
import scan_identity
import ad_decision_policy


MODULES = ("ads", "titles", "blocked")
ADS_DECISIONS = "decisions/ads_decisions.jsonl"
ADS_DRAFT_DECISIONS = "decisions/ads_decisions.draft.jsonl"
REVIEW_ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "review"
REVIEW_TEMPLATE_VERSION = "2"
REVIEW_UI_SCHEMA = 2
REVIEW_TEMPLATE_SENTINEL = "__CML_REVIEW_TEMPLATE_VERSION__"
MAX_REVIEW_ORIGINAL_CHARS = 4_000
MAX_REVIEW_CONTEXT_CHARS = 600
MAX_REVIEW_ANCHOR_ORIGINAL_CHARS = 1_000
MAX_REVIEW_ANCHOR_AFFIX_CHARS = 120
MAX_REVIEW_ANCHORS = 20
MAX_REVIEW_METADATA_ITEMS = 20
MAX_REVIEW_METADATA_CHARS = 2_000

MIXED_WHOLE_BLOCK_GUARDS = ad_decision_policy.SEGMENT_REVIEW_GUARDS
REASON_CODES = (
    "external_ad_block",
    "narrative_text",
    "mixed_keep",
    "insufficient_context",
    "inconsistent_occurrences",
    "segment_boundary_wrong",
    "custom",
)

REVIEW_INPUT_PATHS = {
    "ads_pages": "candidates/ads_pages",
    "ads_candidates": "candidates/ads.jsonl",
    "titles_candidates": "candidates/titles.jsonl",
    "blocked_candidates": "candidates/blocked.jsonl",
    "ads_decisions": ADS_DECISIONS,
    "ads_draft": ADS_DRAFT_DECISIONS,
    "operations": "logs/operations.jsonl",
    "anomalies": "logs/anomalies.jsonl",
    "structure_report": "report/structure_report.json",
    "ads_report": "report/ads_scan_report.json",
    "ad_decisions_report": "report/ad_decision_draft_report.json",
    "titles_report": "report/titles_scan_report.json",
    "blocked_report": "report/blocked_scan_report.json",
    "verify_report": "report/verify_report.json",
    "layout_report": "report/layout_report.json",
    "export_report": "report/export_report.json",
}

MODULE_LABELS = {
    "ads": "广告清洗",
    "titles": "章节标题",
    "blocked": "屏蔽词候选",
}
METRIC_LABELS = {
    "workspace_count": "小说数量",
    "ads_candidates": "广告候选",
    "ad_draft_delete": "草稿建议删除",
    "ad_draft_uncertain": "草稿待判",
    "title_candidates": "标题候选",
    "blocked_candidates": "屏蔽词候选",
    "operations": "已执行操作",
    "deleted_operations": "已删除内容",
    "changed_characters": "变更字符数",
    "formal_decisions": "Agent 正式决策",
    "formal_uncertain": "正式未决",
    "ads_decision_pending": "正式判断未完成",
    "validation_issues": "验证残留",
    "protection_conflicts": "保护冲突",
    "anomalies": "锚点异常",
    "focus_items": "需要关注",
    "chapter_count": "识别章节",
    "structure_confidence": "结构置信度",
    "fallback_chunks": "定位分块",
    "candidates": "候选",
    "drafts": "草稿",
    "decisions": "正式决策",
}
VALUE_LABELS = {
    "delete": "建议删除",
    "keep": "建议保留",
    "uncertain": "待人工审核",
    "no-draft": "未生成草稿",
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
    "unknown": "未标注",
    "none": "无",
    "mask_chars": "遮罩字符",
    "possible_pseudo_title": "疑似伪标题",
    "duplicate_chapter_label": "重复章节编号",
    "non_monotonic_chapter_number": "章节编号非递增",
    "unbalanced_brackets": "标题括号不匹配",
}
VALUE_LABELS.update(SIGNAL_LABELS)

REVIEW_REPORT_PATH_HINTS = (
    "residual",
    "remaining",
    "unresolved",
    "anomal",
    "conflict",
    "failed",
    "mismatch",
    "error",
    "warning",
    "残留",
    "异常",
    "冲突",
    "失败",
)
STRUCTURE_CONFIDENCE_LABELS = {
    "high": "高",
    "medium": "中",
    "low": "低",
    "unknown": "未标注",
}
WORKFLOW_PRIORITY = {
    "needs-review": 0,
    "pending-verify": 1,
    "awaiting-agent": 2,
    "verified": 3,
    "completed": 4,
}
WORKFLOW_RISK_LABELS = {
    "needs-review": "需优先复核",
    "pending-verify": "等待验证",
    "awaiting-agent": "等待自动处理",
    "verified": "已验证，等待导出",
    "completed": "已完成，无阻止项",
}
BATCH_BOOK_SUMMARY_KEYS = (
    "ads_candidates",
    "title_candidates",
    "blocked_candidates",
    "operations",
    "deleted_operations",
    "changed_characters",
    "formal_decisions",
    "formal_uncertain",
    "ads_decision_pending",
    "anomalies",
    "validation_issues",
    "protection_conflicts",
    "chapter_count",
    "structure_confidence",
    "fallback_chunks",
)
BATCH_AGGREGATE_SUMMARY_KEYS = (
    "workspace_count",
    "ads_candidates",
    "ad_draft_delete",
    "ad_draft_uncertain",
    "title_candidates",
    "blocked_candidates",
    "operations",
    "deleted_operations",
    "changed_characters",
    "formal_decisions",
    "formal_uncertain",
    "ads_decision_pending",
    "anomalies",
    "validation_issues",
    "protection_conflicts",
    "focus_items",
)


def load_review_assets() -> tuple[str, str]:
    assets: dict[str, str] = {}
    for name in ("review.css", "review.js"):
        path = REVIEW_ASSET_DIR / name
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f"无法加载必需的审核页面资产 {name}: {exc}") from exc
        if content.count(REVIEW_TEMPLATE_SENTINEL) != 1:
            raise RuntimeError(f"审核页面资产 {name} 必须包含且仅包含一个 version sentinel")
        assets[name] = content.replace(REVIEW_TEMPLATE_SENTINEL, REVIEW_TEMPLATE_VERSION).strip()

    css = assets["review.css"]
    script = assets["review.js"]
    if not css or css.count("{") != css.count("}") or "</style" in css.lower():
        raise RuntimeError("审核页面资产 review.css 结构无效")
    if not script or "</script" in script.lower():
        raise RuntimeError("审核页面资产 review.js 结构无效")
    return css, script


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return data if isinstance(data, dict) else {}


def is_workspace(path: Path) -> bool:
    if (
        not path.is_dir()
        or not (path / "manifest.json").is_file()
        or not (path / "versions/v0_original.txt").is_file()
    ):
        return False
    try:
        validate_workspace(path)
    except (OSError, ValueError):
        return False
    return True


def discover_workspaces(paths: list[Path], recursive: bool) -> list[Path]:
    found: dict[str, Path] = {}
    for path in paths:
        path = path.resolve()
        if is_workspace(path):
            found[str(path)] = path
            continue
        if not path.is_dir():
            continue
        iterator = path.rglob("*.cleanwork") if recursive else path.glob("*.cleanwork")
        for candidate in iterator:
            workspace = candidate.resolve()
            try:
                workspace.relative_to(path)
            except ValueError:
                continue
            if is_workspace(workspace):
                found[str(workspace)] = workspace
    return [found[key] for key in sorted(found)]


def preflight_workspace_review(workspace: Path) -> tuple[Path, tuple[Path, ...]]:
    workspace, _, _ = resolve_workspace_paths(workspace)
    manifest = load_manifest(workspace)
    values = {
        "v0": "versions/v0_original.txt",
        "current_head": str(manifest["current_head"]),
    }
    for name, relative in REVIEW_INPUT_PATHS.items():
        if name == "ads_pages":
            continue
        path = workspace / relative
        if path.is_file():
            values[name] = relative
    ads_report_path = workspace / "report/ads_scan_report.json"
    if ads_report_path.is_file():
        report = read_json(ads_report_path)
        pages = report.get("pages") if isinstance(report, dict) else None
        page_manifest = pages.get("manifest") if isinstance(pages, dict) else None
        if isinstance(page_manifest, list):
            for index, entry in enumerate(page_manifest, 1):
                if isinstance(entry, dict) and isinstance(entry.get("file"), str):
                    values[f"ads_page_{index}"] = entry["file"]
    workspace, reads, _ = resolve_workspace_paths(workspace, reads=values)
    return workspace, tuple(dict.fromkeys(reads.values()))


def text_value(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def bounded_text(value: Any, limit: int, keep: str = "both") -> tuple[str, bool]:
    text = text_value(value)
    if len(text) <= limit:
        return text, False
    if limit < 2:
        raise ValueError("bounded review text limit must be at least 2")
    if keep == "head":
        return text[: limit - 1] + "…", True
    if keep == "tail":
        return "…" + text[-(limit - 1) :], True
    if keep != "both":
        raise ValueError("bounded review text keep mode is unsupported")
    left = (limit - 1) // 2
    right = limit - 1 - left
    return text[:left] + "…" + text[-right:], True


def bounded_review_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        return bounded_text(value, MAX_REVIEW_METADATA_CHARS)
    if isinstance(value, list):
        bounded: list[Any] = []
        truncated = len(value) > MAX_REVIEW_METADATA_ITEMS
        for item in value[:MAX_REVIEW_METADATA_ITEMS]:
            child, child_truncated = bounded_review_value(item)
            bounded.append(child)
            truncated = truncated or child_truncated
        return bounded, truncated
    if isinstance(value, dict):
        bounded_dict: dict[str, Any] = {}
        items = list(value.items())
        truncated = len(items) > MAX_REVIEW_METADATA_ITEMS
        for key, item in items[:MAX_REVIEW_METADATA_ITEMS]:
            bounded_key, key_truncated = bounded_text(key, 200)
            child, child_truncated = bounded_review_value(item)
            bounded_dict[bounded_key] = child
            truncated = truncated or key_truncated or child_truncated
        return bounded_dict, truncated
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    return bounded_text(value, MAX_REVIEW_METADATA_CHARS)


def json_for_html_script(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def count_by(records: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = record.get(field)
        if isinstance(value, list):
            keys = [str(item) for item in value] or ["none"]
        else:
            keys = [str(value or "unknown")]
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    return counts


MODULE_STAGES = {
    "ads": "2_ads",
    "titles": "3_titles",
    "blocked": "4_blocked_words",
}
SCAN_REPORT_PATHS = {
    "ads": "report/ads_scan_report.json",
    "titles": "report/titles_scan_report.json",
    "blocked": "report/blocked_scan_report.json",
}
ACTIVE_SCAN_STATUSES = frozenset(
    {"candidates_ready", "draft_decisions_ready", "formal_decisions_ready", "done"}
)


def _stage(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    stages = manifest.get("stages")
    value = stages.get(name) if isinstance(stages, dict) else None
    return value if isinstance(value, dict) else {}


def _ledger_path(
    workspace: Path,
    manifest: dict[str, Any],
    relative: str,
) -> Path:
    relative = relative.replace("\\", "/")
    path = resolve_in_workspace(workspace, relative, role="read")
    artifacts = manifest.get("artifacts")
    record = artifacts.get(relative) if isinstance(artifacts, dict) else None
    if not isinstance(record, dict) or record.get("sha256") != sha256_file(path):
        raise ValueError(f"review input is not a current committed artifact: {relative}")
    return path


def _ledger_json(
    workspace: Path,
    manifest: dict[str, Any],
    relative: str,
) -> dict[str, Any]:
    data = read_json(_ledger_path(workspace, manifest, relative))
    if not data:
        raise ValueError(f"review report is empty or invalid: {relative}")
    return data


def current_scan(
    workspace: Path,
    manifest: dict[str, Any],
    module: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    stage = _stage(manifest, MODULE_STAGES[module])
    if stage.get("status") not in ACTIVE_SCAN_STATUSES:
        return [], {}, False
    if not isinstance(stage.get("scan_id"), str):
        raise ValueError(f"active {module} stage has no bound scan identity")
    report_rel = SCAN_REPORT_PATHS[module]
    report = _ledger_json(workspace, manifest, report_rel)
    if module == "ads":
        candidates = scan_identity.load_validated_pages(workspace, report)
    else:
        output = report.get("output")
        if not isinstance(output, str):
            raise ValueError(f"{module} scan report has no candidate artifact")
        candidates = load_jsonl(_ledger_path(workspace, manifest, output))
        scan_identity.validate_scan_identity(workspace, report, candidates)
    return candidates, report, True


def _validate_ad_decision_identity(
    records: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    scan_report: dict[str, Any],
) -> None:
    candidate_map = by_candidate_id(candidates)
    record_map = by_candidate_id(records)
    if len(record_map) != len(records):
        raise ValueError("current ad decisions contain missing or duplicate candidate IDs")
    if set(record_map) != set(candidate_map):
        raise ValueError("current ad decisions do not cover the complete candidate set")
    for candidate_id, record in record_map.items():
        candidate = candidate_map[candidate_id]
        if record.get("scan_id") != scan_report.get("scan_id"):
            raise ValueError("current ad decision has a stale scan_id")
        if record.get("candidate_fingerprint") != candidate.get("candidate_fingerprint"):
            raise ValueError("current ad decision has a stale candidate fingerprint")


def current_ad_decisions(
    workspace: Path,
    manifest: dict[str, Any],
    candidates: list[dict[str, Any]],
    scan_report: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    stage = _stage(manifest, "2_ads")
    status = stage.get("status")
    if status not in {"formal_decisions_ready", "done"}:
        return [], False
    relative = stage.get("decisions") if status == "done" else stage.get("formal_decisions")
    if not isinstance(relative, str):
        return [], False
    relative = relative.replace("\\", "/")
    if relative != ADS_DECISIONS:
        raise ValueError("current ad decision path is not canonical")
    path = _ledger_path(workspace, manifest, relative)
    if status == "done" and stage.get("decision_sha256") != sha256_file(path):
        raise ValueError("current ad apply decision hash is stale")
    records = load_jsonl(path)
    _validate_ad_decision_identity(records, candidates, scan_report)
    return records, True


def current_ad_drafts(
    workspace: Path,
    manifest: dict[str, Any],
    candidates: list[dict[str, Any]],
    scan_report: dict[str, Any],
) -> list[dict[str, Any]]:
    stage = _stage(manifest, "2_ads")
    if stage.get("status") not in {"draft_decisions_ready", "formal_decisions_ready", "done"}:
        return []
    relative = stage.get("draft_decisions")
    if not isinstance(relative, str):
        return []
    relative = relative.replace("\\", "/")
    if relative != ADS_DRAFT_DECISIONS:
        raise ValueError("current ad draft decision path is not canonical")
    records = load_jsonl(_ledger_path(workspace, manifest, relative))
    _validate_ad_decision_identity(records, candidates, scan_report)
    return records


def current_logs(
    workspace: Path,
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    run_ids: set[str] = set()
    ads_stage = _stage(manifest, "2_ads")
    ads_run_id = ads_stage.get("active_run_id")
    if ads_stage.get("status") == "done" and isinstance(ads_run_id, str) and ads_run_id:
        run_ids.add(ads_run_id)
    current_head = str(manifest.get("current_head") or "")
    artifacts = manifest.get("artifacts")
    current_record = artifacts.get(current_head) if isinstance(artifacts, dict) else None
    if isinstance(current_record, dict) and str(current_record.get("stage") or "").startswith(
        "rollback_"
    ):
        run_id = current_record.get("run_id")
        if isinstance(run_id, str) and run_id:
            run_ids.add(run_id)
    operations_path = workspace / "logs/operations.jsonl"
    anomalies_path = workspace / "logs/anomalies.jsonl"
    operations = [
        record
        for run_id in sorted(run_ids)
        for record in load_jsonl_for_run(operations_path, run_id)
        if record.get("module") == "ads"
    ]
    anomalies = [
        record
        for run_id in sorted(run_ids)
        for record in load_jsonl_for_run(anomalies_path, run_id)
    ]
    return operations, anomalies


def active_stage_report(
    workspace: Path,
    manifest: dict[str, Any],
    stage_name: str,
    statuses: set[str],
    field: str = "report",
) -> dict[str, Any]:
    stage = _stage(manifest, stage_name)
    if stage.get("status") not in statuses:
        return {}
    relative = stage.get(field)
    artifacts = stage.get("artifacts")
    if (
        not isinstance(relative, str)
        or not isinstance(artifacts, list)
        or relative not in artifacts
    ):
        raise ValueError(f"active {stage_name} stage has no committed {field}")
    return _ledger_json(workspace, manifest, relative)


def current_verification(
    workspace: Path,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    report = active_stage_report(
        workspace,
        manifest,
        "6_verify",
        {"passed", "blocked", "incomplete"},
    )
    if _stage(manifest, "6_verify").get("status") != "passed":
        return report, None
    trace = require_export_attestation(workspace, resolve_current_head(workspace))
    return report, trace


def current_export(
    workspace: Path,
    manifest: dict[str, Any],
    verification: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    report = active_stage_report(workspace, manifest, "7_export", {"done"})
    if not report:
        return {}, False
    stage = _stage(manifest, "7_export")
    if (
        verification is None
        or stage.get("verification") != verification
        or report.get("verification") != verification
        or report.get("status") != "passed"
    ):
        raise ValueError("active export report does not match the current verification credential")
    outputs = report.get("outputs")
    artifacts = report.get("output_artifacts")
    if not isinstance(outputs, dict) or not isinstance(artifacts, dict) or set(outputs) != set(artifacts):
        raise ValueError("active export report has incomplete output artifacts")
    for kind, value in outputs.items():
        if not isinstance(value, str) or not isinstance(artifacts.get(kind), dict):
            raise ValueError("active export output entry is invalid")
        path = Path(value)
        if not path.is_absolute():
            path = workspace / path
        if not path.is_file() or sha256_file(path) != artifacts[kind].get("sha256"):
            raise ValueError(f"active export artifact is missing or stale: {kind}")
    return report, True


def by_candidate_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        candidate_id = str(record.get("candidate_id") or "")
        if candidate_id:
            result[candidate_id] = record
    return result


def as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def metadata_sources(
    candidate: dict[str, Any], draft: dict[str, Any] | None, decision: dict[str, Any] | None
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for record in (decision, draft, candidate):
        if not isinstance(record, dict):
            continue
        sources.append(record)
        source_candidate = record.get("source_candidate")
        if isinstance(source_candidate, dict):
            sources.append(source_candidate)
    return sources


def first_metadata(sources: list[dict[str, Any]], key: str) -> Any:
    for source in sources:
        value = source.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def family_metadata(
    candidate: dict[str, Any], draft: dict[str, Any] | None, decision: dict[str, Any] | None
) -> dict[str, Any]:
    sources = metadata_sources(candidate, draft, decision)
    signature = first_metadata(sources, "family_signature")
    cluster_id = first_metadata(sources, "cluster_id")
    site_entities: list[Any] = []
    intents: list[Any] = []
    if isinstance(signature, dict):
        site_entities = as_list(signature.get("site_entities") or signature.get("site_entity"))
        intents = as_list(signature.get("intents") or signature.get("intent"))
    site_entities = site_entities or as_list(first_metadata(sources, "site_entities") or first_metadata(sources, "site_entity"))
    intents = intents or as_list(first_metadata(sources, "intents") or first_metadata(sources, "intent"))
    family_label = format_family_label(
        {"site_entities": site_entities, "intents": intents}
    )
    if not family_label and isinstance(signature, str):
        family_label = signature
    family_key = str(cluster_id or "")
    if not family_key and signature not in (None, "", {}, []):
        family_key = json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "cluster_id": cluster_id,
        "family_signature": signature,
        "family_key": family_key,
        "family_label": family_label,
        "evidence": as_list(first_metadata(sources, "evidence")),
        "promoted_from": as_list(first_metadata(sources, "promoted_from")),
        "neighbor_span": as_list(first_metadata(sources, "neighbor_span") or first_metadata(sources, "neighbor_spans")),
    }


def formal_blocking_metadata(decision: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(decision, dict):
        return {"protected_terms": [], "blockers": [], "protection_conflict": False}
    protected_terms = as_list(decision.get("protected_terms") or decision.get("protected_hits"))
    blockers: list[Any] = []
    for key in ("safety_blockers", "blocking_reasons", "blockers"):
        blockers.extend(as_list(decision.get(key)))
    searchable = json.dumps(blockers, ensure_ascii=False).lower()
    protection_conflict = bool(
        protected_terms
        or decision.get("protection_conflict")
        or "protect" in searchable
        or "保护" in searchable
    )
    return {
        "protected_terms": protected_terms,
        "blockers": blockers,
        "protection_conflict": protection_conflict,
    }


def report_candidate_ids(report: dict[str, Any]) -> set[str]:
    candidate_ids: set[str] = set()

    def visit(value: Any, path: tuple[str, ...], relevant: bool = False) -> None:
        path_text = ".".join(path).lower()
        relevant = relevant or any(hint in path_text for hint in REVIEW_REPORT_PATH_HINTS)
        if isinstance(value, dict):
            if relevant and value.get("candidate_id"):
                candidate_ids.add(str(value["candidate_id"]))
            for key, child in value.items():
                visit(child, (*path, str(key)), relevant)
        elif isinstance(value, list):
            for child in value:
                visit(child, path, relevant)

    visit(report, ())
    return candidate_ids


def report_issue_count(report: dict[str, Any], candidate_ids: set[str]) -> int:
    warnings = report.get("warnings", []) if isinstance(report, dict) else []
    warning_count = len(warnings) if isinstance(warnings, list) else 0
    residuals = report.get("residuals", []) if isinstance(report, dict) else []
    residual_count = len(residuals) if isinstance(residuals, list) else 0
    reported_counts: list[int] = [warning_count, len(candidate_ids), residual_count]
    issue_count_names = {
        "strong_candidate_count",
        "residual_count",
        "unresolved_count",
        "failed_count",
        "mismatch_count",
        "conflict_count",
        "error_count",
        "validation_issue_count",
    }

    def visit(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, (*path, str(key)))
        elif isinstance(value, int):
            leaf = path[-1].lower() if path else ""
            if leaf in issue_count_names:
                reported_counts.append(value)

    visit(report, ())
    return max(reported_counts, default=0)


def bounded_context(before: Any, original: Any, after: Any) -> dict[str, Any]:
    before_text, before_truncated = bounded_text(before, MAX_REVIEW_CONTEXT_CHARS, "tail")
    original_text, original_truncated = bounded_text(original, MAX_REVIEW_ORIGINAL_CHARS)
    after_text, after_truncated = bounded_text(after, MAX_REVIEW_CONTEXT_CHARS, "head")
    return {
        "before": before_text,
        "original": original_text,
        "after": after_text,
        "excerpt_truncated": before_truncated or original_truncated or after_truncated,
    }


def contexts(candidate: dict[str, Any]) -> dict[str, Any]:
    if isinstance(candidate.get("contexts"), list) and candidate["contexts"]:
        item = candidate["contexts"][0]
        if isinstance(item, dict):
            return bounded_context(
                item.get("before"),
                item.get("original") or candidate.get("sample"),
                item.get("after"),
            )
    if isinstance(candidate.get("context"), dict):
        item = candidate["context"]
        return bounded_context(
            item.get("before"),
            item.get("original") or candidate.get("original"),
            item.get("after"),
        )
    anchors = candidate.get("anchors")
    original = ""
    if isinstance(anchors, list) and anchors and isinstance(anchors[0], dict):
        original = str(anchors[0].get("original") or "")
    return bounded_context(
        "",
        candidate.get("sample") or candidate.get("current") or candidate.get("original") or original,
        "",
    )


def first_chapter(candidate: dict[str, Any], decision: dict[str, Any] | None = None) -> dict[str, Any] | None:
    locator = candidate.get("locator")
    if isinstance(locator, dict):
        return locator
    for source in (candidate, decision or {}):
        anchors = source.get("anchors")
        if not isinstance(anchors, list):
            continue
        for anchor in anchors:
            if not isinstance(anchor, dict):
                continue
            chapter = anchor.get("chapter")
            if isinstance(chapter, dict):
                return chapter
            locator = anchor.get("locator")
            if isinstance(locator, dict):
                return locator
    return None


def candidate_group(
    module: str,
    candidate: dict[str, Any],
    draft: dict[str, Any] | None,
    family: dict[str, Any] | None = None,
) -> str:
    if module == "ads":
        if family and family.get("family_key"):
            return f"广告家族 / {family.get('family_label') or '未命名候选组'}"
        signals = candidate.get("signals")
        signal_text = "+".join(str(item) for item in signals) if isinstance(signals, list) and signals else "none"
        return f"{candidate.get('layer', 'unknown')} / {signal_text}"
    if module == "titles":
        return f"{candidate.get('category', 'unknown')} / {candidate.get('severity', 'unknown')}"
    return str(candidate.get("mask_type") or "unknown")


def shell_quote(value: Any, shell: str) -> str:
    text = str(value)
    if shell == "powershell":
        return "'" + text.replace("'", "''") + "'"
    if shell == "posix":
        return shlex.quote(text)
    raise ValueError("shell must be 'powershell' or 'posix'")


def rollback_command(
    workspace: Path,
    module: str,
    level: str,
    value: str | int | None = None,
    *,
    shell: str,
) -> str:
    if level not in {"all", "module", "chapter", "point"}:
        raise ValueError(f"unsupported rollback level: {level}")
    if level != "all" and module != "ads":
        raise ValueError("targeted rollback is supported only for ads")
    base = f"python scripts/rollback.py {shell_quote(workspace, shell)} --level {level}"
    if level in {"module", "chapter", "point"}:
        base += f" --module {module}"
    if level == "module":
        base += " --overwrite"
    if level == "chapter":
        base += f" --chapter {value}"
    if level == "point":
        base += f" --candidate-id {shell_quote(value, shell)}"
    return base


def rollback_commands(
    workspace: Path,
    module: str,
    level: str,
    value: str | int | None = None,
) -> dict[str, str]:
    return {
        shell: rollback_command(workspace, module, level, value, shell=shell)
        for shell in ("powershell", "posix")
    }


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _human_excerpt(value: Any, limit: int = 36) -> str:
    text = " ".join(text_value(value).split())
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bounded_locator(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    title, _ = bounded_text(value.get("title"), 200, "head")
    return {
        key: field
        for key, field in (
            ("kind", value.get("kind")),
            ("index", value.get("index")),
            ("title", title),
        )
        if field not in (None, "")
    }


_EXECUTABLE_REVIEW_KEYS = frozenset(
    {
        "edit_plan",
        "end",
        "free_end",
        "free_offset",
        "offset",
        "parent_end",
        "parent_start",
        "range",
        "ranges",
        "relative_end",
        "relative_start",
        "segments",
        "source_end",
        "source_offset",
        "spans",
        "start",
    }
)


def _is_executable_review_key(value: Any) -> bool:
    key = str(value).strip().lower().replace("-", "_")
    return key in _EXECUTABLE_REVIEW_KEYS or key.endswith(("_offset", "_start", "_end"))


def _redact_executable_review_value(value: Any) -> Any:
    """Keep the offline review projection informative, never executable.

    The formal compiler derives splice coordinates from the current candidates;
    a browser payload must not become an alternate source of executable ranges.
    Unknown metadata is retained only after recursively removing range-shaped
    fields, so compatibility inputs cannot reintroduce offsets by accident.
    """
    if isinstance(value, dict):
        return {
            str(key): _redact_executable_review_value(child)
            for key, child in value.items()
            if not _is_executable_review_key(key)
        }
    if isinstance(value, list):
        return [_redact_executable_review_value(child) for child in value]
    return value


def _review_anchor_projection(anchor: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded, non-executable anchor description for the browser."""
    original, original_truncated = bounded_text(
        anchor.get("original"),
        MAX_REVIEW_ANCHOR_ORIGINAL_CHARS,
    )
    prefix, prefix_truncated = bounded_text(
        anchor.get("prefix"),
        MAX_REVIEW_ANCHOR_AFFIX_CHARS,
        "tail",
    )
    suffix, suffix_truncated = bounded_text(
        anchor.get("suffix"),
        MAX_REVIEW_ANCHOR_AFFIX_CHARS,
        "head",
    )
    return {
        "anchor_id": anchor.get("anchor_id"),
        "line": anchor.get("line"),
        "original": original,
        "prefix": prefix,
        "suffix": suffix,
        "chapter": _bounded_locator(anchor.get("chapter") or anchor.get("locator")),
        "truncated": original_truncated or prefix_truncated or suffix_truncated,
    }


def _plain_reason(signals: Any, mutation_guard: Any) -> str:
    signal_set = {str(value) for value in signals} if isinstance(signals, list) else set()
    if mutation_guard in MIXED_WHOLE_BLOCK_GUARDS:
        return "正文与外部引导混在同一文本块，需要保留正文并重新确认边界。"
    if "domain" in signal_set or "url" in signal_set:
        if "contact" in signal_set:
            return "含站外地址和联系引导。"
        if "download" in signal_set:
            return "含站外地址和下载引导。"
        return "含网址或域名等站外引导。"
    if "contact" in signal_set or "email" in signal_set:
        return "含联系方式或联系引导。"
    if "watermark" in signal_set or "copy_marker" in signal_set:
        return "含来源、水印或转载标记。"
    if "author_note" in signal_set:
        return "含作者说明，需要结合正文判断。"
    return "规则命中此处，请结合上下文复核。"


def _display_title(candidate: dict[str, Any], family: dict[str, Any], original: str) -> str:
    signals = candidate.get("signals")
    signal_values = signals if isinstance(signals, list) else []
    labels = [
        SIGNAL_LABELS.get(str(value), str(value))
        for value in signal_values
        if str(value)
    ]
    labels = list(dict.fromkeys(labels))
    if labels:
        return "、".join(labels[:3]) + "候选"
    family_label = str(family.get("family_label") or "").strip()
    if family_label:
        return family_label
    excerpt = _human_excerpt(original)
    return excerpt or "未命名候选组"


def _edit_plan_metadata(
    candidate: dict[str, Any],
    draft: dict[str, Any] | None,
    decision: dict[str, Any] | None,
) -> tuple[str | None, bool, str | None]:
    del draft, decision
    edit_plan = candidate.get("edit_plan")
    if not isinstance(edit_plan, dict):
        return None, False, None
    value = edit_plan.get("edit_plan_id")
    if not isinstance(value, str) or not value:
        return None, False, None
    try:
        normalized = ad_decision_policy.normalize_edit_plan(edit_plan, candidate)
    except ValueError:
        return value, False, _canonical_sha256(edit_plan)
    return normalized["edit_plan_id"], True, _canonical_sha256(normalized)


def _review_delete_eligibility(
    *,
    module: str,
    anchors_count: int,
    anchors_truncated: bool,
    mutation_guard: Any,
    blocking: dict[str, Any],
    edit_plan_id: str | None,
    edit_plan_validated: bool,
) -> dict[str, Any]:
    """Compatibility projection backed by the shared Python authority."""
    candidate: dict[str, Any] = {
        "occurrence_count": anchors_count,
        "anchors_truncated": anchors_truncated,
        "anchors": [{} for _ in range(anchors_count)],
    }
    if mutation_guard:
        candidate["mutation_guard"] = mutation_guard
    result = ad_decision_policy.delete_eligibility(
        candidate,
        module=module,
        protection_conflict=bool(blocking.get("protection_conflict")),
        formal_blockers=tuple(str(value) for value in blocking.get("blockers", [])),
    )
    if edit_plan_id and edit_plan_validated:
        # A normalized review item no longer carries executable plan ranges.
        # Preserve only the earlier Python projection; this branch never makes
        # an unvalidated plan executable.
        result["segment_delete_allowed"] = bool(
            result["segment_delete_allowed"] and edit_plan_validated
        )
    return result


def review_candidate(
    workspace: Path,
    module: str,
    candidate: dict[str, Any],
    draft: dict[str, Any] | None,
    decision: dict[str, Any] | None,
    operation: dict[str, Any] | None,
) -> dict[str, Any]:
    candidate_id = str(candidate.get("candidate_id") or "")
    chapter = _bounded_locator(first_chapter(candidate, decision or draft))
    ctx = contexts(candidate)
    rollback: dict[str, dict[str, str]] = {}
    if module == "ads":
        rollback["module"] = rollback_commands(workspace, module, "module")
        if operation and candidate_id:
            rollback["point"] = rollback_commands(workspace, module, "point", candidate_id)
        if operation and chapter and isinstance(chapter.get("index"), int):
            rollback["chapter"] = rollback_commands(
                workspace,
                module,
                "chapter",
                int(chapter["index"]),
            )

    anchors = candidate.get("anchors")
    anchor_details = []
    if isinstance(anchors, list):
        for anchor in anchors[:MAX_REVIEW_ANCHORS]:
            if not isinstance(anchor, dict):
                continue
            anchor_details.append(_review_anchor_projection(anchor))
    family_raw = family_metadata(candidate, draft, decision) if module == "ads" else {}
    formal_blocking = formal_blocking_metadata(decision)
    draft_blocking = formal_blocking_metadata(draft)
    blocking_raw = {
        "protected_terms": [
            *formal_blocking["protected_terms"],
            *draft_blocking["protected_terms"],
        ],
        "blockers": [*formal_blocking["blockers"], *draft_blocking["blockers"]],
        "protection_conflict": bool(
            formal_blocking["protection_conflict"]
            or draft_blocking["protection_conflict"]
        ),
    }
    family, family_truncated = bounded_review_value(family_raw)
    blocking, blocking_truncated = bounded_review_value(blocking_raw)
    if not isinstance(family, dict) or not isinstance(blocking, dict):
        raise RuntimeError("bounded review metadata must remain objects")
    family_key = str(family_raw.get("family_key") or "")
    if len(family_key) > MAX_REVIEW_METADATA_CHARS:
        family["family_key"] = "sha256:" + hashlib.sha256(family_key.encode("utf-8")).hexdigest()
    draft_reason, draft_reason_truncated = bounded_text(
        (draft or {}).get("reason"),
        MAX_REVIEW_METADATA_CHARS,
    )
    formal_reason, formal_reason_truncated = bounded_text(
        (decision or {}).get("reason"),
        MAX_REVIEW_METADATA_CHARS,
    )
    mutation_guard = first_metadata(
        metadata_sources(candidate, draft, decision),
        "mutation_guard",
    )
    edit_plan_id, edit_plan_validated, edit_plan_sha256 = _edit_plan_metadata(
        candidate, draft, decision
    )
    anchors_count = len(anchors) if isinstance(anchors, list) else 0
    projection_truncated = isinstance(anchors, list) and len(anchors) > MAX_REVIEW_ANCHORS
    anchors_truncated = projection_truncated or bool(candidate.get("anchors_truncated"))
    eligibility = ad_decision_policy.delete_eligibility(
        candidate,
        module=module,
        protection_conflict=bool(blocking.get("protection_conflict")),
        formal_blockers=tuple(str(value) for value in blocking.get("blockers", [])),
        projection_truncated=projection_truncated,
    )
    segment_previews: list[dict[str, Any]] = []
    segment_preview_text_truncated = False
    if edit_plan_validated:
        raw_segment_previews = ad_decision_policy.edit_plan_preview(candidate)
        for preview in raw_segment_previews[:MAX_REVIEW_ANCHORS]:
            bounded_preview = {
                "anchor_id": preview["anchor_id"],
                "boundary_kind": preview["boundary_kind"],
            }
            preview_truncated = False
            for key in ("keep_text", "delete_text", "after_text"):
                bounded_value, value_truncated = bounded_text(
                    preview[key], MAX_REVIEW_METADATA_CHARS
                )
                bounded_preview[key] = bounded_value
                preview_truncated = preview_truncated or value_truncated
            bounded_preview["preview_truncated"] = preview_truncated
            segment_preview_text_truncated = (
                segment_preview_text_truncated or preview_truncated
            )
            segment_previews.append(bounded_preview)
    segment_previews_truncated = (
        edit_plan_validated
        and len(candidate.get("anchors", [])) > MAX_REVIEW_ANCHORS
    ) or segment_preview_text_truncated
    first_segment_preview = segment_previews[0] if segment_previews else {}
    first_anchor = next(
        (value for value in anchor_details if isinstance(value, dict)),
        {},
    )
    fingerprint = candidate.get("candidate_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        fingerprint = _canonical_sha256(
            {
                "candidate_id": candidate_id,
                "module": module,
                "anchors": [
                    {
                        "anchor_id": value.get("anchor_id"),
                        "original": value.get("original"),
                    }
                    for value in anchor_details
                ],
            }
        )
    result = {
        "candidate_id": candidate_id,
        "candidate_fingerprint": fingerprint,
        "module": module,
        "group": candidate_group(module, candidate, draft, family),
        "risk": candidate.get("risk_hint") or candidate.get("severity") or "unknown",
        "draft_verdict": (draft or {}).get("verdict") or (draft or {}).get("action"),
        "draft_reason": draft_reason,
        "draft_record_sha256": _canonical_sha256(draft) if isinstance(draft, dict) else None,
        "formal_decision": (decision or {}).get("verdict") or (decision or {}).get("action"),
        "formal_reason": formal_reason,
        "formal_record_sha256": (
            _canonical_sha256(decision) if isinstance(decision, dict) else None
        ),
        "operation_action": (operation or {}).get("action"),
        "chapter": chapter,
        "occurrence_count": candidate.get("occurrence_count", anchors_count),
        "anchors_count": anchors_count,
        "anchors": anchor_details,
        "anchors_truncated": anchors_truncated,
        "line_number": first_anchor.get("line"),
        "before": ctx["before"],
        "original": ctx["original"],
        "after": ctx["after"],
        "match_text": first_anchor.get("original") or "",
        "excerpt_truncated": bool(ctx["excerpt_truncated"]),
        "mutation_guard": mutation_guard,
        "edit_plan_id": edit_plan_id,
        "edit_plan_validated": edit_plan_validated,
        "edit_plan_sha256": edit_plan_sha256,
        "segment_previews": segment_previews,
        "segment_previews_truncated": segment_previews_truncated,
        "keep_preview": first_segment_preview.get("keep_text", ""),
        "delete_preview": first_segment_preview.get("delete_text", ""),
        "post_edit_preview": first_segment_preview.get("after_text", ""),
        **family,
        **blocking,
        **eligibility,
        "display_title": _display_title(candidate, family, ctx["original"]),
        "plain_reason": _plain_reason(candidate.get("signals"), mutation_guard),
        "metadata_truncated": (
            family_truncated
            or blocking_truncated
            or draft_reason_truncated
            or formal_reason_truncated
            or segment_preview_text_truncated
        ),
        "needs_review": False,
        "review_reasons": [],
        "rollback": rollback,
    }
    return result


def group_records(records: list[dict[str, Any]], sample_limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        group = str(record.get("group") or "unknown")
        item = grouped.setdefault(group, {"group": group, "count": 0, "anchors_count": 0, "samples": []})
        item["count"] += 1
        item["anchors_count"] += int(record.get("anchors_count", 0) or 0)
        if len(item["samples"]) < sample_limit:
            item["samples"].append(record)
    return sorted(grouped.values(), key=lambda item: (-int(item["count"]), str(item["group"])))


def ad_operation_map(operations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for operation in operations:
        if operation.get("module") != "ads":
            continue
        candidate_id = str(operation.get("candidate_id") or "")
        if candidate_id and candidate_id not in result:
            result[candidate_id] = operation
    return result


def module_review(
    workspace: Path,
    module: str,
    max_items: int,
    sample_limit: int,
    candidates: list[dict[str, Any]],
    draft_records: list[dict[str, Any]],
    decision_records: list[dict[str, Any]],
    operation_records: list[dict[str, Any]],
    scan_report: dict[str, Any],
    scan_current: bool,
    decisions_current: bool,
) -> dict[str, Any]:
    drafts = by_candidate_id(draft_records)
    decisions = by_candidate_id(decision_records)
    operations = ad_operation_map(operation_records) if module == "ads" else {}

    review_items: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        review_items.append(
            review_candidate(
                workspace,
                module,
                candidate,
                drafts.get(candidate_id),
                decisions.get(candidate_id),
                operations.get(candidate_id),
            )
        )
    details = review_items[:max_items]

    draft_values = list(drafts.values())
    report_summary = scan_report.get("summary", {}) if scan_current else {}

    return {
        "scan_id": scan_report.get("scan_id") if scan_current else None,
        "candidate_set_sha256": (
            scan_report.get("candidate_set_sha256") if scan_current else None
        ),
        "candidate_count": len(candidates),
        "scan_current": scan_current,
        "detail_count": len(details),
        "decision_count": len(decisions),
        "decisions_current": decisions_current,
        "decision_by_verdict": count_by(
            [{"verdict": item.get("verdict") or item.get("action")} for item in decisions.values()], "verdict"
        ),
        "draft_count": len(drafts),
        "draft_by_verdict": count_by(draft_values, "verdict"),
        "candidate_summary": report_summary,
        "groups": group_records(details, sample_limit),
        "details": details,
        "review_items": review_items,
    }


def source_name(workspace: Path, manifest: dict[str, Any]) -> str:
    source = manifest.get("source")
    if isinstance(source, dict) and source.get("name"):
        return str(source["name"])
    return workspace.name.removesuffix(".cleanwork")


def annotate_review_items(
    modules: dict[str, Any], verify_report: dict[str, Any], anomalies: list[dict[str, Any]]
) -> dict[str, Any]:
    residual_ids = report_candidate_ids(verify_report)
    anomaly_ids = {
        str(item.get("candidate_id"))
        for item in anomalies
        if isinstance(item, dict) and item.get("candidate_id")
    }
    formal_uncertain = 0
    protection_conflicts = 0
    review_candidate_count = 0
    for module in MODULES:
        for item in modules[module].get("review_items", []):
            reasons: list[str] = []
            if item.get("formal_decision") == "uncertain":
                formal_uncertain += 1
                reasons.append("Agent 正式结论仍为未决")
            if item.get("formal_decision") and item.get("protection_conflict"):
                protection_conflicts += 1
                reasons.append("正式结论存在作品保护冲突")
            candidate_id = str(item.get("candidate_id") or "")
            if candidate_id and candidate_id in residual_ids:
                reasons.append("验证后仍有对应残留")
            if candidate_id and candidate_id in anomaly_ids:
                reasons.append("候选锚点记录异常")
            item["review_reasons"] = list(dict.fromkeys(reasons))
            item["needs_review"] = bool(reasons)
            if reasons:
                review_candidate_count += 1
    return {
        "formal_uncertain": formal_uncertain,
        "protection_conflicts": protection_conflicts,
        "validation_issues": report_issue_count(verify_report, residual_ids),
        "validation_candidate_ids": sorted(residual_ids),
        "anomaly_candidate_ids": sorted(anomaly_ids),
        "review_candidate_count": review_candidate_count,
    }


def focus_items(
    workspace: Path,
    modules: dict[str, Any],
    reports: dict[str, Any],
    anomalies: list[dict[str, Any]],
    review_summary: dict[str, Any],
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if int(reports.get("blocked", {}).get("summary", {}).get("candidate_count", 0) or 0) > 200:
        items.append(
            {
                "level": "info",
                "message": "屏蔽词候选较多；Agent 仅报告候选与上下文，原文保持不变。",
            }
        )
    if int(review_summary.get("formal_uncertain", 0) or 0) > 0:
        items.append(
            {
                "level": "review",
                "message": f"{review_summary['formal_uncertain']} 条 Agent 正式结论存在安全阻止项。",
            }
        )
    if int(review_summary.get("protection_conflicts", 0) or 0) > 0:
        items.append(
            {
                "level": "review",
                "message": f"{review_summary['protection_conflicts']} 条正式结论存在作品保护冲突。",
            }
        )
    if anomalies:
        items.append({"level": "warning", "message": f"记录到 {len(anomalies)} 条锚点异常。"})
    verify_warnings = reports.get("verify", {}).get("warnings", [])
    if isinstance(verify_warnings, list):
        for warning in verify_warnings[:5]:
            items.append({"level": "warning", "message": str(warning)})
    return items


def workflow_state(
    manifest: dict[str, Any],
    reports: dict[str, Any],
    modules: dict[str, Any],
    operations: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
    review_summary: dict[str, Any],
    verify_current: bool,
    export_current: bool,
) -> dict[str, str]:
    stages = manifest.get("stages", {}) if isinstance(manifest, dict) else {}
    ads_stage = str(stages.get("2_ads", {}).get("status") or "") if isinstance(stages, dict) else ""
    verify_stage = str(stages.get("6_verify", {}).get("status") or "") if isinstance(stages, dict) else ""
    export_stage = str(stages.get("7_export", {}).get("status") or "") if isinstance(stages, dict) else ""
    verify = reports.get("verify", {})
    warnings = verify.get("warnings", []) if isinstance(verify, dict) else []
    warning_count = len(warnings) if isinstance(warnings, list) else 0
    ads_candidate_count = int(modules["ads"].get("candidate_count", 0) or 0)
    ads_decision_count = int(modules["ads"].get("decision_count", 0) or 0)
    if anomalies or warning_count:
        return {
            "key": "needs-review",
            "label": "需要复核",
            "title": "自动处理已暂停",
            "message": "验证发现异常或风险提示。正文版本和操作记录已保留，建议查看处理明细后再决定是否回退或继续。",
        }
    if verify_stage in {"blocked", "incomplete"}:
        return {
            "key": "needs-review",
            "label": "需要复核",
            "title": "当前验证尚未通过",
            "message": "当前验证状态为 blocked 或 incomplete；修复阻止项并重新验证后才能完成。",
        }
    if not modules["ads"].get("scan_current"):
        return {
            "key": "awaiting-agent",
            "label": "等待自动处理",
            "title": "等待当前广告扫描",
            "message": "尚无绑定当前输入的完整广告扫描，历史候选不会计入本次结果。",
        }
    if not modules["ads"].get("decisions_current") or ads_decision_count != ads_candidate_count:
        return {
            "key": "awaiting-agent",
            "label": "等待自动处理",
            "title": "等待 Agent 完成候选判断",
            "message": f"广告候选共 {ads_candidate_count} 条，已生成 {ads_decision_count} 条正式决策。Agent 必须完成完整候选集判断后，才能进入验证和导出。",
        }
    blocker_count = int(review_summary.get("formal_uncertain", 0) or 0) + int(
        review_summary.get("protection_conflicts", 0) or 0
    )
    if blocker_count or int(review_summary.get("validation_issues", 0) or 0):
        return {
            "key": "needs-review",
            "label": "需要复核",
            "title": "自动处理遇到安全阻止项",
            "message": f"Agent 正式结论或验证结果中有 {blocker_count + int(review_summary.get('validation_issues', 0) or 0)} 项需要复核；草稿待判不计入这里。",
        }
    if ads_stage and ads_stage != "done":
        return {
            "key": "pending-verify",
            "label": "等待执行",
            "title": "正式决策已生成，等待执行与验证",
            "message": "Agent 已完成全部候选判断；必须重新执行正式决策并完成验证，旧的验证或导出报告不会被当成本次结果。",
        }
    if export_stage == "done" and export_current:
        return {
            "key": "completed",
            "label": "处理完成",
            "title": "清洗与导出已完成",
            "message": "Agent 已完成自动决策、执行、验证和导出。原文及每一步版本均已保留，可随时查看记录或回退。",
        }
    if verify_stage == "passed" and verify_current:
        return {
            "key": "verified",
            "label": "已验证",
            "title": "清洗已完成，等待导出",
            "message": "Agent 已完成自动决策和验证。当前没有阻止项，可继续生成阅读文件。",
        }
    if ads_stage == "done" or operations:
        return {
            "key": "pending-verify",
            "label": "等待验证",
            "title": "自动决策已生成",
            "message": "Agent 已生成正式决策或写入处理版本；完成验证前不会将本次结果标记为完成。",
        }
    return {
        "key": "awaiting-agent",
        "label": "等待自动处理",
        "title": "等待 Agent 自动决策",
        "message": "候选扫描已完成。调用本 Skill 的 Agent 会继续进行自动判断、执行、验证和导出；无需用户逐条审核。",
    }


def workspace_review(workspace: Path, max_items: int, sample_limit: int) -> dict[str, Any]:
    workspace, _, _ = resolve_workspace_paths(workspace)
    manifest = load_manifest(workspace)
    scans = {
        module: current_scan(workspace, manifest, module)
        for module in MODULES
    }
    decisions = {module: ([], False) for module in MODULES}
    drafts = {module: [] for module in MODULES}
    ad_candidates, ad_scan_report, ad_scan_current = scans["ads"]
    if ad_scan_current:
        decisions["ads"] = current_ad_decisions(
            workspace,
            manifest,
            ad_candidates,
            ad_scan_report,
        )
        drafts["ads"] = current_ad_drafts(
            workspace,
            manifest,
            ad_candidates,
            ad_scan_report,
        )
    operations, anomalies = current_logs(workspace, manifest)

    structure_report = active_stage_report(
        workspace,
        manifest,
        "1_parse_structure",
        {"done"},
    )
    verify_report, verification_trace = current_verification(workspace, manifest)
    export_report, export_current = current_export(
        workspace,
        manifest,
        verification_trace,
    )
    draft_stage = _stage(manifest, "2_ads")
    draft_report: dict[str, Any] = {}
    if draft_stage.get("status") in {"draft_decisions_ready", "formal_decisions_ready", "done"}:
        draft_report_value = draft_stage.get("draft_report")
        if isinstance(draft_report_value, str):
            draft_report = _ledger_json(workspace, manifest, draft_report_value)
    reports = {
        "structure": structure_report,
        "ads": scans["ads"][1],
        "ad_decisions": draft_report,
        "titles": scans["titles"][1],
        "blocked": scans["blocked"][1],
        "verify": verify_report,
        "layout": active_stage_report(workspace, manifest, "5_layout", {"done"}),
        "export": export_report,
    }
    modules = {
        module: module_review(
            workspace,
            module,
            max_items,
            sample_limit,
            scans[module][0],
            drafts[module],
            decisions[module][0],
            operations,
            scans[module][1],
            scans[module][2],
            decisions[module][1],
        )
        for module in MODULES
    }
    review_summary = annotate_review_items(modules, verify_report, anomalies)
    rollback = {"all": rollback_commands(workspace, "ads", "all")}
    if operations:
        rollback["ads_module"] = rollback_commands(workspace, "ads", "module")
    summary = {
        "ads_candidates": modules["ads"]["candidate_count"],
        "ad_draft_delete": int(modules["ads"].get("draft_by_verdict", {}).get("delete", 0) or 0),
        "ad_draft_uncertain": int(modules["ads"].get("draft_by_verdict", {}).get("uncertain", 0) or 0),
        "title_candidates": modules["titles"]["candidate_count"],
        "blocked_candidates": modules["blocked"]["candidate_count"],
        "operations": len(operations),
        "deleted_operations": sum(1 for item in operations if item.get("action") == "delete"),
        "changed_characters": sum(
            abs(len(str(item.get("original") or "")) - len(str(item.get("replacement") or "")))
            for item in operations
        ),
        "formal_decisions": int(modules["ads"].get("decision_count", 0) or 0),
        "formal_uncertain": int(review_summary.get("formal_uncertain", 0) or 0),
        "ads_decision_pending": max(modules["ads"]["candidate_count"] - modules["ads"]["decision_count"], 0),
        "anomalies": len(anomalies),
        "validation_issues": int(review_summary.get("validation_issues", 0) or 0),
        "protection_conflicts": int(review_summary.get("protection_conflicts", 0) or 0),
        "chapter_count": reports["structure"].get("chapter_count", 0),
        "structure_confidence": (reports["structure"].get("structure_confidence") or {}).get("level", "unknown"),
        "fallback_chunks": (reports["structure"].get("fallback_chunking") or {}).get("chunk_count", 0),
    }
    return {
        "workspace": str(workspace),
        "workspace_identity": str(
            (manifest.get("source") or {}).get("sha256")
            if isinstance(manifest.get("source"), dict)
            else ""
        ),
        "name": source_name(workspace, manifest),
        "summary": summary,
        "workflow": workflow_state(
            manifest,
            reports,
            modules,
            operations,
            anomalies,
            review_summary,
            verification_trace is not None,
            export_current,
        ),
        "focus": focus_items(
            workspace,
            modules,
            reports,
            anomalies,
            review_summary,
        ),
        "review_summary": review_summary,
        "rollback": rollback,
        "reports": reports,
        "modules": modules,
        "operations_sample": operations[:max_items],
        "anomalies_sample": anomalies[:max_items],
    }


def _user_state(item: dict[str, Any]) -> dict[str, str]:
    key = str(item.get("workflow", {}).get("key") or "")
    candidates = sum(
        int(item.get("modules", {}).get(module, {}).get("candidate_count", 0) or 0)
        for module in MODULES
    )
    if key == "completed":
        return {
            "key": "completed",
            "label": "已完成",
            "next_action": "打开清洗后的阅读文件；如需抽查，可主动打开候选工作台。",
        }
    if key == "needs-review":
        return {
            "key": "needs-review",
            "label": "需要复核",
            "next_action": "阅读下方首条候选，形成复核请求后交给 Agent 校验并应用。",
        }
    if key == "awaiting-agent" and candidates:
        return {
            "key": "needs-review",
            "label": "需要复核",
            "next_action": "阅读下方当前候选，形成复核请求后交给 Agent 校验并应用。",
        }
    if key in {"awaiting-agent", "pending-verify", "verified"}:
        return {
            "key": "awaiting-agent",
            "label": "等待 Agent 应用",
            "next_action": "把复核请求交给 Agent；网页本身不会修改小说。",
        }
    if not candidates:
        return {
            "key": "inspection-only",
            "label": "仅检查",
            "next_action": "当前没有待处理候选，无需操作。",
        }
    return {
        "key": "stopped",
        "label": "已安全停止",
        "next_action": "查看技术审计中的阻止原因，修复后重新生成页面。",
    }


def _normalized_review_item(raw: dict[str, Any], display_index: int) -> dict[str, Any]:
    item = _redact_executable_review_value(copy.deepcopy(raw))
    if not isinstance(item, dict):  # Defensive: callers promise a mapping.
        raise TypeError("review item must be a mapping")
    candidate_id = str(item.get("candidate_id") or "")
    module = str(item.get("module") or "ads")
    raw_anchors = item.get("anchors") if isinstance(item.get("anchors"), list) else []
    anchors = [
        _review_anchor_projection(anchor)
        for anchor in raw_anchors[:MAX_REVIEW_ANCHORS]
        if isinstance(anchor, dict)
    ]
    item["anchors"] = anchors
    if len(raw_anchors) > MAX_REVIEW_ANCHORS:
        item["anchors_truncated"] = True
    blocking = {
        "protection_conflict": bool(item.get("protection_conflict")),
        "blockers": item.get("blockers") if isinstance(item.get("blockers"), list) else [],
    }
    mutation_guard = item.get("mutation_guard")
    edit_plan_id = item.get("edit_plan_id")
    if not isinstance(edit_plan_id, str) or not edit_plan_id:
        edit_plan_id = None
    eligibility_keys = (
        "delete_allowed",
        "delete_blockers",
        "batch_delete_allowed",
        "batch_delete_blockers",
        "segment_delete_allowed",
        "segment_delete_blockers",
        "segment_support_message",
    )
    if all(key in item for key in eligibility_keys):
        eligibility = {key: copy.deepcopy(item[key]) for key in eligibility_keys}
    else:
        eligibility = _review_delete_eligibility(
            module=module,
            anchors_count=int(item.get("anchors_count", len(raw_anchors)) or 0),
            anchors_truncated=bool(item.get("anchors_truncated")),
            mutation_guard=mutation_guard,
            blocking=blocking,
            edit_plan_id=edit_plan_id,
            edit_plan_validated=item.get("edit_plan_validated") is True,
        )
    fingerprint = item.get("candidate_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        fingerprint = _canonical_sha256(
            {
                "candidate_id": candidate_id,
                "module": module,
                "anchors": [
                    {
                        "anchor_id": anchor.get("anchor_id"),
                        "original": anchor.get("original"),
                    }
                    for anchor in anchors
                    if isinstance(anchor, dict)
                ],
            }
        )
    family_key = str(item.get("family_key") or "")
    group_basis = family_key or f"{module}:{candidate_id}"
    first_anchor = next((anchor for anchor in anchors if isinstance(anchor, dict)), {})
    display_title = _human_excerpt(
        item.get("display_title") or item.get("original"),
        80,
    ) or "未命名候选组"
    plain_reason, _ = bounded_text(
        item.get("plain_reason") or "规则命中此处，请结合上下文复核。",
        240,
        "head",
    )
    family_label, _ = bounded_text(item.get("family_label"), 120, "head")
    item.update(eligibility)
    item.update(
        {
            "candidate_id": candidate_id,
            "candidate_fingerprint": fingerprint,
            "display_index": display_index,
            "mutation_guard": mutation_guard,
            "review_group_id": "RG-" + hashlib.sha256(group_basis.encode("utf-8")).hexdigest()[:16],
            "edit_plan_id": edit_plan_id,
            "edit_plan_sha256": item.get("edit_plan_sha256"),
            "line_number": item.get("line_number") or first_anchor.get("line"),
            "display_title": display_title,
            "plain_reason": plain_reason,
            "family_label": family_label,
            "segment_support_message": eligibility["segment_support_message"],
        }
    )
    return item


def build_review_payload(item: dict[str, Any]) -> dict[str, Any]:
    workspace_identity = item.get("workspace_identity")
    if not isinstance(workspace_identity, str) or len(workspace_identity) != 64:
        workspace_identity = hashlib.sha256(
            str(item.get("workspace") or item.get("name") or "anonymous-workspace").encode("utf-8")
        ).hexdigest()
    modules: dict[str, Any] = {}
    identity_modules: list[dict[str, Any]] = []
    display_index = 0
    for module in MODULES:
        source = item.get("modules", {}).get(module, {})
        normalized: list[dict[str, Any]] = []
        identities: list[dict[str, Any]] = []
        for raw in source.get("review_items", []):
            if not isinstance(raw, dict):
                continue
            display_index += 1
            review_item = _normalized_review_item(raw, display_index)
            normalized.append(review_item)
            identities.append(
                {
                    "candidate_id": review_item["candidate_id"],
                    "candidate_fingerprint": review_item["candidate_fingerprint"],
                    "formal_decision": review_item.get("formal_decision"),
                    "formal_record_sha256": review_item.get("formal_record_sha256"),
                    "draft_record_sha256": review_item.get("draft_record_sha256"),
                    "mutation_guard": review_item.get("mutation_guard"),
                    "edit_plan_id": review_item.get("edit_plan_id"),
                    "edit_plan_sha256": review_item.get("edit_plan_sha256"),
                }
            )
        modules[module] = {"label": MODULE_LABELS[module], "items": normalized}
        identity_modules.append(
            {
                "module": module,
                "scan_id": source.get("scan_id"),
                "candidate_set_sha256": source.get("candidate_set_sha256"),
                "items": identities,
            }
        )
    review_state_id = _canonical_sha256(
        {
            "review_ui_schema": REVIEW_UI_SCHEMA,
            "workspace_identity": workspace_identity,
            "modules": identity_modules,
        }
    )
    return {
        "review_ui_schema": REVIEW_UI_SCHEMA,
        "workspace_identity": workspace_identity,
        "review_state_id": review_state_id,
        "name": item.get("name"),
        "user_state": _user_state(item),
        "review_summary": item.get("review_summary", {}),
        "modules": modules,
        "reason_codes": list(REASON_CODES),
    }


def aggregate(workspaces: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "workspace_count": len(workspaces),
        "ads_candidates": sum(int(item["summary"].get("ads_candidates", 0)) for item in workspaces),
        "ad_draft_delete": sum(int(item["summary"].get("ad_draft_delete", 0)) for item in workspaces),
        "ad_draft_uncertain": sum(int(item["summary"].get("ad_draft_uncertain", 0)) for item in workspaces),
        "title_candidates": sum(int(item["summary"].get("title_candidates", 0)) for item in workspaces),
        "blocked_candidates": sum(int(item["summary"].get("blocked_candidates", 0)) for item in workspaces),
        "operations": sum(int(item["summary"].get("operations", 0)) for item in workspaces),
        "deleted_operations": sum(int(item["summary"].get("deleted_operations", 0)) for item in workspaces),
        "changed_characters": sum(int(item["summary"].get("changed_characters", 0)) for item in workspaces),
        "formal_decisions": sum(int(item["summary"].get("formal_decisions", 0)) for item in workspaces),
        "formal_uncertain": sum(int(item["summary"].get("formal_uncertain", 0)) for item in workspaces),
        "ads_decision_pending": sum(int(item["summary"].get("ads_decision_pending", 0)) for item in workspaces),
        "anomalies": sum(int(item["summary"].get("anomalies", 0)) for item in workspaces),
        "validation_issues": sum(int(item["summary"].get("validation_issues", 0)) for item in workspaces),
        "protection_conflicts": sum(int(item["summary"].get("protection_conflicts", 0)) for item in workspaces),
        "focus_items": sum(len(item.get("focus", [])) for item in workspaces),
    }


def workspace_attention_count(item: dict[str, Any]) -> int:
    summary = item.get("summary", {})
    return sum(
        int(summary.get(key, 0) or 0)
        for key in ("formal_uncertain", "validation_issues", "protection_conflicts", "anomalies")
    )


def order_workspaces_for_display(workspaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
        state = str(item.get("workflow", {}).get("key") or "")
        pending = int(item.get("summary", {}).get("ads_decision_pending", 0) or 0)
        return (
            WORKFLOW_PRIORITY.get(state, 1),
            -workspace_attention_count(item),
            -pending,
            str(item.get("name") or "").casefold(),
        )

    return sorted(workspaces, key=sort_key)


def batch_book_id(workspace: Path) -> str:
    normalized = os.path.normcase(str(Path(workspace).resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def public_book_name(value: Any) -> str:
    name = str(value or "未命名小说").replace("\\", "/").rsplit("/", 1)[-1]
    return name or "未命名小说"


def batch_book_sort_key(book: dict[str, Any]) -> tuple[int, int, int, str, str]:
    risk = book["risk"]
    return (
        int(risk["priority"]),
        -int(risk["attention_count"]),
        -int(risk["pending_count"]),
        str(book["name"]).casefold(),
        str(book["id"]),
    )


def build_batch_books(
    workspaces: list[dict[str, Any]],
    page_sha256s: list[str],
) -> list[dict[str, Any]]:
    if len(workspaces) != len(page_sha256s):
        raise RuntimeError("批量审核子页数量与工作区数量不一致")
    books: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item, page_sha256 in zip(workspaces, page_sha256s, strict=True):
        workspace_value = item.get("workspace")
        if not isinstance(workspace_value, str) or not workspace_value:
            raise RuntimeError("批量审核书目缺少工作区身份")
        book_id = batch_book_id(Path(workspace_value))
        if book_id in seen_ids:
            raise RuntimeError("批量审核书目存在重复的内部安全 ID")
        seen_ids.add(book_id)
        if len(page_sha256) != 64 or any(char not in "0123456789abcdef" for char in page_sha256):
            raise RuntimeError("批量审核子页 SHA-256 无效")
        workflow = item.get("workflow", {})
        state = str(workflow.get("key") or "unknown")
        if state not in WORKFLOW_PRIORITY:
            raise RuntimeError(f"批量审核书目状态无效: {state}")
        summary = item.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}
        attention_count = workspace_attention_count(item)
        pending_count = int(summary.get("ads_decision_pending", 0) or 0)
        books.append(
            {
                "id": book_id,
                "name": public_book_name(item.get("name")),
                "status": {
                    "key": state,
                    "label": str(workflow.get("label") or WORKFLOW_RISK_LABELS.get(state, "未标注")),
                },
                "risk": {
                    "priority": WORKFLOW_PRIORITY.get(state, 1),
                    "label": WORKFLOW_RISK_LABELS.get(state, str(workflow.get("label") or "未标注")),
                    "attention_count": attention_count,
                    "pending_count": pending_count,
                },
                "summary": {
                    key: summary[key]
                    for key in BATCH_BOOK_SUMMARY_KEYS
                    if key in summary
                },
                "html": f"books/{book_id}.html",
                "sha256": page_sha256,
            }
        )
    return sorted(books, key=batch_book_sort_key)


def build_batch_index_data(
    books: list[dict[str, Any]],
    summary: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "template_version": REVIEW_TEMPLATE_VERSION,
        "mode": "batch",
        "run_id": run_id,
        "summary": dict(summary),
        "books": books,
    }


def validate_batch_book_pages(index_path: Path) -> None:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"批量审核索引无法读取: {index_path}") from exc
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or data.get("template_version") != REVIEW_TEMPLATE_VERSION
        or data.get("mode") != "batch"
        or not isinstance(data.get("run_id"), str)
        or not data["run_id"]
    ):
        raise RuntimeError("批量审核索引结构无效")
    allowed_top = {"schema_version", "template_version", "mode", "run_id", "summary", "books"}
    if set(data) != allowed_top:
        raise RuntimeError("批量审核索引包含不允许的字段")
    summary = data.get("summary")
    if (
        not isinstance(summary, dict)
        or not set(summary).issubset(BATCH_AGGREGATE_SUMMARY_KEYS)
        or any(not isinstance(value, int) or isinstance(value, bool) for value in summary.values())
    ):
        raise RuntimeError("批量审核索引摘要结构无效")
    books = data.get("books")
    if not isinstance(books, list) or not books:
        raise RuntimeError("批量审核索引缺少书目")
    root = index_path.resolve().parent
    seen_ids: set[str] = set()
    for book in books:
        if not isinstance(book, dict):
            raise RuntimeError("批量审核书目结构无效")
        if set(book) != {"id", "name", "status", "risk", "summary", "html", "sha256"}:
            raise RuntimeError("批量审核书目包含不允许的字段")
        book_id = book.get("id")
        expected_sha256 = book.get("sha256")
        relative_html = book.get("html")
        name = book.get("name")
        status = book.get("status")
        risk = book.get("risk")
        book_summary = book.get("summary")
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or "\\" in name
            or not isinstance(status, dict)
            or set(status) != {"key", "label"}
            or not isinstance(risk, dict)
            or set(risk) != {"priority", "label", "attention_count", "pending_count"}
            or not isinstance(book_summary, dict)
            or not set(book_summary).issubset(BATCH_BOOK_SUMMARY_KEYS)
            or any(
                (
                    not isinstance(value, str)
                    if key == "structure_confidence"
                    else not isinstance(value, int) or isinstance(value, bool)
                )
                for key, value in book_summary.items()
            )
        ):
            raise RuntimeError("批量审核书目目录结构无效")
        state = status.get("key")
        if (
            state not in WORKFLOW_PRIORITY
            or risk.get("priority") != WORKFLOW_PRIORITY[state]
            or risk.get("label") != WORKFLOW_RISK_LABELS[state]
            or any(
                not isinstance(risk.get(key), int)
                or isinstance(risk.get(key), bool)
                or int(risk[key]) < 0
                for key in ("priority", "attention_count", "pending_count")
            )
            or not isinstance(status.get("label"), str)
            or not status["label"]
        ):
            raise RuntimeError("批量审核书目状态与风险不一致")
        if (
            not isinstance(book_id, str)
            or len(book_id) != 64
            or any(char not in "0123456789abcdef" for char in book_id)
            or book_id in seen_ids
        ):
            raise RuntimeError("批量审核书目内部安全 ID 无效或重复")
        seen_ids.add(book_id)
        expected_relative = f"books/{book_id}.html"
        if relative_html != expected_relative:
            raise RuntimeError(f"批量审核子页链接不安全: {relative_html!r}")
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256)
        ):
            raise RuntimeError(f"批量审核子页 SHA-256 无效: {expected_relative}")
        child = index_path.parent / "books" / f"{book_id}.html"
        resolved_child = child.resolve()
        try:
            resolved_child.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"批量审核子页链接越界: {expected_relative}") from exc
        if not child.is_file():
            raise RuntimeError(f"批量审核子页缺失: {expected_relative}")
        if child.stat().st_size == 0:
            raise RuntimeError(f"批量审核子页为空: {expected_relative}")
        if sha256_file(child) != expected_sha256:
            raise RuntimeError(f"批量审核子页 SHA-256 不匹配: {expected_relative}")
    expected_order = sorted(books, key=batch_book_sort_key)
    if [book["id"] for book in books] != [book["id"] for book in expected_order]:
        raise RuntimeError("批量审核书目未按风险排序")


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def display_path(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def localized(value: Any) -> str:
    text = "" if value is None else str(value)
    if "+" in text:
        return "、".join(localized(part) for part in text.split("+"))
    return VALUE_LABELS.get(text, text)


def localized_group(value: Any) -> str:
    parts = str(value or "").split(" / ")
    return " · ".join(localized(part) for part in parts if part)


def draft_label(value: Any) -> str:
    return {
        "delete": "草稿建议删除",
        "keep": "草稿建议保留",
        "uncertain": "草稿待判",
        "no-draft": "未生成草稿",
        "unknown": "未生成草稿",
    }.get(str(value or "unknown"), str(value))


def formal_label(value: Any) -> str:
    return {
        "delete": "正式删除",
        "keep": "正式保留",
        "uncertain": "正式未决",
        "unknown": "尚无正式结论",
    }.get(str(value or "unknown"), str(value))


def render_metric_cards(metrics: dict[str, Any], keys: tuple[str, ...] | None = None) -> str:
    cards = []
    selected = keys or tuple(metrics.keys())
    for key in selected:
        if key not in metrics:
            continue
        value = metrics[key]
        zero_class = " metric-zero" if isinstance(value, (int, float)) and value == 0 else ""
        cards.append(
            f"<div class='metric metric-{esc(key)}{zero_class}'><span>{esc(METRIC_LABELS.get(key, key))}</span>"
            f"<strong>{esc(localized(value))}</strong></div>"
        )
    return "\n".join(cards)


def render_focus(items: list[dict[str, str]]) -> str:
    if not items:
        return "<p class='empty-state'>当前没有需要额外处理的异常。</p>"
    rows = []
    for item in items:
        level = {"review": "需要复核", "warning": "需要注意", "info": "处理信息"}.get(
            str(item.get("level")), "处理信息"
        )
        rows.append(f"<li><span class='focus-level'>{esc(level)}</span>{esc(item.get('message'))}</li>")
    return "<ul class='focus-list'>" + "\n".join(rows) + "</ul>"


SHELL_LABELS = {"powershell": "PowerShell", "posix": "POSIX shell"}


def render_copyable_command(command: Any, label: str = "回退", shell: str | None = None) -> str:
    if not command:
        return ""
    shell_label = SHELL_LABELS.get(str(shell), "")
    suffix = f"（{shell_label}）" if shell_label else ""
    shell_html = f"<span class='command-shell'>{esc(shell_label)}</span>" if shell_label else ""
    return (
        "<div class='command-row'>"
        f"{shell_html}"
        f"<code data-command-text>{esc(command)}</code>"
        f"<button type='button' class='quiet-button' data-copy-command aria-label='复制{esc(label)}命令{esc(suffix)}'>"
        f"复制{esc(label)}命令{esc(suffix)}</button>"
        "<span class='copy-status' role='status' aria-live='polite'></span>"
        "</div>"
    )


def render_copyable_commands(commands: Any, label: str) -> str:
    if not isinstance(commands, dict):
        return ""
    return "".join(
        render_copyable_command(commands.get(shell), label, shell)
        for shell in ("powershell", "posix")
    )


def render_rollback_cell(sample: dict[str, Any]) -> str:
    if not sample.get("operation_action"):
        return "<span class='muted'>—</span>"
    commands = sample.get("rollback", {}).get("point", {})
    return (
        "<details class='inline-details'><summary>查看恢复方式</summary>"
        f"{render_copyable_commands(commands, '候选回退')}</details>"
    )


def render_group(group: dict[str, Any], module: str) -> str:
    rows = []
    report_only = module != "ads"
    show_rollback = not report_only and any(
        sample.get("operation_action") for sample in group.get("samples", [])
    )
    for sample in group.get("samples", []):
        draft = "report-only" if report_only else sample.get("draft_verdict") or "no-draft"
        formal = "unchanged" if report_only else sample.get("formal_decision") or "unknown"
        draft_text = "只报告" if report_only else draft_label(draft)
        formal_text = "原文不变" if report_only else formal_label(formal)
        rollback_cell = f"<td>{render_rollback_cell(sample)}</td>" if show_rollback else ""
        rows.append(
            "<tr>"
            f"<td class='candidate-id'>{esc(sample.get('candidate_id'))}</td>"
            f"<td><span class='status status-{esc(draft)}'>{esc(draft_text)}</span></td>"
            f"<td><span class='status status-{esc(formal)}'>{esc(formal_text)}</span></td>"
            f"<td>{esc(localized(sample.get('risk')))}</td>"
            f"<td><pre>{esc(sample.get('original'))}</pre></td>"
            f"{rollback_cell}"
            "</tr>"
        )
    column_count = 6 if show_rollback else 5
    rows_html = (
        "\n".join(rows)
        if rows
        else f"<tr><td class='empty-state' colspan='{column_count}'>该分组暂无候选。</td></tr>"
    )
    rollback_header = "<th>恢复</th>" if show_rollback else ""
    status_headers = (
        "<th>扫描状态</th><th>处理边界</th>"
        if report_only
        else "<th>脚本草稿</th><th>Agent 正式结论</th>"
    )
    return f"""
<details class='group'>
<summary><span>{esc(localized_group(group.get('group')))}</span><b>{esc(group.get('count'))} 个候选 · {esc(group.get('anchors_count', 0))} 个位置</b></summary>
<div class='table-wrap'>
<table>
<thead><tr><th>编号</th>{status_headers}<th>风险</th><th>候选原文</th>{rollback_header}</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div>
</details>
"""


def render_rollback_summary(item: dict[str, Any]) -> str:
    operation_count = int(item.get("summary", {}).get("operations", 0) or 0)
    if not operation_count:
        return ""
    message = (
        f"已记录 {operation_count} 项正文操作；可按模块、章节或单个候选恢复。"
    )
    labels = {
        "all": "全部回退",
        "ads_module": "广告模块回退",
    }
    commands = "".join(
        render_copyable_commands(shell_commands, labels.get(key, "回退"))
        for key, shell_commands in item.get("rollback", {}).items()
        if shell_commands
    )
    return f"""
<aside class='rollback-panel'>
<p class='eyebrow'>版本安全</p>
<h3>回退与追溯</h3>
<p>{esc(message)}</p>
<details>
<summary>高级：查看并复制回退命令</summary>
<p class='command-note'>同时提供 PowerShell 与 POSIX shell 命令；复制后请在对应 shell 和本项目根目录执行，并先核对工作区路径和影响范围。</p>
<div class='command-list'>{commands}</div>
</details>
</aside>
"""


def result_metric_keys(item: dict[str, Any]) -> tuple[str, ...]:
    state = str(item.get("workflow", {}).get("key") or "")
    if state == "awaiting-agent":
        return ("ads_candidates", "ads_decision_pending", "title_candidates", "blocked_candidates")
    if state == "needs-review":
        return ("formal_uncertain", "validation_issues", "anomalies", "protection_conflicts")
    return ("formal_decisions", "deleted_operations", "changed_characters", "anomalies")


def render_completion_panel(item: dict[str, Any]) -> str:
    workflow = item.get("workflow", {})
    summary = item.get("summary", {})
    export_report = item.get("reports", {}).get("export", {})
    output_dir = export_report.get("output_dir_abs") or export_report.get("output_dir")
    output_html = f"<p class='output-location'>阅读文件已导出至：{esc(output_dir)}</p>" if output_dir else ""
    return f"""
<section class='completion-panel completion-{esc(workflow.get('key'))}'>
  <div class='completion-heading'>
    <div><p class='eyebrow'>自动处理状态</p><h3>{esc(workflow.get('title'))}</h3></div>
    <span class='workflow-status state-badge state-{esc(workflow.get('key'))}'>{esc(workflow.get('label'))}</span>
  </div>
  <p>{esc(workflow.get('message'))}</p>
  <div class='completion-metrics'>{render_metric_cards(summary, result_metric_keys(item))}</div>
  {output_html}
</section>
"""


def render_exception_review(item: dict[str, Any], index: int) -> str:
    review_summary = item.get("review_summary", {})
    payload = build_review_payload(item)
    payload_json = json_for_html_script(payload)
    bounded_excerpt = any(
        bool(review_item.get("excerpt_truncated"))
        or bool(review_item.get("anchors_truncated"))
        or bool(review_item.get("metadata_truncated"))
        or any(
            isinstance(anchor, dict) and bool(anchor.get("truncated"))
            for anchor in review_item.get("anchors", [])
        )
        for module in MODULES
        for review_item in item["modules"][module].get("review_items", [])
    )
    bounded_notice = (
        "<p class='review-alert'><b>网页仅展示有界摘录</b><span>完整候选、证据与锚点请以工作区 candidates/ 和 decisions/ 为准。</span></p>"
        if bounded_excerpt
        else ""
    )
    issue_parts = []
    for key, label in (
        ("formal_uncertain", "正式未决"),
        ("validation_issues", "验证残留"),
        ("protection_conflicts", "保护冲突"),
    ):
        count = int(review_summary.get(key, 0) or 0)
        if count:
            issue_parts.append(f"{label} {count}")
    anomaly_count = int(item.get("summary", {}).get("anomalies", 0) or 0)
    if anomaly_count:
        issue_parts.append(f"锚点异常 {anomaly_count}")
    issue_html = (
        f"<div class='review-alert'><b>本次需要复核</b><span>{esc(' · '.join(issue_parts))}</span></div>"
        if issue_parts
        else ""
    )
    body = f"""
<section class='exception-review' aria-labelledby='review-title-{index}'>
<div class='review-heading'>
  <div><p class='eyebrow'>当前任务</p><h3 id='review-title-{index}'>阅读候选并形成复核请求</h3></div>
  <p class='muted'>网页不会修改小说；Agent 会重新读取当前 ledger、校验身份并 dry-run。</p>
</div>
{issue_html}
{bounded_notice}
<div class='review-results' data-review-results tabindex='-1' aria-label='候选工作台'></div>
<div class='review-toolbar'>
  <label>模块<select data-review-module><option value='all'>全部模块</option>{''.join(f"<option value='{module}'>{MODULE_LABELS[module]}</option>" for module in MODULES)}</select></label>
  <label>范围<select data-review-scope><option value='review'>仅需复核</option><option value='all'>全部候选（主动查看）</option></select></label>
  <label>状态<select data-review-status><option value='all'>全部状态</option><option value='formal:uncertain'>正式未决</option><option value='formal:delete'>正式删除</option><option value='formal:keep'>正式保留</option><option value='no-formal'>尚无正式结论</option><option value='draft:delete'>草稿建议删除</option><option value='draft:uncertain'>草稿待判</option><option value='draft:keep'>草稿建议保留</option></select></label>
  <label class='review-search'>搜索<input type='search' data-review-search placeholder='编号、章节、原文或信号'></label>
</div>
<div class='review-summary'><span data-review-count role='status' aria-live='polite'></span><span data-review-selected role='status' aria-live='polite'>未提出调整</span></div>
<div class='review-batchbar'>
  <button type='button' class='quiet-button' data-review-check-visible>勾选当前页</button>
  <button type='button' class='quiet-button' data-review-batch='keep'>批量保留</button>
  <button type='button' class='quiet-button' data-review-batch='uncertain'>批量暂不判断</button>
  <button type='button' class='danger-button' data-review-batch='delete' disabled>批量删除</button>
  <label class='batch-note'>批量说明（暂不判断或推翻正式结论时必填）<textarea maxlength='500' rows='2' data-review-batch-note placeholder='说明共同的上下文或判断依据'></textarea></label>
  <span class='muted' data-review-batch-status></span>
</div>
<div class='review-footer'>
  <button type='button' data-review-copy>复制复核请求 JSON</button>
  <button type='button' class='quiet-button' data-review-export-progress>导出进度 JSON</button>
  <label class='file-button'>导入进度 JSON<input type='file' accept='application/json,.json' data-review-import-progress></label>
  <button type='button' class='quiet-button' data-review-clear>清除全部请求</button>
  <span class='copy-status' data-review-copy-status role='status' aria-live='polite'></span>
</div>
<script type='application/json' class='review-payload'>{payload_json}</script>
</section>
"""
    candidate_count = sum(
        len(payload["modules"][module]["items"])
        for module in MODULES
    )
    if item.get("workflow", {}).get("key") == "completed" and candidate_count == 0:
        return f"<details class='completed-inspection'><summary>主动复核</summary>{body}</details>"
    return body


def render_processing_info(item: dict[str, Any]) -> str:
    summary = item.get("summary", {})
    ads_summary = item.get("reports", {}).get("ads", {}).get("summary", {})
    performance = ads_summary.get("performance", {}) if isinstance(ads_summary, dict) else {}
    l2 = performance.get("l2", {}) if isinstance(performance, dict) else {}
    timings = performance.get("timings_seconds", {}) if isinstance(performance, dict) else {}
    secondary = item.get("reports", {}).get("ad_decisions", {}).get("secondary_review", {})
    rows = [
        ("工作区", item.get("workspace")),
        ("识别章节", summary.get("chapter_count", 0)),
        ("定位分块", summary.get("fallback_chunks", 0)),
    ]
    if l2:
        scope = {"boundary": "章节边界快速扫描", "all": "全文深度扫描", "fallback_all": "fallback 全文扫描"}.get(
            str(l2.get("scope")), str(l2.get("scope"))
        )
        rows.append(("近似重复范围", f"{scope}：{l2.get('selected_blocks', 0)} / {l2.get('eligible_blocks', 0)} 段"))
    if timings.get("total") is not None:
        rows.append(("广告扫描耗时", f"{float(timings['total']):.2f} 秒"))
    if isinstance(secondary, dict) and secondary:
        family_count = secondary.get("cluster_count", secondary.get("family_count"))
        if family_count is not None:
            rows.append(("广告家族", family_count))
        elapsed = secondary.get("elapsed_seconds", secondary.get("duration_seconds"))
        if elapsed is not None:
            rows.append(("二次判定耗时", f"{float(elapsed):.3f} 秒"))
    rendered_rows = "\n".join(f"<dt>{esc(label)}</dt><dd>{esc(value)}</dd>" for label, value in rows)
    return f"""
<details class='processing-info'>
<summary>处理信息</summary>
<dl>{rendered_rows}</dl>
</details>
"""


def render_batch_index_html(data: dict[str, Any], title: str) -> str:
    review_css, _ = load_review_assets()
    rows = []
    for book in data.get("books", []):
        status = book.get("status", {})
        risk = book.get("risk", {})
        summary = book.get("summary", {})
        detail_parts = []
        attention_count = int(risk.get("attention_count", 0) or 0)
        pending_count = int(risk.get("pending_count", 0) or 0)
        if attention_count:
            detail_parts.append(f"{attention_count} 项阻止或异常")
        if pending_count:
            detail_parts.append(f"{pending_count} 条广告候选待正式判断")
        if not detail_parts:
            detail_parts.append(str(risk.get("label") or "当前没有额外风险说明。"))
        rows.append(
            f"""
<li class='batch-book batch-state-{esc(status.get('key'))}'>
  <div class='batch-book-main'>
    <div><span class='batch-book-name'>{esc(book.get('name'))}</span><span class='state-badge state-{esc(status.get('key'))}'>{esc(status.get('label'))}</span></div>
    <p>{esc(' · '.join(detail_parts))}</p>
    <div class='module-metrics'>{render_metric_cards(summary, ('ads_candidates', 'formal_uncertain', 'deleted_operations', 'anomalies'))}</div>
  </div>
  <a class='batch-open' href='{esc(book.get('html'))}'>查看本书结果</a>
</li>
"""
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="cml-review-template-version" content="{REVIEW_TEMPLATE_VERSION}">
<title>{esc(title)}</title>
<style>
{review_css}
</style>
</head>
<body>
<main class='page-shell'>
<header class='page-header'>
  <div><p class='eyebrow'>CML 小说清洗</p><h1>{esc(title)}</h1><p class='header-copy'>按风险优先查看每本小说的独立离线结果。</p></div>
</header>
<section class='overview'><p class='eyebrow'>批量处理概览</p><div class='metrics'>{render_metric_cards(data.get('summary', {}), (
    'workspace_count', 'formal_uncertain', 'ads_decision_pending', 'anomalies', 'deleted_operations', 'changed_characters',
))}</div></section>
<section class='batch-home' aria-labelledby='batch-home-title'>
  <div class='batch-heading'>
    <div><p class='eyebrow'>批量处理入口</p><h2 id='batch-home-title'>小说列表（风险优先）</h2></div>
    <p>每本小说使用独立、自包含的离线页面；首页不载入候选正文和审计明细。</p>
  </div>
  <ol class='batch-book-list'>{''.join(rows)}</ol>
</section>
</main>
</body>
</html>
"""


def render_workspace(item: dict[str, Any], expanded: bool, index: int) -> str:
    modules = item["modules"]
    module_blocks = []
    for module in MODULES:
        groups = "\n".join(
            render_group(group, module) for group in modules[module].get("groups", [])
        )
        module_metrics = {"candidates": modules[module].get("candidate_count", 0)}
        module_boundary = ""
        if module == "ads":
            module_metrics.update(
                {
                    "drafts": modules[module].get("draft_count", 0),
                    "decisions": modules[module].get("decision_count", 0),
                }
            )
        else:
            module_boundary = "<p class='module-boundary'>本版本严格只报告；原文保持不变。</p>"
        module_blocks.append(
            f"""
<section class='review-section' id='workspace-{index}-{module}'>
<div class='section-heading'>
  <div><p class='eyebrow'>审核模块</p><h3>{esc(MODULE_LABELS[module])}</h3></div>
  <div class='section-count'>{esc(modules[module].get('candidate_count', 0))} 条候选</div>
</div>
{module_boundary}
<div class='module-metrics'>{render_metric_cards(module_metrics)}</div>
{groups or "<p class='empty-state'>该模块暂未发现候选。</p>"}
</section>
"""
        )
    summary = item.get("summary", {})
    user_state = _user_state(item)
    support_panels = []
    if item.get("focus"):
        support_panels.append(
            f"<section class='focus-panel'><p class='eyebrow'>处理说明</p><h3>风险与注意事项</h3>{render_focus(item['focus'])}</section>"
        )
    rollback_panel = render_rollback_summary(item)
    if rollback_panel:
        support_panels.append(rollback_panel)
    support_html = f"<div class='workspace-grid'>{''.join(support_panels)}</div>" if support_panels else ""
    return f"""
<section class='workspace' id='workspace-{index}'>
<div class='workspace-hero'>
  <div>
    <h2>《{esc(Path(str(item.get('name') or '')).stem)}》</h2>
    <p class='hero-copy'><b>下一步：</b>{esc(user_state.get('next_action'))}</p>
  </div>
  <span class='state-badge state-{esc(user_state.get('key'))}'>{esc(user_state.get('label'))}</span>
</div>
{render_exception_review(item, index)}
<details class='audit-details'>
<summary>技术审计与处理明细</summary>
<p class='muted'>内部阶段、风险指标、ADF/AD 追踪号、精确锚点和回退命令集中在这里。</p>
{render_completion_panel(item)}
<div class='primary-metrics'>{render_metric_cards(summary, (
    'deleted_operations', 'changed_characters', 'formal_decisions', 'ads_decision_pending', 'anomalies', 'ads_candidates',
))}</div>
{support_html}
{render_processing_info(item)}
{''.join(module_blocks)}
</details>
</section>
"""


def render_html(data: dict[str, Any], title: str) -> str:
    review_css, review_script = load_review_assets()
    raw_workspaces = data.get("workspaces", [])
    if not isinstance(raw_workspaces, list) or len(raw_workspaces) != 1:
        raise ValueError("single-book review HTML requires exactly one workspace")
    workspaces = raw_workspaces
    workspace_html = "\n".join(
        render_workspace(item, expanded=len(workspaces) == 1, index=index)
        for index, item in enumerate(workspaces, 1)
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="cml-review-template-version" content="{REVIEW_TEMPLATE_VERSION}">
<title>{esc(title)}</title>
<style>
{review_css}
</style>
</head>
<body>
<main class='page-shell'>
<header class='page-header'>
  <div><p class='eyebrow'>CML 小说清洗</p><h1>{esc(title)}</h1><p class='header-copy'>先读候选正文，再形成给 Agent 的复核请求。</p></div>
</header>
{workspace_html}
</main>
<script>
{review_script}
</script>
</body>
</html>
"""


def rollback_lines_for_workspace(item: dict[str, Any]) -> list[str]:
    lines = [f"## {item['name']}", "", f"工作区：`{item['workspace']}`", "", "### 模块回退", ""]
    for commands in item.get("rollback", {}).values():
        if not isinstance(commands, dict):
            continue
        for shell in ("powershell", "posix"):
            command = commands.get(shell)
            if command:
                lines.append(f"- {SHELL_LABELS[shell]}：`{command}`")
    lines.extend(["", "### 候选回退示例", ""])
    added = 0
    for detail in item["modules"]["ads"].get("details", []):
        point = detail.get("rollback", {}).get("point")
        chapter = detail.get("rollback", {}).get("chapter")
        if isinstance(point, dict):
            for shell in ("powershell", "posix"):
                command = point.get(shell)
                if command:
                    lines.append(
                        f"- `{detail.get('candidate_id')}`（{SHELL_LABELS[shell]}）：`{command}`"
                    )
            added += 1
        if isinstance(chapter, dict):
            for shell in ("powershell", "posix"):
                command = chapter.get(shell)
                if command:
                    lines.append(
                        f"- `{detail.get('candidate_id')}` 所在章节（{SHELL_LABELS[shell]}）：`{command}`"
                    )
        if added >= 20:
            break
    if added == 0:
        lines.append("- 暂无可用的候选回退示例。")
    lines.append("")
    return lines


def write_rollback_guide(path: Path, data: dict[str, Any]) -> None:
    lines = ["# 回退指南", "", "命令仅用于高级恢复操作；执行前请核对工作区和影响范围。", ""]
    for workspace in data.get("workspaces", []):
        lines.extend(rollback_lines_for_workspace(workspace))
    write_utf8(path, "\n".join(lines))


def output_dir_for(paths: list[Path], workspaces: list[Path], output_dir: str | None) -> Path:
    if output_dir:
        return resolve_external_output_dir(output_dir, workspaces=workspaces)
    if len(workspaces) == 1 and is_workspace(workspaces[0]):
        _, _, writes = resolve_workspace_paths(
            workspaces[0],
            writes={"report_dir": "report"},
        )
        return writes["report_dir"]
    first = paths[0].resolve()
    return resolve_external_output_dir(
        (first if first.is_dir() else first.parent) / "review_report",
        workspaces=workspaces,
    )


def run(
    paths: list[Path],
    output_dir: str | None,
    recursive: bool,
    max_items: int,
    sample_limit: int,
) -> dict[str, Any]:
    discovered = discover_workspaces(paths, recursive)
    with ExitStack() as stack:
        for workspace in sorted(discovered, key=lambda path: os.path.normcase(str(path))):
            stack.enter_context(workspace_transaction_lock(workspace))
        return _run_locked(paths, output_dir, max_items, sample_limit, discovered)


def _run_locked(
    paths: list[Path],
    output_dir: str | None,
    max_items: int,
    sample_limit: int,
    discovered: list[Path],
) -> dict[str, Any]:
    preflighted: list[tuple[Path, tuple[Path, ...]]] = []
    workspace_data: list[dict[str, Any]] = []
    for workspace in discovered:
        with workspace_transaction_lock(workspace):
            preflighted_workspace, inputs = preflight_workspace_review(workspace)
            preflighted.append((preflighted_workspace, inputs))
            workspace_data.append(workspace_review(preflighted_workspace, max_items, sample_limit))
    workspaces = [workspace for workspace, _ in preflighted]
    input_paths = [path for _, inputs in preflighted for path in inputs]
    out_dir = output_dir_for(paths, workspaces, output_dir)

    batch_mode = len(workspaces) > 1
    book_keys: list[str] = []
    if not batch_mode:
        html_name = "review.html"
        data_name = "review_data.json"
    else:
        html_name = "review_index.html"
        data_name = "review_index.json"
    output_values = {
        "html": html_name,
        "data": data_name,
        "rollback": "rollback_guide.md",
    }
    if batch_mode:
        book_ids = [batch_book_id(workspace) for workspace in workspaces]
        if len(set(book_ids)) != len(book_ids):
            raise RuntimeError("批量审核工作区存在重复的内部安全 ID")
        for index, book_id in enumerate(book_ids, 1):
            key = f"book_{index}"
            book_keys.append(key)
            output_values[key] = f"books/{book_id}.html"
    internal_output = output_dir is None and len(workspaces) == 1 and is_workspace(workspaces[0])
    if internal_output:
        relative_values = {
            name: (out_dir / value).relative_to(workspaces[0]).as_posix()
            for name, value in output_values.items()
        }
        _, _, output_paths = resolve_workspace_paths(
            workspaces[0],
            reads={
                f"input_{index}": path.relative_to(workspaces[0]).as_posix()
                for index, path in enumerate(input_paths, 1)
            },
            writes=relative_values,
        )
    else:
        out_dir = resolve_external_output_dir(out_dir, workspaces=workspaces)
        output_paths = resolve_external_output_paths(
            out_dir,
            writes=output_values,
            workspaces=workspaces,
            inputs=input_paths,
        )

    summary = aggregate(workspace_data)
    data: dict[str, Any] = {}
    books: list[dict[str, Any]] = []
    html_path = output_paths["html"]
    data_path = output_paths["data"]
    rollback_path = output_paths["rollback"]
    guide_data = {"workspaces": workspace_data}

    def write_bundle(staged: dict[str, Path], run_id: str) -> None:
        nonlocal books, data
        if batch_mode:
            page_sha256s = []
            for item, key in zip(workspace_data, book_keys, strict=True):
                child_data = {
                    "mode": "single",
                    "run_id": run_id,
                    "summary": item["summary"],
                    "workspaces": [item],
                }
                title = f"{item.get('name') or '未命名小说'} · 小说清洗结果"
                write_utf8(staged[key], render_html(child_data, title))
                page_sha256s.append(sha256_file(staged[key]))
            books = build_batch_books(workspace_data, page_sha256s)
            data = build_batch_index_data(books, summary, run_id)
            write_json(staged["data"], data)
            write_utf8(staged["html"], render_batch_index_html(data, "小说清洗结果"))
            write_rollback_guide(staged["rollback"], guide_data)
            validate_batch_book_pages(staged["data"])
            return

        data = {
            "mode": "single",
            "output_dir": str(out_dir),
            "run_id": run_id,
            "summary": summary,
            "workspaces": workspace_data,
        }
        write_json(staged["data"], data)
        write_utf8(staged["html"], render_html(data, "小说清洗结果"))
        write_rollback_guide(staged["rollback"], guide_data)

    stage_details = {
        "html": display_path(html_path, workspaces[0]) if len(workspaces) == 1 else str(html_path),
        "data": display_path(data_path, workspaces[0]) if len(workspaces) == 1 else str(data_path),
        "rollback_guide": (
            display_path(rollback_path, workspaces[0])
            if len(workspaces) == 1
            else str(rollback_path)
        ),
    }
    if internal_output:
        with WorkspaceTransaction(workspaces[0]) as transaction:
            staged = {
                "data": transaction.stage_path(data_path),
                "rollback": transaction.stage_path(rollback_path),
                "html": transaction.stage_path(html_path),
            }
            write_bundle(staged, transaction.run_id)
            transaction.commit({"review": ("done", stage_details)})
    else:
        with ExternalDeliveryTransaction(
            out_dir,
            workspaces=workspaces,
            inputs=input_paths,
        ) as delivery:
            staged = {
                "data": delivery.stage_path(data_path),
                "rollback": delivery.stage_path(rollback_path),
                "html": delivery.stage_path(html_path),
            }
            for key in book_keys:
                staged[key] = delivery.stage_path(output_paths[key])
            write_bundle(staged, delivery.run_id)
            commits = tuple((workspace, "review", "done") for workspace in workspaces)
            with ExitStack() as stack:
                transactions = [
                    stack.enter_context(WorkspaceTransaction(workspace, run_id=delivery.run_id))
                    for workspace in workspaces
                ]
                delivery.publish(commits=commits)
                if batch_mode:
                    validate_batch_book_pages(data_path)
                books_by_id = {book["id"]: book for book in books}
                for workspace, transaction in zip(workspaces, transactions, strict=True):
                    details = stage_details
                    if batch_mode:
                        book = books_by_id[batch_book_id(workspace)]
                        details = {
                            **stage_details,
                            "book_id": book["id"],
                            "book_html": book["html"],
                            "book_sha256": book["sha256"],
                        }
                    transaction.commit(
                        {"review": ("done", details)},
                        defer_cleanup=True,
                        group_commits=commits,
                    )
                for transaction in transactions:
                    transaction.finalize()
                delivery.finalize()

    result = {
        "mode": data["mode"],
        "workspace_count": len(workspaces),
        "html": str(html_path),
        "data": str(data_path),
        "rollback_guide": str(rollback_path),
        "summary": data["summary"],
    }
    if batch_mode:
        result["books"] = books
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build offline review and rollback helper HTML.")
    parser.add_argument("paths", nargs="+", help="One or more .cleanwork directories or parent directories.")
    parser.add_argument("--output-dir", help="Output directory. Defaults to workspace/report for one workspace.")
    parser.add_argument("--no-recursive", action="store_true", help="Do not search parent directories recursively.")
    parser.add_argument("--max-items", type=int, default=80, help="Maximum detail candidates per module per workspace.")
    parser.add_argument("--sample-limit", type=int, default=3, help="Samples shown per group.")
    args = parser.parse_args()

    report = run(
        paths=[Path(path) for path in args.paths],
        output_dir=args.output_dir,
        recursive=not args.no_recursive,
        max_items=args.max_items,
        sample_limit=args.sample_limit,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
