from __future__ import annotations

import argparse
import html
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import build_review_html
import export_outputs
from common import (
    ExternalDeliveryTransaction,
    JOB_INPUT_DIR_NAME,
    JOB_RESULT_DIR_NAME,
    SKILL_PUBLIC_ROOT,
    WorkspaceTransaction,
    load_manifest,
    load_jsonl,
    load_jsonl_for_run,
    now_iso,
    portable_path_segment,
    read_utf8,
    resolve_current_head,
    resolve_external_output_dir,
    resolve_external_output_paths,
    resolve_in_workspace,
    resolve_workspace_paths,
    sha256_file,
    source_identity_id,
    workspace_transaction_lock,
    write_json,
    write_utf8,
)
from normalize_layout import load_config


RESULT_SCHEMA = "cml.result.v1"
LATEST_SCHEMA = "cml.latest.v1"
ATTEMPT_SCHEMA = "cml.delivery-attempt.v1"
TERMINAL_STATUSES = frozenset(
    {"completed", "needs_review", "blocked", "incomplete", "report_only"}
)
DELIVERY_ID_RE = re.compile(r"\A\d{8}-\d{6}-\d{6}Z-[0-9a-f]{8}\Z")
REVIEW_NAME = "01_查看结果_Review.html"
RESULT_NAME = "03_处理摘要_Result.json"
START_NAME = "00_从这里开始_Start-Here.html"
LATEST_NAME = "latest.json"
FORMAT_NAMES = {
    "txt": "02_清洗后_Cleaned.txt",
    "markdown": "02_清洗后_Cleaned.md",
    "epub": "02_清洗后_Cleaned.epub",
}


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _stage(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    stages = manifest.get("stages")
    value = stages.get(name) if isinstance(stages, dict) else None
    return value if isinstance(value, dict) else {}


def _manifest_source(manifest: dict[str, Any]) -> dict[str, Any]:
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise ValueError("workspace manifest has no source identity")
    required = ("path", "name", "sha256", "size_bytes")
    if any(source.get(key) is None for key in required):
        raise ValueError("workspace manifest source identity is incomplete")
    return source


def source_matches_v0(workspace: Path, manifest: dict[str, Any]) -> bool:
    source = _manifest_source(manifest)
    path = Path(str(source["path"])).resolve(strict=False)
    if not path.is_file():
        return False
    v0 = workspace / "versions/v0_original.txt"
    return (
        path.stat().st_size == source["size_bytes"]
        and sha256_file(path) == source["sha256"]
        and sha256_file(v0) == source["sha256"]
    )


def source_id(manifest: dict[str, Any]) -> str:
    source = _manifest_source(manifest)
    return source_identity_id(str(source["sha256"]), Path(str(source["path"])))


def default_delivery_root(manifest: dict[str, Any]) -> Path:
    source = Path(str(_manifest_source(manifest)["path"])).resolve(strict=False)
    if source.parent.name == JOB_INPUT_DIR_NAME:
        return source.parent.parent / JOB_RESULT_DIR_NAME
    return source.parent / JOB_RESULT_DIR_NAME


def resolve_delivery_root(
    workspace: Path,
    manifest: dict[str, Any],
    value: Path | None,
) -> Path:
    root = Path(value).resolve(strict=False) if value is not None else default_delivery_root(manifest)
    public_root = SKILL_PUBLIC_ROOT.resolve(strict=False)
    if _is_relative_to(root, public_root):
        raise ValueError("new delivery roots must be outside the Skill public root")
    return resolve_external_output_dir(root, workspaces=(workspace,))


def delivery_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _artifact_record(final_path: Path, staged_path: Path) -> dict[str, Any]:
    """Describe staged bytes using their immutable final public path."""

    if not staged_path.is_file():
        raise ValueError("delivery artifact was not staged as a regular file")
    return {
        "path": str(final_path),
        "sha256": sha256_file(staged_path),
        "size_bytes": staged_path.stat().st_size,
    }


def _bind_artifact_to_delivery(
    record: dict[str, Any], *, source_id_value: str, identifier: str
) -> dict[str, Any]:
    bound = dict(record)
    bound["source_id"] = source_id_value
    bound["delivery_id"] = identifier
    return bound


def _validate_delivery_artifact_binding(
    record: object, *, source_id_value: str, identifier: str, label: str
) -> None:
    if (
        not isinstance(record, dict)
        or record.get("source_id") != source_id_value
        or record.get("delivery_id") != identifier
    ):
        raise ValueError(f"{label} belongs to another delivery")


def _valid_delivery_id(value: object) -> bool:
    return isinstance(value, str) and DELIVERY_ID_RE.fullmatch(value) is not None


def book_root_for(
    root: Path,
    manifest: dict[str, Any],
) -> Path:
    source_name = Path(str(_manifest_source(manifest)["name"])).stem
    label = portable_path_segment(source_name, max_bytes=60)
    return root / f"{label}--{source_id(manifest)}"


def _source_blocker() -> dict[str, str]:
    return {
        "code": "source_identity_changed",
        "message": "原文件当前字节与不可变 v0 身份不一致；请确认原文件位置和内容。",
    }


def _plain_preprocess_reason(reason: str) -> str:
    normalized = reason.strip().casefold()
    if normalized == "low_text_quality":
        return "编码检测后的文本质量过低，当前输入不能安全进入清洗。"
    if normalized in {
        "ambiguous_strict_decoding",
        "no_strict_decoder",
        "unsupported_bom",
        "unsupported_explicit_encoding",
        "explicit_encoding_conflicts_with_bom",
    }:
        return "输入编码无法被唯一且严格地确认，当前输入不能安全进入清洗。"
    return f"预处理安全检查已阻止继续：{reason[:360]}"


def determine_status(
    manifest: dict[str, Any],
    *,
    source_unchanged: bool,
) -> tuple[str, list[dict[str, str]]]:
    blockers: list[dict[str, str]] = []
    if not source_unchanged:
        blockers.append(_source_blocker())
        return "blocked", blockers

    preprocess_stage = _stage(manifest, "0_preprocess")
    verify_stage = _stage(manifest, "6_verify")
    preprocess_status = str(preprocess_stage.get("status") or "pending")
    verify_status = str(verify_stage.get("status") or "pending")
    if preprocess_status in {"blocked", "failed"}:
        reason = str(
            preprocess_stage.get("blocked_reason")
            or preprocess_stage.get("error")
            or "preprocess_not_safe"
        )
        blockers.append(
            {
                "code": "preprocess_blocked",
                "message": _plain_preprocess_reason(reason)[:500],
                "detail": reason[:500],
            }
        )
        return "blocked", blockers
    if verify_status in {"blocked", "failed"}:
        reason = str(
            verify_stage.get("blocked_reason")
            or verify_stage.get("error")
            or "verification_not_passed"
        )
        blockers.append({"code": "verification_blocked", "message": reason[:500]})
        return "blocked", blockers
    if verify_status == "incomplete":
        blockers.append(
            {
                "code": "verification_incomplete",
                "message": "最终验证不完整，不能生成阅读文件。",
            }
        )
        return "incomplete", blockers
    warnings = verify_stage.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        blockers.append(
            {
                "code": "verification_warnings",
                "message": "; ".join(str(value) for value in warnings[:5])[:500],
            }
        )
        return "needs_review", blockers
    if verify_status == "passed":
        return "completed", blockers

    ads_status = str(_stage(manifest, "2_ads").get("status") or "pending")
    report_statuses = {
        str(_stage(manifest, name).get("status") or "pending")
        for name in ("3_titles", "4_blocked_words")
    }
    if ads_status in {"pending", "skipped"} and report_statuses.intersection(
        {"candidates_ready", "done"}
    ):
        return "report_only", blockers
    blockers.append(
        {
            "code": "workflow_requires_review",
            "message": "清洗流程尚未形成可导出的 passed 验证终态。",
        }
    )
    return "needs_review", blockers


def next_actions(status: str, blockers: list[dict[str, str]]) -> list[str]:
    if status in {"completed", "report_only"}:
        return []
    if status == "incomplete":
        return ["修复验证缺口后重新运行完整 verify，再调用 publisher。"]
    if status == "needs_review":
        return ["打开复核页，处理首个阻止项后从最早的 pending 阶段继续。"]
    code = blockers[0].get("code") if blockers else None
    if code == "source_identity_changed":
        return ["确认原文件未被移动或改写；恢复与 v0 相同的原文件后重新调用 publisher。"]
    if code == "preprocess_blocked":
        return [
            "打开复核页中的编码报告，确认正确编码后重新运行 preprocess.py；混合编码仅走受支持的 input_repair.py 流程。"
        ]
    if code == "verification_blocked":
        return ["打开复核页处理首个验证阻止项，然后重新运行完整 verify.py 和 publisher。"]
    if code == "export_attestation_rejected":
        return ["重新运行完整 verify.py；只有新验证通过后才能再次调用 publisher。"]
    reason = blockers[0]["message"] if blockers else "未知阻止项"
    return [f"打开复核页处理该阻止项后重试：{reason}"]


def _review_workflow(status: str) -> dict[str, str]:
    values = {
        "completed": ("completed", "处理完成", "清洗结果已验证并安全发布。"),
        "needs_review": ("needs-review", "需要复核", "当前存在需要处理的判断或流程缺口。"),
        "blocked": ("needs-review", "已安全阻止", "安全门禁已停止阅读文件生成。"),
        "incomplete": ("needs-review", "验证不完整", "验证未覆盖完整终态，未生成阅读文件。"),
        "report_only": ("verified", "仅生成检查报告", "本任务只检查，不修改也不导出小说正文。"),
    }
    key, label, message = values[status]
    return {"key": key, "label": label, "title": label, "message": message}


def _fallback_review_html(
    status: str,
    workspace: Path,
    blockers: list[dict[str, str]],
    audit_paths: list[Path],
) -> str:
    blocker_html = "".join(
        f"<li><b>{html.escape(item['code'])}</b>：{html.escape(item['message'])}</li>"
        for item in blockers
    ) or "<li>当前没有额外阻止项。</li>"
    audit_html = "".join(f"<li>{html.escape(str(path))}</li>" for path in audit_paths)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>小说清洗结果</title><style>body{{font:16px/1.65 system-ui,sans-serif;max-width:900px;margin:auto;padding:2rem;color:#18202a}}code,li{{overflow-wrap:anywhere}}.state{{padding:.8rem 1rem;background:#f3f5f7;border-radius:.6rem}}</style></head>
<body><main><h1>小说清洗结果</h1><p class="state">状态：{html.escape(status)}</p>
<h2>需要注意</h2><ul>{blocker_html}</ul>
<details><summary>高级审计</summary><p>工作区：{html.escape(str(workspace))}</p><ul>{audit_html}</ul></details>
</main></body></html>
"""


def _review_binding_markup(
    *, source_id_value: str, identifier: str, status: str
) -> str:
    binding = {
        "schema": "cml.review-delivery-binding.v1",
        "source_id": source_id_value,
        "delivery_id": identifier,
        "status": status,
    }
    encoded = json.dumps(binding, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f'<script id="cml-delivery-binding" type="application/json">{encoded}</script>'


def bind_review_delivery(
    rendered: str,
    *,
    source_id_value: str,
    identifier: str,
    status: str,
) -> str:
    marker = _review_binding_markup(
        source_id_value=source_id_value, identifier=identifier, status=status
    )
    if "</body>" not in rendered or 'id="cml-delivery-binding"' in rendered:
        raise ValueError("review HTML cannot receive a unique delivery binding")
    return rendered.replace("</body>", marker + "</body>", 1)


def _validate_review_delivery_binding(
    path: Path,
    *,
    expected_source_id: str,
    expected_delivery_id: str,
    expected_status: str,
) -> None:
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("review delivery binding is not a bounded regular file")
    try:
        rendered = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("review delivery binding cannot be read") from exc
    matches = re.findall(
        r'<script id="cml-delivery-binding" type="application/json">([^<]+)</script>',
        rendered,
    )
    if len(matches) != 1:
        raise ValueError("review delivery binding is missing or duplicated")
    try:
        binding = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise ValueError("review delivery binding is invalid JSON") from exc
    if (
        not isinstance(binding, dict)
        or binding
        != {
            "schema": "cml.review-delivery-binding.v1",
            "source_id": expected_source_id,
            "delivery_id": expected_delivery_id,
            "status": expected_status,
        }
    ):
        raise ValueError("review delivery binding is stale or belongs to another delivery")


def render_review(
    workspace: Path,
    status: str,
    blockers: list[dict[str, str]],
    export_report: dict[str, Any] | None,
    trusted_counts: dict[str, int],
) -> tuple[str, dict[str, Any]]:
    audit_paths = [
        workspace / "logs/operations.jsonl",
        workspace / "logs/anomalies.jsonl",
        workspace / "report/verify_report.json",
        workspace / "report/final_report.md",
    ]
    try:
        item = build_review_html.workspace_review(workspace, 80, 3)
        _apply_trusted_counts(item, trusted_counts)
        item["workflow"] = _review_workflow(status)
        if export_report is not None:
            item.setdefault("reports", {})["export"] = export_report
        data = {
            "mode": "single",
            "run_id": uuid.uuid4().hex,
            "summary": item["summary"],
            "workspaces": [item],
        }
        rendered = build_review_html.render_html(data, "小说清洗结果")
        audit = "".join(f"<li>{html.escape(str(path))}</li>" for path in audit_paths)
        injected = (
            "<details class='processing-info'><summary>高级审计：内部日志路径</summary>"
            f"<ul>{audit}</ul></details>"
        )
        rendered = rendered.replace("</main>", injected + "</main>", 1)
        return rendered, item
    except Exception as exc:
        # A stale, partial, or unrenderable *interactive* review page is never
        # an acceptable substitute for a completed or needs-review bundle.
        # Definitively blocked/report-only runs may still publish a deliberately
        # plain, non-interactive explanation with no reading file.
        if status in {"completed", "needs_review"}:
            raise RuntimeError(
                "interactive review could not be rendered; no reliable terminal bundle was published"
            ) from exc
        return _fallback_review_html(status, workspace, blockers, audit_paths), {
            "summary": {},
            "modules": {},
            "review_fallback": True,
        }


def _apply_trusted_counts(item: dict[str, Any], counts: dict[str, int]) -> None:
    """Prevent presentation-derived summaries from becoming delivery facts."""

    summary = item.get("summary")
    modules = item.get("modules")
    if not isinstance(summary, dict) or not isinstance(modules, dict):
        raise ValueError("review builder returned an invalid summary model")
    ads = modules.get("ads")
    if not isinstance(ads, dict):
        raise ValueError("review builder returned no ads module")
    summary.update(
        {
            "ads_candidates": counts["ads_candidates"],
            "title_candidates": counts["title_candidates"],
            "blocked_candidates": counts["blocked_candidates"],
            "operations": counts["operations"],
            "deleted_operations": counts["delete"],
            "changed_characters": counts["deleted_characters"],
            "formal_decisions": counts["delete"] + counts["keep"] + counts["uncertain"],
            "formal_uncertain": counts["uncertain"],
            "ads_decision_pending": max(
                counts["ads_candidates"]
                - counts["delete"]
                - counts["keep"]
                - counts["uncertain"],
                0,
            ),
        }
    )
    ads["candidate_count"] = counts["ads_candidates"]
    ads["decision_count"] = counts["delete"] + counts["keep"] + counts["uncertain"]
    ads["decision_by_verdict"] = {
        key: counts[key] for key in ("delete", "keep", "uncertain")
    }


def _bounded_stage_count(stage: dict[str, Any], *names: str) -> int:
    values: list[int] = []
    for name in names:
        value = stage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            values.append(value)
    return max(values, default=0)


def _authoritative_counts(workspace: Path, manifest: dict[str, Any]) -> dict[str, int]:
    """Count only committed manifest/ledger records, never presentation output."""

    ads_stage = _stage(manifest, "2_ads")
    ads_candidates = _bounded_stage_count(
        ads_stage,
        "total_candidate_count",
        "candidate_count",
        "formal_decision_count",
        "decision_count",
    )
    title_candidates = _bounded_stage_count(
        _stage(manifest, "3_titles"), "total_candidate_count", "candidate_count"
    )
    blocked_candidates = _bounded_stage_count(
        _stage(manifest, "4_blocked_words"), "total_candidate_count", "candidate_count"
    )

    decisions: list[dict[str, Any]] = []
    decisions_value = ads_stage.get("formal_decisions")
    if isinstance(decisions_value, str) and decisions_value:
        decisions_path = resolve_in_workspace(workspace, decisions_value, role="read")
        decisions = load_jsonl(decisions_path)
    by_verdict = {"delete": 0, "keep": 0, "uncertain": 0}
    if decisions:
        for decision in decisions:
            verdict = decision.get("verdict")
            if verdict not in by_verdict:
                raise ValueError("formal decision ledger contains an invalid verdict")
            by_verdict[str(verdict)] += 1
        expected = _bounded_stage_count(
            ads_stage,
            "formal_decision_count",
            "decision_count",
        )
        if expected and expected != len(decisions):
            raise ValueError("formal decision ledger count does not match the manifest")
        ads_candidates = max(ads_candidates, len(decisions))
    else:
        manifest_counts = ads_stage.get("formal_by_verdict")
        if isinstance(manifest_counts, dict):
            for verdict in by_verdict:
                value = manifest_counts.get(verdict, 0)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError("manifest formal verdict count is invalid")
                by_verdict[verdict] = value

    active_run_id = ads_stage.get("active_run_id")
    operations = (
        load_jsonl_for_run(workspace / "logs/operations.jsonl", active_run_id)
        if isinstance(active_run_id, str) and active_run_id
        else []
    )
    deleted_characters = 0
    for operation in operations:
        original = operation.get("original")
        replacement = operation.get("replacement")
        if not isinstance(original, str) or not isinstance(replacement, str):
            raise ValueError("operation ledger contains invalid text accounting")
        deleted_characters += abs(len(original) - len(replacement))
    return {
        "ads_candidates": ads_candidates,
        "title_candidates": title_candidates,
        "blocked_candidates": blocked_candidates,
        "candidates": ads_candidates + title_candidates + blocked_candidates,
        **by_verdict,
        "operations": len(operations),
        "deleted_characters": deleted_characters,
    }


def _strict_json_file(path: Path, *, max_bytes: int, label: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError(f"{label} is not a bounded regular file")

    def reject_constant(value: str) -> None:
        raise ValueError(f"{label} contains a non-finite number: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} cannot be read as strict JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _expected_link(path: Path | None, book_root: Path, *, directory: bool = False) -> str | None:
    if path is None:
        return None
    relative = path.relative_to(book_root).as_posix()
    if directory:
        relative += "/"
    return quote(relative, safe="/-_.~")


def _validate_artifact_record(path: Path, record: object, *, label: str) -> None:
    if not isinstance(record, dict):
        raise ValueError(f"latest success {label} integrity record is missing")
    if record.get("path") != str(path):
        raise ValueError(f"latest success {label} integrity path is stale")
    size = record.get("size_bytes")
    digest = record.get("sha256")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError(f"latest success {label} integrity record is invalid")
    if not path.is_file() or path.stat().st_size != size or sha256_file(path) != digest:
        raise ValueError(f"latest success {label} integrity check failed")


def _validate_latest_entry(
    entry: object,
    *,
    book_root: Path,
    expected_source_id: str,
    require_success: bool,
) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("latest entry is invalid")
    identifier = entry.get("delivery_id")
    created_at = entry.get("created_at")
    status = entry.get("status")
    if entry.get("source_id") != expected_source_id:
        raise ValueError("latest entry belongs to another source")
    if (
        entry.get("schema") != ATTEMPT_SCHEMA
        or not _valid_delivery_id(identifier)
        or not isinstance(created_at, str)
        or not created_at
        or status not in TERMINAL_STATUSES
        or (require_success and status != "completed")
    ):
        raise ValueError("latest entry identity or status is invalid")

    delivery_dir = Path(str(entry.get("delivery_dir") or ""))
    review = Path(str(entry.get("review") or ""))
    result_path = Path(str(entry.get("result") or ""))
    expected_dir = book_root / identifier
    if (
        not delivery_dir.is_absolute()
        or not review.is_absolute()
        or not result_path.is_absolute()
        or delivery_dir.resolve(strict=False) != expected_dir.resolve(strict=False)
        or review.resolve(strict=False) != (expected_dir / REVIEW_NAME).resolve(strict=False)
        or result_path.resolve(strict=False) != (expected_dir / RESULT_NAME).resolve(strict=False)
    ):
        raise ValueError("latest entry paths are outside the bound delivery")

    outputs_value = entry.get("outputs")
    if not isinstance(outputs_value, dict) or any(key not in FORMAT_NAMES for key in outputs_value):
        raise ValueError("latest entry outputs are invalid")
    outputs: dict[str, str] = {}
    for kind, raw_path in outputs_value.items():
        if not isinstance(raw_path, str):
            raise ValueError("latest entry output path is invalid")
        output = Path(raw_path)
        expected_output = expected_dir / FORMAT_NAMES[kind]
        if not output.is_absolute() or output.resolve(strict=False) != expected_output.resolve(strict=False):
            raise ValueError("latest entry output escapes the bound delivery")
        outputs[kind] = str(output)
    primary_value = entry.get("primary_output")
    primary = Path(primary_value) if isinstance(primary_value, str) else None
    if primary_value is not None and str(primary) not in outputs.values():
        raise ValueError("latest entry primary output is not a bound output")
    if status == "completed":
        if "txt" not in outputs or primary is None:
            raise ValueError("latest completed entry has no bound TXT reading output")
    elif outputs or primary_value is not None:
        raise ValueError("latest non-completed entry exposes a reading output")

    links = entry.get("links")
    expected_links = {
        "review": _expected_link(review, book_root),
        "primary_output": _expected_link(primary, book_root),
        "delivery_dir": _expected_link(delivery_dir, book_root, directory=True),
    }
    if links != expected_links:
        raise ValueError("latest entry link is unsafe or stale")

    # Authenticate result bytes before parsing them.  This makes a changed JSON
    # file an integrity failure, not a source of new untrusted fields.
    entry_artifacts = entry.get("artifacts")
    if not isinstance(entry_artifacts, dict):
        raise ValueError("latest entry integrity records are missing")
    if set(entry_artifacts) != {"result", "review", "outputs"}:
        raise ValueError("latest entry integrity records are incomplete")
    _validate_delivery_artifact_binding(
        entry_artifacts.get("result"),
        source_id_value=expected_source_id,
        identifier=identifier,
        label="latest result artifact",
    )
    _validate_artifact_record(result_path, entry_artifacts.get("result"), label="result")

    result = _strict_json_file(result_path, max_bytes=2 * 1024 * 1024, label="delivery result")
    result_delivery = result.get("delivery")
    source = result.get("source")
    if (
        result.get("schema") != RESULT_SCHEMA
        or result.get("delivery_id") != identifier
        or result.get("source_id") != expected_source_id
        or result.get("status") != status
        or not isinstance(result_delivery, dict)
        or not isinstance(source, dict)
    ):
        raise ValueError("latest entry result binding is invalid")
    try:
        result_source_id = source_identity_id(str(source["sha256"]), Path(str(source["path"])))
    except (KeyError, OSError, ValueError) as exc:
        raise ValueError("latest entry source identity is invalid") from exc
    if result_source_id != expected_source_id:
        raise ValueError("latest entry belongs to another source")
    expected_delivery = {
        "source_id": expected_source_id,
        "delivery_id": identifier,
        "delivery_dir": str(delivery_dir),
        "review": str(review),
        "result": str(result_path),
        "primary_output": primary_value,
        "outputs": outputs,
    }
    for key, expected in expected_delivery.items():
        if result_delivery.get(key) != expected:
            raise ValueError(f"latest entry result has stale {key}")
    artifacts = result_delivery.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("delivery result integrity records are missing")
    _validate_delivery_artifact_binding(
        artifacts.get("review"),
        source_id_value=expected_source_id,
        identifier=identifier,
        label="result review artifact",
    )
    _validate_artifact_record(review, artifacts.get("review"), label="review")
    _validate_review_delivery_binding(
        review,
        expected_source_id=expected_source_id,
        expected_delivery_id=identifier,
        expected_status=status,
    )
    output_artifacts = artifacts.get("outputs")
    if not isinstance(output_artifacts, dict) or set(output_artifacts) != set(outputs):
        raise ValueError("latest success output integrity records are incomplete")
    for kind, raw_path in outputs.items():
        _validate_delivery_artifact_binding(
            output_artifacts.get(kind),
            source_id_value=expected_source_id,
            identifier=identifier,
            label=f"result {kind} output artifact",
        )
        _validate_artifact_record(
            Path(raw_path),
            output_artifacts.get(kind),
            label=f"{kind} output",
        )
    _validate_delivery_artifact_binding(
        entry_artifacts.get("review"),
        source_id_value=expected_source_id,
        identifier=identifier,
        label="latest review artifact",
    )
    _validate_artifact_record(review, entry_artifacts.get("review"), label="review")
    entry_outputs = entry_artifacts.get("outputs")
    if not isinstance(entry_outputs, dict) or set(entry_outputs) != set(outputs):
        raise ValueError("latest entry output integrity records are incomplete")
    for kind, raw_path in outputs.items():
        _validate_delivery_artifact_binding(
            entry_outputs.get(kind),
            source_id_value=expected_source_id,
            identifier=identifier,
            label=f"latest {kind} output artifact",
        )
        _validate_artifact_record(
            Path(raw_path), entry_outputs.get(kind), label=f"{kind} output"
        )
    return entry


def _load_latest(
    path: Path,
    *,
    book_root: Path,
    expected_source_id: str,
    expected_book: str,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = _strict_json_file(path, max_bytes=256 * 1024, label="latest index")
    if (
        value.get("schema") != LATEST_SCHEMA
        or value.get("source_id") != expected_source_id
        or value.get("book") != expected_book
    ):
        raise ValueError("latest index schema is invalid")
    _validate_latest_entry(
        value.get("latest_attempt"),
        book_root=book_root,
        expected_source_id=expected_source_id,
        require_success=False,
    )
    success = value.get("latest_success")
    if success is not None:
        _validate_latest_entry(
            success,
            book_root=book_root,
            expected_source_id=expected_source_id,
            require_success=True,
        )
    return value


def _attempt_entry(
    *,
    identifier: str,
    created_at: str,
    status: str,
    delivery_dir: Path,
    review: Path,
    result: Path,
    outputs: dict[str, str],
    primary_output: str | None,
    book_root: Path,
    source_id_value: str,
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    def relative_link(path: Path | None, *, directory: bool = False) -> str | None:
        if path is None:
            return None
        relative = path.relative_to(book_root).as_posix()
        if directory:
            relative += "/"
        return quote(relative, safe="/-_.~")

    return {
        "schema": ATTEMPT_SCHEMA,
        "delivery_id": identifier,
        "source_id": source_id_value,
        "created_at": created_at,
        "status": status,
        "delivery_dir": str(delivery_dir),
        "review": str(review),
        "result": str(result),
        "primary_output": primary_output,
        "outputs": outputs,
        "artifacts": artifacts,
        "links": {
            "review": relative_link(review),
            "primary_output": relative_link(
                Path(primary_output) if primary_output is not None else None
            ),
            "delivery_dir": relative_link(delivery_dir, directory=True),
        },
    }


def _render_start_here(index: dict[str, Any]) -> str:
    attempt = index["latest_attempt"]
    success = index.get("latest_success")
    if (
        isinstance(success, dict)
        and success.get("delivery_id") == attempt.get("delivery_id")
    ):
        success = {**success, "status": f"{success.get('status')} · 与最新尝试相同"}

    def card(label: str, value: dict[str, Any] | None) -> str:
        if value is None:
            return f"<section><h2>{label}</h2><p>尚无成功交付。</p></section>"
        primary = value.get("primary_output") or "未生成"
        links = value.get("links") if isinstance(value.get("links"), dict) else {}
        review_link = html.escape(str(links.get("review") or ""), quote=True)
        primary_link = html.escape(
            str(links.get("primary_output") or ""),
            quote=True,
        )
        delivery_link = html.escape(str(links.get("delivery_dir") or ""), quote=True)
        review_open = (
            f"<a href=\"{review_link}\">打开复核页</a> · " if review_link else ""
        )
        primary_open = (
            f"<a href=\"{primary_link}\">打开清洗后文件</a> · "
            if primary_link
            else ""
        )
        delivery_open = (
            f"<a href=\"{delivery_link}\">打开结果目录</a> · "
            if delivery_link
            else ""
        )
        return (
            f"<p class='attempt-meta'>Attempt ID: {html.escape(str(value['delivery_id']))} · Time: {html.escape(str(value['created_at']))}</p>"
            f"<section><h2>{label}</h2><p>状态：{html.escape(str(value['status']))}</p>"
            f"<p>{review_open}复核页：{html.escape(str(value['review']))}</p>"
            f"<p>{primary_open}清洗后文件：{html.escape(str(primary))}</p>"
            f"<p>{delivery_open}结果目录：{html.escape(str(value['delivery_dir']))}</p></section>"
        )

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>小说清洗入口</title><style>body{{font:16px/1.65 system-ui,sans-serif;max-width:900px;margin:auto;padding:2rem;color:#18202a}}section{{margin:1rem 0;padding:1rem;border:1px solid #d9dee5;border-radius:.7rem}}p{{overflow-wrap:anywhere}}</style></head><body><main><h1>{html.escape(str(index['book']))}</h1>{card('最新尝试', attempt)}{card('最近成功', success)}</main></body></html>"""


def _result_payload(
    *,
    workspace: Path,
    manifest: dict[str, Any],
    status: str,
    blockers: list[dict[str, str]],
    counts: dict[str, int],
    created_at: str,
    identifier: str,
    delivery_root: Path,
    book_root: Path,
    delivery_dir: Path,
    review_path: Path,
    result_path: Path,
    outputs: dict[str, str],
    primary_output: str | None,
    requested_formats: tuple[str, ...],
    verification: dict[str, Any],
    unchanged: bool,
    delivery_artifacts: dict[str, Any],
) -> dict[str, Any]:
    source = _manifest_source(manifest)
    v0 = manifest.get("v0") if isinstance(manifest.get("v0"), dict) else {}
    current_head = str(manifest.get("current_head") or "")
    current_path = resolve_in_workspace(workspace, current_head, role="read")
    source_id_value = source_id(manifest)
    if status == "completed":
        if verification.get("status") != "passed" or verification.get("publisher_gate_status") != "passed":
            raise ValueError("completed delivery has no passed publisher verification")
        if "txt" not in outputs or primary_output is None:
            raise ValueError("completed delivery has no TXT reading output")
    elif outputs or primary_output is not None:
        raise ValueError("non-completed delivery cannot expose reading outputs")
    return {
        "schema": RESULT_SCHEMA,
        "created_at": created_at,
        "delivery_id": identifier,
        "source_id": source_id_value,
        "status": status,
        "source": {
            "path": str(source["path"]),
            "name": str(source["name"]),
            "sha256": str(source["sha256"]),
            "size_bytes": int(source["size_bytes"]),
            "v0_path": str(workspace / "versions/v0_original.txt"),
            "v0_sha256": v0.get("sha256"),
            "v0_unchanged": sha256_file(workspace / "versions/v0_original.txt")
            == v0.get("sha256"),
            "source_matches_v0": unchanged,
        },
        "workspace": {
            "path": str(workspace),
            "current_head": current_head,
            "current_head_sha256": sha256_file(current_path),
        },
        "verification": verification,
        "blockers": blockers,
        "counts": counts,
        "delivery": {
            "source_id": source_id_value,
            "delivery_id": identifier,
            "root": str(delivery_root),
            "book_root": str(book_root),
            "delivery_dir": str(delivery_dir),
            "review": str(review_path),
            "result": str(result_path),
            "requested_formats": list(requested_formats),
            "produced_formats": list(outputs),
            "primary_output": primary_output,
            "outputs": outputs,
            "artifacts": delivery_artifacts,
        },
        "next_actions": next_actions(status, blockers),
    }


def _terminal_receipt(result: dict[str, Any]) -> dict[str, Any]:
    delivery = result["delivery"]
    counts = result["counts"]
    status = str(result["status"])
    actions = result["next_actions"]
    return {
        "status": status,
        "review": delivery["review"],
        "primary_output": delivery["primary_output"],
        "delivery_dir": delivery["delivery_dir"],
        "result": delivery["result"],
        "formats": delivery["produced_formats"],
        "requested_formats": delivery["requested_formats"],
        "counts": counts,
        "source_v0_unchanged": bool(
            result["source"]["v0_unchanged"]
            and result["source"]["source_matches_v0"]
        ),
        "source_v0": (
            "unchanged"
            if bool(result["source"]["v0_unchanged"] and result["source"]["source_matches_v0"])
            else "mismatch"
        ),
        "reason": result["blockers"][0] if result["blockers"] else None,
        "next_action": actions[0] if actions else None,
        "exit_code": 0 if status == "completed" else 2,
    }


def run(
    workspace: Path,
    *,
    delivery_root: Path | None = None,
    requested_formats: Iterable[str] | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    requested = export_outputs.normalize_requested_formats(requested_formats)
    workspace = Path(workspace).resolve(strict=False)
    with workspace_transaction_lock(workspace):
        workspace, _, _ = resolve_workspace_paths(workspace)
        manifest = load_manifest(workspace)
        unchanged = source_matches_v0(workspace, manifest)
        status, blockers = determine_status(manifest, source_unchanged=unchanged)
        root = resolve_delivery_root(workspace, manifest, delivery_root)
        book_root = book_root_for(root, manifest)
        identifier = delivery_id()
        delivery_dir = book_root / identifier
        review_path = delivery_dir / REVIEW_NAME
        result_path = delivery_dir / RESULT_NAME
        start_path = book_root / START_NAME
        latest_path = book_root / LATEST_NAME

        output_paths: dict[str, Path] = {}
        export_plan: dict[str, Any] | None = None
        export_report: dict[str, Any] | None = None
        declared_verification_status = str(
            _stage(manifest, "6_verify").get("status") or "pending"
        )
        verification: dict[str, Any] = {
            "declared_status": declared_verification_status,
            # "status" is the publisher's public conclusion, rather than a
            # stale manifest claim that can disagree with the terminal bundle.
            "status": status,
            "publisher_gate_status": status,
            "report": str(workspace / "report/verify_report.json"),
            "run_id": _stage(manifest, "6_verify").get("run_id"),
        }
        config_inputs: set[Path] = set()
        if status == "completed":
            input_path = resolve_current_head(workspace)
            try:
                trace = export_outputs.require_export_attestation(workspace, input_path)
            except ValueError as exc:
                status = "blocked"
                verification["status"] = "blocked"
                verification["publisher_gate_status"] = "blocked"
                verification["publisher_gate_reason"] = str(exc)[:500]
                blockers.append(
                    {
                        "code": "export_attestation_rejected",
                        "message": str(exc)[:500],
                    }
                )
            else:
                verification.update(trace)
                verification["status"] = "passed"
                verification["publisher_gate_status"] = "passed"
                config = load_config(config_path, config_inputs)
                text = read_utf8(input_path)
                identity = export_outputs.resolve_export_identity(workspace, config, text)
                output_paths = {
                    kind: delivery_dir / FORMAT_NAMES[kind]
                    for kind in requested
                }
                export_plan = {
                    "workspace": workspace,
                    "input_path": input_path,
                    "protected_inputs": (input_path, *config_inputs),
                    "output_root": root,
                    "output_dir": delivery_dir,
                    "output_paths": output_paths,
                    "requested_formats": requested,
                    "report_path": workspace / "report/export_report.json",
                    "text": text,
                    "identity": identity,
                    "title": str(identity["title"]),
                    "author": str(identity["author"]),
                    "language": str(config.get("export", {}).get("language", "zh-CN")),
                    "verification": trace,
                }

        writes = {
            "review": review_path.relative_to(root).as_posix(),
            "result": result_path.relative_to(root).as_posix(),
            "start": start_path.relative_to(root).as_posix(),
            "latest": latest_path.relative_to(root).as_posix(),
            **{
                f"output_{kind}": path.relative_to(root).as_posix()
                for kind, path in output_paths.items()
            },
        }
        resolved = resolve_external_output_paths(
            root,
            writes=writes,
            workspaces=(workspace,),
            inputs=tuple(config_inputs),
        )
        source_id_value = source_id(manifest)
        book_name = Path(str(_manifest_source(manifest)["name"])).stem
        previous_latest = _load_latest(
            resolved["latest"],
            book_root=book_root,
            expected_source_id=source_id_value,
            expected_book=book_name,
        )

        inputs = [workspace / "manifest.json", workspace / "versions/v0_original.txt"]
        inputs.extend(config_inputs)
        if export_plan is not None:
            inputs.append(export_plan["input_path"])

        with ExternalDeliveryTransaction(
            root,
            workspaces=(workspace,),
            inputs=inputs,
        ) as delivery:
            delivery.stage_directory(delivery_dir, require_new=True)
            staged_outputs = {
                kind: delivery.stage_path(resolved[f"output_{kind}"])
                for kind in output_paths
            }
            outputs: dict[str, str] = {}
            output_artifacts: dict[str, dict[str, Any]] = {}
            if export_plan is not None:
                outputs, output_artifacts = export_outputs.write_export_outputs(
                    export_plan,
                    staged_outputs,
                )
                export_report = export_outputs.export_report(
                    export_plan,
                    outputs,
                    output_artifacts,
                )

            counts = _authoritative_counts(workspace, manifest)
            review_html, _ = render_review(
                workspace,
                status,
                blockers,
                export_report,
                counts,
            )
            review_html = bind_review_delivery(
                review_html,
                source_id_value=source_id_value,
                identifier=identifier,
                status=status,
            )
            created_at = now_iso()
            primary_output = (
                outputs[requested[0]]
                if export_plan is not None and requested[0] in outputs
                else None
            )
            write_utf8(delivery.stage_path(resolved["review"]), review_html)
            bound_output_artifacts = {
                kind: _bind_artifact_to_delivery(
                    record,
                    source_id_value=source_id_value,
                    identifier=identifier,
                )
                for kind, record in output_artifacts.items()
            }
            delivery_artifacts = {
                "review": _bind_artifact_to_delivery(
                    _artifact_record(
                        review_path,
                        delivery.stage_path(resolved["review"]),
                    ),
                    source_id_value=source_id_value,
                    identifier=identifier,
                ),
                "outputs": bound_output_artifacts,
            }
            result = _result_payload(
                workspace=workspace,
                manifest=manifest,
                status=status,
                blockers=blockers,
                counts=counts,
                created_at=created_at,
                identifier=identifier,
                delivery_root=root,
                book_root=book_root,
                delivery_dir=delivery_dir,
                review_path=review_path,
                result_path=result_path,
                outputs=outputs,
                primary_output=primary_output,
                requested_formats=requested if export_plan is not None else (),
                verification=verification,
                unchanged=unchanged,
                delivery_artifacts=delivery_artifacts,
            )
            write_json(delivery.stage_path(resolved["result"]), result)
            attempt_artifacts = {
                "review": delivery_artifacts["review"],
                "outputs": bound_output_artifacts,
                "result": _bind_artifact_to_delivery(
                    _artifact_record(
                        result_path,
                        delivery.stage_path(resolved["result"]),
                    ),
                    source_id_value=source_id_value,
                    identifier=identifier,
                ),
            }
            attempt = _attempt_entry(
                identifier=identifier,
                created_at=created_at,
                status=status,
                delivery_dir=delivery_dir,
                review=review_path,
                result=result_path,
                outputs=outputs,
                primary_output=primary_output,
                book_root=book_root,
                source_id_value=source_id_value,
                artifacts=attempt_artifacts,
            )
            latest = {
                "schema": LATEST_SCHEMA,
                "book": book_name,
                "source_id": source_id_value,
                "latest_attempt": attempt,
                "latest_success": (
                    attempt
                    if status == "completed"
                    else previous_latest.get("latest_success")
                    if previous_latest is not None
                    else None
                ),
            }

            write_utf8(delivery.stage_path(resolved["start"]), _render_start_here(latest))
            write_json(delivery.stage_path(resolved["latest"]), latest)

            if export_report is None or export_plan is None:
                delivery.publish()
                delivery.finalize()
            else:
                report_path = export_plan["report_path"]
                commits = ((workspace, "7_export", "done"),)
                with WorkspaceTransaction(workspace, run_id=delivery.run_id) as transaction:
                    write_json(transaction.stage_path(report_path), export_report)
                    delivery.publish(commits=commits)
                    transaction.commit(
                        {
                            "7_export": (
                                "done",
                                export_outputs.export_stage_update(
                                    export_plan,
                                    export_report,
                                ),
                            )
                        },
                        defer_cleanup=True,
                        group_commits=commits,
                    )
                    transaction.finalize()
                    delivery.finalize()
        return _terminal_receipt(result)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish the only user-facing terminal bundle for one workspace."
    )
    parser.add_argument("workspace", help="Path to one .cleanwork directory.")
    parser.add_argument("--delivery-root", help="External Novel-Purifier result root.")
    parser.add_argument("--config", help="Optional validated config JSON.")
    formats = parser.add_mutually_exclusive_group()
    formats.add_argument(
        "--format",
        action="append",
        choices=export_outputs.ALL_FORMATS,
        dest="requested_formats",
    )
    formats.add_argument("--all-formats", action="store_true")
    args = parser.parse_args()
    requested = (
        export_outputs.ALL_FORMATS
        if args.all_formats
        else export_outputs.normalize_requested_formats(args.requested_formats)
    )
    try:
        report = run(
            Path(args.workspace),
            delivery_root=Path(args.delivery_root) if args.delivery_root else None,
            requested_formats=requested,
            config_path=Path(args.config).resolve() if args.config else None,
        )
    except Exception as exc:  # CLI boundary: no unreliable partial receipt.
        failure = {
            "status": "publisher_failed",
            "error": str(exc)[:1000],
            "exit_code": 1,
        }
        # Keep the CLI receipt ASCII-only so a parent process can decode it
        # deterministically even when Windows console encodings disagree.
        print(json.dumps(failure, ensure_ascii=True, separators=(",", ":")))
        raise SystemExit(1) from exc
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
    raise SystemExit(int(report["exit_code"]))


if __name__ == "__main__":
    main()
