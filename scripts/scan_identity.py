from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import book_profile
from common import load_manifest, sha256_file


_HASH_KEYS_EXCLUDED_FROM_CANDIDATE = frozenset(
    {
        "anchor_id",
        "candidate_fingerprint",
        "candidate_id",
        # An edit plan is derived after the candidate fingerprint and then binds
        # back to that fingerprint.  Hashing it here creates a self-reference:
        # binding a valid plan would immediately make the candidate stale.
        "edit_plan",
        "edit_plan_id",
    }
)
_SHA256_LENGTH = 64
_STAGE_BY_SCANNER = {
    "ads": "2_ads",
    "titles": "3_titles",
    "blocked": "4_blocked_words",
}
SCAN_IDENTITY_SCHEMA_VERSION = 3
SCAN_RULE_PACK_SCHEMA_VERSION = 1
DRAFT_RULE_PACK_SCHEMA_VERSION = 1
REVIEW_PROTOCOL_IDENTITY_SCHEMA_VERSION = 1
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCAN_RULE_PACK_FILES: dict[str, tuple[str, ...]] = {
    "ads": (
        "scripts/ad_decision_policy.py",
        "scripts/ad_rules.py",
        "scripts/parse_structure.py",
        "scripts/scan_ads.py",
    ),
    "titles": ("scripts/parse_structure.py", "scripts/scan_titles.py"),
    "blocked": ("scripts/parse_structure.py", "scripts/scan_blocked.py"),
}
_DRAFT_RULE_PACK_FILES = (
    "scripts/ad_decision_policy.py",
    "scripts/ad_review_protocol.py",
    "scripts/ad_rules.py",
    "scripts/book_profile.py",
    "scripts/make_ad_decisions.py",
)
_REVIEW_PROTOCOL_FILES = ("scripts/ad_review_protocol.py",)
_ACTIVE_SCAN_STATUSES = frozenset(
    {"candidates_ready", "draft_decisions_ready", "formal_decisions_ready", "done"}
)
STRUCTURE_SLICE_KINDS = frozenset(
    {"front_matter", "chapter", "body", "fallback_chunk"}
)
NON_CHAPTER_LOCATOR_KINDS = STRUCTURE_SLICE_KINDS - {"chapter"}


class ScanIdentityError(ValueError):
    """Raised when scan metadata or candidate pages no longer match their bindings."""


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _build_source_pack(
    *,
    schema_version: int,
    identity_key: str,
    identity_value: str,
    relative_paths: tuple[str, ...],
) -> dict[str, Any]:
    files: list[dict[str, str]] = []
    for relative in sorted(relative_paths):
        path = (_PROJECT_ROOT / relative).resolve()
        try:
            path.relative_to(_PROJECT_ROOT)
        except ValueError as error:
            raise ScanIdentityError("scanner rule-pack path escapes the project root") from error
        if not path.is_file():
            raise ScanIdentityError(f"runtime rule-pack file is missing: {relative}")
        files.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "schema_version": schema_version,
        identity_key: identity_value,
        "files": files,
    }


def build_scan_rule_pack(scanner: str) -> dict[str, Any]:
    """Return the current scanner implementation identity without host paths/mtimes."""
    relative_paths = _SCAN_RULE_PACK_FILES.get(scanner)
    if relative_paths is None:
        raise ScanIdentityError(f"scanner has no declared rule pack: {scanner!r}")
    return _build_source_pack(
        schema_version=SCAN_RULE_PACK_SCHEMA_VERSION,
        identity_key="scanner",
        identity_value=scanner,
        relative_paths=relative_paths,
    )


def build_draft_rule_pack() -> dict[str, Any]:
    """Return the current deterministic ad-draft implementation identity."""
    return _build_source_pack(
        schema_version=DRAFT_RULE_PACK_SCHEMA_VERSION,
        identity_key="component",
        identity_value="ads_draft",
        relative_paths=_DRAFT_RULE_PACK_FILES,
    )


def build_review_protocol_identity(
    *, target_page_bytes: int, hard_page_bytes: int
) -> dict[str, Any]:
    """Bind the bounded Agent projection implementation and byte-budget policy."""
    if (
        not isinstance(target_page_bytes, int)
        or isinstance(target_page_bytes, bool)
        or not isinstance(hard_page_bytes, int)
        or isinstance(hard_page_bytes, bool)
        or target_page_bytes < 1
        or hard_page_bytes < target_page_bytes
    ):
        raise ScanIdentityError("review protocol byte limits are invalid")
    source_pack = _build_source_pack(
        schema_version=REVIEW_PROTOCOL_IDENTITY_SCHEMA_VERSION,
        identity_key="component",
        identity_value="ads_agent_review",
        relative_paths=_REVIEW_PROTOCOL_FILES,
    )
    return {
        "schema_version": REVIEW_PROTOCOL_IDENTITY_SCHEMA_VERSION,
        "source_pack": source_pack,
        "target_page_bytes": target_page_bytes,
        "hard_page_bytes": hard_page_bytes,
    }


def build_profile_identity(profile_path: Path) -> dict[str, Any]:
    """Bind draft behavior to the optional profile's semantic JSON and file bytes."""
    if profile_path.exists():
        try:
            value = book_profile.load_book_profile(profile_path)
        except (OSError, ValueError) as error:
            raise ScanIdentityError("book profile does not satisfy the public schema") from error
        return {
            "profile_present": True,
            "book_profile_sha256": canonical_json_sha256(value),
            "book_profile_file_sha256": sha256_file(profile_path),
        }
    return {
        "profile_present": False,
        "book_profile_sha256": canonical_json_sha256({}),
        "book_profile_file_sha256": None,
    }


def _scan_id_payload(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scan_identity_schema_version": identity["scan_identity_schema_version"],
        "scanner": identity["scanner"],
        "input_sha256": identity["input_sha256"],
        "structure_sha256": identity["structure_sha256"],
        "config_sha256": identity["config_sha256"],
        "candidate_set_sha256": identity["candidate_set_sha256"],
        "scan_rule_pack_sha256": identity["scan_rule_pack_sha256"],
    }


def _candidate_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _candidate_payload(item)
            for key, item in value.items()
            if key not in _HASH_KEYS_EXCLUDED_FROM_CANDIDATE
        }
    if isinstance(value, list):
        return [_candidate_payload(item) for item in value]
    return value


def candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    return canonical_json_sha256(_candidate_payload(candidate))


def attach_candidate_fingerprints(candidates: list[dict[str, Any]]) -> None:
    for candidate in candidates:
        candidate["candidate_fingerprint"] = candidate_fingerprint(candidate)


def _expected_anchor_id(
    candidate_fingerprint_value: str,
    anchor_index: int,
    anchor: Mapping[str, Any],
) -> str:
    anchor_payload = {
        key: value
        for key, value in anchor.items()
        if key != "anchor_id"
    }
    digest = canonical_json_sha256(
        {
            "candidate_fingerprint": candidate_fingerprint_value,
            "anchor_index": anchor_index,
            "anchor": anchor_payload,
        }
    )
    return f"AN-{digest}"


def attach_anchor_ids(candidates: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for candidate in candidates:
        fingerprint = candidate.get("candidate_fingerprint")
        if not _is_sha256(fingerprint) or fingerprint != candidate_fingerprint(candidate):
            raise ScanIdentityError("candidate fingerprint must be attached before anchor IDs")
        anchors = candidate.get("anchors", [])
        if not isinstance(anchors, list) or not all(isinstance(anchor, dict) for anchor in anchors):
            raise ScanIdentityError("candidate anchors are invalid")
        for anchor_index, anchor in enumerate(anchors):
            anchor_id = _expected_anchor_id(fingerprint, anchor_index, anchor)
            if anchor_id in seen:
                raise ScanIdentityError("anchor ID is duplicated")
            anchor["anchor_id"] = anchor_id
            seen.add(anchor_id)


def validate_anchor_ids(candidates: list[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for candidate in candidates:
        fingerprint = candidate.get("candidate_fingerprint")
        if not _is_sha256(fingerprint) or fingerprint != candidate_fingerprint(candidate):
            raise ScanIdentityError("candidate fingerprint is missing or invalid")
        anchors = candidate.get("anchors", [])
        if not isinstance(anchors, list) or not all(isinstance(anchor, Mapping) for anchor in anchors):
            raise ScanIdentityError("candidate anchors are invalid")
        for anchor_index, anchor in enumerate(anchors):
            anchor_id = anchor.get("anchor_id")
            expected = _expected_anchor_id(fingerprint, anchor_index, anchor)
            if anchor_id != expected or anchor_id in seen:
                raise ScanIdentityError("anchor ID is invalid or duplicated")
            seen.add(anchor_id)


def candidate_set_sha256(candidates: list[Mapping[str, Any]]) -> str:
    fingerprints: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        fingerprint = candidate.get("candidate_fingerprint")
        if not _is_sha256(fingerprint):
            raise ScanIdentityError("candidate fingerprint is missing or invalid")
        if fingerprint != candidate_fingerprint(candidate):
            raise ScanIdentityError("candidate fingerprint does not match candidate content")
        if fingerprint in seen:
            raise ScanIdentityError("candidate fingerprint is duplicated")
        seen.add(fingerprint)
        fingerprints.append(fingerprint)
    return canonical_json_sha256(sorted(fingerprints))


def validate_candidate_set(candidates: list[Mapping[str, Any]]) -> str:
    candidate_ids: set[str] = set()
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in candidate_ids:
            raise ScanIdentityError("candidate ID is missing, invalid, or duplicated")
        candidate_ids.add(candidate_id)
    validate_anchor_ids(candidates)
    return candidate_set_sha256(candidates)


def load_bound_structure(input_path: Path, structure_path: Path) -> dict[str, Any]:
    try:
        structure = json.loads(structure_path.read_text(encoding="utf-8-sig"))
        text = input_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScanIdentityError("structure artifact is not valid UTF-8 JSON") from error
    if not isinstance(structure, dict) or structure.get("schema_version") != 2:
        raise ScanIdentityError("structure artifact schema_version must be 2")
    if structure.get("input_sha256") != sha256_file(input_path):
        raise ScanIdentityError("structure artifact is stale for the selected input")

    chapters = structure.get("chapters")
    slices = structure.get("slices")
    locators = structure.get("locators")
    fallback = structure.get("fallback_chunking")
    fallback_chunks = structure.get("fallback_chunks")
    if not isinstance(chapters, list) or not all(isinstance(item, dict) for item in chapters):
        raise ScanIdentityError("structure chapters must be a list of objects")
    if not isinstance(slices, list) or not slices or not all(isinstance(item, dict) for item in slices):
        raise ScanIdentityError("structure slices must be a non-empty list of objects")
    if locators != slices:
        raise ScanIdentityError("structure locators must match the complete slice model")

    cursor = 0
    for item in slices:
        if item.get("kind") not in STRUCTURE_SLICE_KINDS:
            raise ScanIdentityError("structure slice kind is invalid")
        start = item.get("start_offset")
        heading_end = item.get("heading_end_offset")
        end = item.get("end_offset")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(heading_end, int)
            or isinstance(heading_end, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start != cursor
            or not start <= heading_end <= end <= len(text)
        ):
            raise ScanIdentityError("structure slices do not cover the input exactly")
        cursor = end
    if cursor != len(text):
        raise ScanIdentityError("structure slices do not cover the input exactly")

    if not isinstance(fallback, dict) or not isinstance(fallback.get("enabled"), bool):
        raise ScanIdentityError("structure fallback metadata is invalid")
    if not isinstance(fallback_chunks, list) or not all(
        isinstance(item, dict) for item in fallback_chunks
    ):
        raise ScanIdentityError("structure fallback chunks are invalid")
    declared_count = fallback.get("chunk_count")
    if (
        not isinstance(declared_count, int)
        or isinstance(declared_count, bool)
        or declared_count != len(fallback_chunks)
        or fallback["enabled"] != bool(fallback_chunks)
    ):
        raise ScanIdentityError("structure fallback metadata is incomplete")
    if fallback_chunks:
        stripped_slices = [
            {key: value for key, value in item.items() if key != "heading_end_offset"}
            for item in slices
        ]
        if (
            chapters
            or any(item.get("kind") != "fallback_chunk" for item in slices)
            or any(item.get("heading_end_offset") != item.get("start_offset") for item in slices)
            or stripped_slices != fallback_chunks
        ):
            raise ScanIdentityError("structure fallback chunks do not match the slice model")
    elif any(item.get("kind") == "fallback_chunk" for item in slices):
        raise ScanIdentityError("structure fallback slice is not declared")
    elif not chapters:
        only = slices[0] if len(slices) == 1 else None
        if (
            not isinstance(only, dict)
            or only.get("kind") != "body"
            or only.get("start_offset") != 0
            or only.get("heading_end_offset") != 0
            or only.get("end_offset") != len(text)
        ):
            raise ScanIdentityError("structure without chapters must contain one complete body slice")
    else:
        chapter_slices = [item for item in slices if item.get("kind") == "chapter"]
        stripped_chapters = [
            {key: value for key, value in item.items() if key != "kind"}
            for item in chapter_slices
        ]
        first_start = chapters[0].get("start_offset")
        front_slices = [item for item in slices if item.get("kind") == "front_matter"]
        expected_front_count = 1 if isinstance(first_start, int) and first_start > 0 else 0
        if (
            stripped_chapters != chapters
            or len(front_slices) != expected_front_count
            or any(item.get("kind") not in {"front_matter", "chapter"} for item in slices)
        ):
            raise ScanIdentityError("structure chapters do not match the slice model")
        if front_slices and (
            slices[0] is not front_slices[0]
            or front_slices[0].get("start_offset") != 0
            or front_slices[0].get("heading_end_offset") != 0
            or front_slices[0].get("end_offset") != first_start
        ):
            raise ScanIdentityError("structure front matter does not match the first chapter")
    return structure


def build_scan_identity(
    scanner: str,
    input_path: Path,
    structure_path: Path,
    config: Mapping[str, Any],
    candidates: list[Mapping[str, Any]],
) -> dict[str, Any]:
    if not scanner:
        raise ScanIdentityError("scanner name must not be empty")
    candidate_hash = validate_candidate_set(candidates)
    rule_pack = build_scan_rule_pack(scanner)
    identity = {
        "scan_identity_schema_version": SCAN_IDENTITY_SCHEMA_VERSION,
        "scanner": scanner,
        "input_sha256": sha256_file(input_path),
        "structure_sha256": sha256_file(structure_path),
        "config_sha256": canonical_json_sha256(config),
        "candidate_set_sha256": candidate_hash,
        "scan_rule_pack": rule_pack,
        "scan_rule_pack_sha256": canonical_json_sha256(rule_pack),
    }
    identity["scan_id"] = canonical_json_sha256(_scan_id_payload(identity))
    return identity


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_scan_identity(
    workspace: Path,
    report: Mapping[str, Any],
    candidates: list[Mapping[str, Any]],
    *,
    allow_pending: bool = False,
) -> None:
    candidate_hash = validate_candidate_set(candidates)
    workspace = workspace.resolve()
    scanner = report.get("scanner")
    if not isinstance(scanner, str) or not scanner:
        raise ScanIdentityError("scan report scanner is missing or invalid")
    if report.get("scan_identity_schema_version") != SCAN_IDENTITY_SCHEMA_VERSION:
        raise ScanIdentityError("scan identity schema version is missing or stale")
    for key in (
        "scan_id",
        "input_sha256",
        "structure_sha256",
        "config_sha256",
        "candidate_set_sha256",
        "scan_rule_pack_sha256",
    ):
        if not _is_sha256(report.get(key)):
            raise ScanIdentityError(f"scan report {key} is missing or invalid")

    declared_rule_pack = report.get("scan_rule_pack")
    if not isinstance(declared_rule_pack, Mapping):
        raise ScanIdentityError("scan report rule pack is missing or invalid")
    if canonical_json_sha256(declared_rule_pack) != report["scan_rule_pack_sha256"]:
        raise ScanIdentityError("scan report rule pack hash is invalid")
    current_rule_pack = build_scan_rule_pack(scanner)
    if (
        dict(declared_rule_pack) != current_rule_pack
        or report["scan_rule_pack_sha256"] != canonical_json_sha256(current_rule_pack)
    ):
        raise ScanIdentityError("scan report does not match the current scanner rule pack")

    stage_name = _STAGE_BY_SCANNER.get(scanner)
    manifest = load_manifest(workspace)
    stage = manifest.get("stages", {}).get(stage_name, {}) if stage_name else {}
    active_statuses = (
        _ACTIVE_SCAN_STATUSES | {"pending"}
        if allow_pending
        else _ACTIVE_SCAN_STATUSES
    )
    if not isinstance(stage, Mapping) or stage.get("status") not in active_statuses:
        raise ScanIdentityError("scan stage is not active in the workspace manifest")
    for key in (
        "scan_identity_schema_version",
        "scanner",
        "scan_id",
        "input_sha256",
        "structure_sha256",
        "config_sha256",
        "candidate_set_sha256",
        "scan_rule_pack",
        "scan_rule_pack_sha256",
    ):
        if stage.get(key) != report.get(key):
            raise ScanIdentityError(f"scan report {key} does not match the committed scan")
    structure_value = report.get("structure", "meta/chapters.json")
    if not isinstance(structure_value, str) or not structure_value:
        raise ScanIdentityError("scan report structure path is missing or invalid")
    if "structure" in report and stage.get("structure") != structure_value:
        raise ScanIdentityError(
            "scan report structure path does not match the committed scan"
        )

    input_value = report.get("input")
    if not isinstance(input_value, str):
        raise ScanIdentityError("scan report input path is missing or invalid")
    input_path = (workspace / input_value).resolve()
    structure_path = (workspace / structure_value).resolve()
    try:
        input_path.relative_to(workspace)
        structure_path.relative_to(workspace)
    except ValueError as error:
        raise ScanIdentityError(
            "scan report input or structure path escapes the workspace"
        ) from error
    if not input_path.is_file() or not structure_path.is_file():
        raise ScanIdentityError("scan input or structure file is missing")
    if sha256_file(input_path) != report["input_sha256"]:
        raise ScanIdentityError("scan input hash does not match the report")
    if sha256_file(structure_path) != report["structure_sha256"]:
        raise ScanIdentityError("scan structure hash does not match the report")
    load_bound_structure(input_path, structure_path)

    scan_config = report.get("scan_config")
    if not isinstance(scan_config, Mapping):
        raise ScanIdentityError("scan report config is missing or invalid")
    if canonical_json_sha256(scan_config) != report["config_sha256"]:
        raise ScanIdentityError("scan config hash does not match the report")
    if candidate_hash != report["candidate_set_sha256"]:
        raise ScanIdentityError("candidate set hash does not match the report")

    expected_scan_id = canonical_json_sha256(_scan_id_payload(report))
    if expected_scan_id != report["scan_id"]:
        raise ScanIdentityError("scan ID does not match the report bindings")


def load_validated_pages(
    workspace: Path,
    report: Mapping[str, Any],
    *,
    allow_pending: bool = False,
) -> list[dict[str, Any]]:
    workspace = workspace.resolve()
    pages = report.get("pages")
    if not isinstance(pages, Mapping):
        raise ScanIdentityError("page metadata is missing or invalid")
    pages_dir_value = pages.get("pages_dir")
    first_page_value = pages.get("first_page")
    page_count = pages.get("page_count")
    manifest = pages.get("manifest")
    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        raise ScanIdentityError("scan summary is missing or invalid")
    page_size = summary.get("page_size")
    total_count = summary.get("total_candidate_count")
    if (
        not isinstance(pages_dir_value, str)
        or not isinstance(first_page_value, str)
        or not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count < 0
        or not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count < 0
    ):
        raise ScanIdentityError("page directory or count is invalid")
    if not isinstance(manifest, list) or len(manifest) != page_count:
        raise ScanIdentityError("page manifest count does not match page_count")

    pages_dir = (workspace / pages_dir_value).resolve()
    try:
        pages_dir.relative_to(workspace)
    except ValueError as error:
        raise ScanIdentityError("page directory escapes the workspace") from error
    if not pages_dir.is_dir():
        raise ScanIdentityError("page directory is missing")

    expected_numbers = list(range(1, page_count + 1))
    numbers: list[int] = []
    files: list[str] = []
    hashes: list[str] = []
    records: list[dict[str, Any]] = []
    for manifest_index, entry in enumerate(manifest):
        if not isinstance(entry, Mapping):
            raise ScanIdentityError("page manifest entry is invalid")
        file_value = entry.get("file")
        page_number = entry.get("page_number")
        record_count = entry.get("record_count")
        expected_sha256 = entry.get("sha256")
        if (
            not isinstance(file_value, str)
            or not isinstance(page_number, int)
            or not isinstance(record_count, int)
            or record_count < 0
            or not _is_sha256(expected_sha256)
        ):
            raise ScanIdentityError("page manifest entry fields are invalid")
        page_path = (workspace / file_value).resolve()
        try:
            page_path.relative_to(pages_dir)
        except ValueError as error:
            raise ScanIdentityError("page file is outside the declared page directory") from error
        if not page_path.is_file():
            raise ScanIdentityError("declared page file is missing")
        if sha256_file(page_path) != expected_sha256:
            raise ScanIdentityError("page file hash does not match the manifest")
        try:
            page_records = [
                json.loads(line)
                for line in page_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ScanIdentityError("page file is not valid UTF-8 JSONL") from error
        if len(page_records) != record_count or not all(isinstance(item, dict) for item in page_records):
            raise ScanIdentityError("page record count or shape does not match the manifest")
        if record_count < 1 or record_count > page_size:
            raise ScanIdentityError("page record count is outside the declared page size")
        if manifest_index < page_count - 1 and record_count != page_size:
            raise ScanIdentityError("a non-final page is not full")
        numbers.append(page_number)
        files.append(file_value)
        hashes.append(expected_sha256)
        records.extend(page_records)

    if numbers != expected_numbers:
        raise ScanIdentityError("page numbers are missing, duplicated, or out of order")
    if len(files) != len(set(files)) or len(hashes) != len(set(hashes)):
        raise ScanIdentityError("page files or page hashes are duplicated")
    actual_files = {
        path.relative_to(workspace).as_posix()
        for path in pages_dir.iterdir()
        if path.is_file() and path.suffix == ".jsonl"
    }
    if actual_files != set(files):
        raise ScanIdentityError("page directory does not match the declared page manifest")
    expected_page_count = (total_count + page_size - 1) // page_size
    if len(records) != total_count or page_count != expected_page_count:
        raise ScanIdentityError("page records do not match the reported complete candidate count")

    first_page_path = (workspace / first_page_value).resolve()
    try:
        first_page_path.relative_to(workspace)
    except ValueError as error:
        raise ScanIdentityError("first-page file escapes the workspace") from error
    try:
        first_page_records = [
            json.loads(line)
            for line in first_page_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScanIdentityError("first-page file is not valid UTF-8 JSONL") from error
    if first_page_records != records[:page_size]:
        raise ScanIdentityError("first-page candidates do not match the declared page sequence")

    validate_scan_identity(
        workspace,
        report,
        records,
        allow_pending=allow_pending,
    )
    return records
