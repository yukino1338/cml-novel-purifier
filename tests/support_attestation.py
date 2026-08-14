from __future__ import annotations

import hashlib
import json
from pathlib import Path

from verify import REQUIRED_VERIFY_CHECKS, VERIFY_RULE_VERSION
import scan_identity


def bind_passed_attestation(workspace: Path) -> dict:
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    current_head = manifest["current_head"]
    current_path = workspace / current_head
    current_sha256 = hashlib.sha256(current_path.read_bytes()).hexdigest()
    decision_sha256 = hashlib.sha256(b"test decision fixture").hexdigest()
    formal_run_id = "f" * 32
    formal_report_sha256 = hashlib.sha256(b"test formal report fixture").hexdigest()
    scan_id = "a" * 64
    candidate_set_sha256 = hashlib.sha256(b"test candidate set fixture").hexdigest()
    run_id = "6" * 32
    profile_path = workspace / "meta/book_profile.json"
    profile_identity = scan_identity.build_profile_identity(profile_path)
    runtime_identity = {
        "scan_rule_pack_sha256": scan_identity.canonical_json_sha256(
            scan_identity.build_scan_rule_pack("ads")
        ),
        "draft_rule_pack_sha256": scan_identity.canonical_json_sha256(
            scan_identity.build_draft_rule_pack()
        ),
        "profile": "meta/book_profile.json",
        **profile_identity,
    }
    checks = [
        {"name": name, "passed": True}
        for name in sorted(REQUIRED_VERIFY_CHECKS)
    ]
    attestation = {
        "schema_version": 3,
        "status": "passed",
        "verification_run_id": run_id,
        "apply_run_id": "2" * 32,
        "apply_output": None,
        "apply_output_sha256": None,
        "layout_run_id": None,
        "layout_config_sha256": None,
        "current_head": current_head,
        "current_head_sha256": current_sha256,
        "parent_path": manifest["artifacts"][current_head].get("parent_path"),
        "parent_sha256": manifest["artifacts"][current_head].get("parent_sha256"),
        "input_sha256": current_sha256,
        "decision_sha256": decision_sha256,
        "formal_run_id": formal_run_id,
        "formal_report_sha256": formal_report_sha256,
        "scan_id": scan_id,
        "candidate_set_sha256": candidate_set_sha256,
        **runtime_identity,
        "rule_version": VERIFY_RULE_VERSION,
        "checks": checks,
    }
    report_path = workspace / "report/verify_report.json"
    report_path.write_text(
        json.dumps(
            {"status": "passed", "warnings": [], "checks": checks, "attestation": attestation},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    relative = report_path.relative_to(workspace).as_posix()
    manifest["artifacts"][relative] = {
        "path": relative,
        "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "size_bytes": report_path.stat().st_size,
        "parent_path": None,
        "parent_sha256": None,
        "run_id": run_id,
        "stage": "6_verify",
        "config_sha256": None,
        "decision_sha256": None,
    }
    manifest["stages"]["6_verify"] = {
        "status": "passed",
        "run_id": run_id,
        "artifacts": [relative],
        "report": relative,
        "warnings": [],
        "decision_sha256": decision_sha256,
        "formal_run_id": formal_run_id,
        "formal_report_sha256": formal_report_sha256,
        "scan_id": scan_id,
        "candidate_set_sha256": candidate_set_sha256,
        **runtime_identity,
        "apply_output": None,
        "apply_output_sha256": None,
        "layout_run_id": None,
        "layout_config_sha256": None,
        "output_sha256": current_sha256,
        "attestation": attestation,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return attestation
