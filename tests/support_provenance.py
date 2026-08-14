from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock

import apply_decisions
import common
import scan_identity
import verify


def fake_formal_provenance(workspace: Path, decisions_value: str) -> dict[str, object]:
    decisions_path = Path(decisions_value)
    if not decisions_path.is_absolute():
        decisions_path = workspace / decisions_path
    decisions_path = decisions_path.resolve()
    return {
        "formal_run_id": "f" * 32,
        "formal_report": "report/ad_decision_formal_report.json",
        "formal_report_sha256": "e" * 64,
        "formal_decisions": decisions_path.relative_to(workspace.resolve()).as_posix(),
        "formal_decisions_sha256": common.sha256_file(decisions_path),
        "formal_reviews_sha256": "c" * 64,
        "formal_draft_sha256": None,
        "scan_id": "a" * 64,
        "candidate_set_sha256": "d" * 64,
        "profile": "meta/book_profile.json",
        "scan_rule_pack_sha256": "1" * 64,
        "draft_rule_pack_sha256": "2" * 64,
        "profile_present": False,
        "book_profile_sha256": scan_identity.canonical_json_sha256({}),
        "book_profile_file_sha256": None,
    }


@contextmanager
def patched_apply_provenance(
    workspace: Path,
    decisions_value: str,
) -> Iterator[None]:
    provenance = fake_formal_provenance(workspace, decisions_value)
    with mock.patch.object(
        apply_decisions,
        "validate_formal_ad_provenance",
        return_value=provenance,
    ):
        yield


def run_isolated_apply(
    workspace: Path,
    module: str,
    input_value: str,
    decisions_value: str,
    output_value: str,
    stage: str,
) -> dict[str, object]:
    with patched_apply_provenance(workspace, decisions_value):
        return apply_decisions.run(
            workspace,
            module,
            input_value,
            decisions_value,
            output_value,
            stage,
        )


def run_isolated_verify(
    workspace: Path,
    module: str,
    before_value: str,
    after_value: str,
    decisions_value: str,
    skip_residual_scan: bool,
) -> dict[str, object]:
    provenance = fake_formal_provenance(workspace, decisions_value)
    with mock.patch.object(
        verify,
        "validate_formal_ad_provenance",
        return_value=provenance,
    ):
        return verify.run(
            workspace,
            module,
            before_value,
            after_value,
            decisions_value,
            skip_residual_scan,
        )
