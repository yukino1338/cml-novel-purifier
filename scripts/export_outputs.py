from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import unicodedata
import uuid
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    ExternalDeliveryTransaction,
    WorkspaceTransaction,
    load_manifest,
    read_utf8,
    recover_external_delivery_transactions,
    resolve_current_head,
    resolve_external_output_dir,
    resolve_external_output_paths,
    resolve_in_workspace,
    resolve_workspace_paths,
    sha256_file,
    validate_workspace,
    workspace_protected_paths,
    workspace_transaction_lock,
    write_json,
    write_utf8,
)
from book_profile import load_book_profile, verified_title
from normalize_layout import load_config
from parse_structure import parse as parse_chapters
import scan_identity
from verify import (
    PROVENANCE_IDENTITY_FIELDS,
    REQUIRED_VERIFY_CHECKS,
    VERIFY_RULE_VERSION,
)


PROTECTED_NAME_RE = re.compile(r"(?:改版|修改|改\d*|同人|二创|番外|外传|续写|衍生|魔改)", re.I)
NOISY_NAME_RE = re.compile(
    r"(?:https?|www\.|\.com|\.net|\.cn|\.org|\.cc|\.vip|@|下载|全本|txt|epub|soushu|cihetxt|biquge|xbiquge|9xiaoxs)",
    re.I,
)


ALL_FORMATS = ("txt", "markdown", "epub")
DEFAULT_FORMATS = ("txt",)


def normalize_requested_formats(
    requested_formats: Iterable[str] | None,
) -> tuple[str, ...]:
    if requested_formats is None:
        return DEFAULT_FORMATS
    if isinstance(requested_formats, str):
        raise ValueError("requested_formats must be an iterable of format names")
    values = tuple(requested_formats)
    if any(not isinstance(value, str) for value in values):
        raise ValueError("requested_formats must contain only format names")
    selected = set(values)
    unsupported = selected.difference(ALL_FORMATS)
    if unsupported:
        raise ValueError(f"unsupported export format: {sorted(unsupported)[0]}")
    normalized = tuple(kind for kind in ALL_FORMATS if kind in selected)
    if not normalized:
        raise ValueError("export requires at least one requested format")
    return normalized


def choose_input(workspace: Path, value: str) -> Path:
    if value != "auto":
        return resolve_in_workspace(workspace, value, role="read")
    return resolve_current_head(workspace)


def require_export_attestation(workspace: Path, input_path: Path) -> dict[str, Any]:
    manifest = load_manifest(workspace)
    input_rel = input_path.relative_to(workspace).as_posix()
    current_head = manifest.get("current_head")
    if current_head != input_rel:
        raise ValueError("export input must be the verified manifest current_head")
    stages = manifest.get("stages")
    verify_stage = stages.get("6_verify") if isinstance(stages, dict) else None
    if not isinstance(verify_stage, dict) or verify_stage.get("status") != "passed":
        raise ValueError("export requires a passed verification attestation")
    attestation = verify_stage.get("attestation")
    if (
        not isinstance(attestation, dict)
        or attestation.get("schema_version") != 3
        or attestation.get("rule_version") != VERIFY_RULE_VERSION
        or attestation.get("status") != "passed"
    ):
        raise ValueError("export verification attestation is missing or invalid")
    checks = attestation.get("checks")
    check_names = [
        check.get("name")
        for check in checks
        if isinstance(check, dict)
    ] if isinstance(checks, list) else []
    if (
        not isinstance(checks, list)
        or set(check_names) != REQUIRED_VERIFY_CHECKS
        or len(check_names) != len(REQUIRED_VERIFY_CHECKS)
        or any(not isinstance(check, dict) or check.get("passed") is not True for check in checks)
    ):
        raise ValueError("export verification attestation has incomplete or blocking checks")
    warnings = verify_stage.get("warnings", [])
    if not isinstance(warnings, list) or warnings:
        raise ValueError("export verification attestation contains warnings")

    current_sha256 = sha256_file(input_path)
    expected = {
        "verification_run_id": verify_stage.get("run_id"),
        "current_head": input_rel,
        "current_head_sha256": current_sha256,
        "decision_sha256": verify_stage.get("decision_sha256"),
        "formal_run_id": verify_stage.get("formal_run_id"),
        "formal_report_sha256": verify_stage.get("formal_report_sha256"),
        "scan_id": verify_stage.get("scan_id"),
        "candidate_set_sha256": verify_stage.get("candidate_set_sha256"),
        "apply_output": verify_stage.get("apply_output"),
        "apply_output_sha256": verify_stage.get("apply_output_sha256"),
        "layout_run_id": verify_stage.get("layout_run_id"),
        "layout_config_sha256": verify_stage.get("layout_config_sha256"),
        **{field: verify_stage.get(field) for field in PROVENANCE_IDENTITY_FIELDS},
    }
    for field, value in expected.items():
        if attestation.get(field) != value:
            raise ValueError(f"export verification attestation has stale {field}")

    artifacts = manifest.get("artifacts")
    current_record = artifacts.get(input_rel) if isinstance(artifacts, dict) else None
    if not isinstance(current_record, dict):
        raise ValueError("export current head is not a committed artifact")
    for field, expected_value in (
        ("parent_path", current_record.get("parent_path")),
        ("parent_sha256", current_record.get("parent_sha256")),
    ):
        if attestation.get(field) != expected_value:
            raise ValueError(f"export verification attestation has stale {field}")

    report_rel = verify_stage.get("report")
    if (
        not isinstance(report_rel, str)
        or not isinstance(artifacts, dict)
        or report_rel not in artifacts
    ):
        raise ValueError("export verification report is not a committed artifact")
    report_path = resolve_in_workspace(workspace, report_rel, role="read")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (
        not isinstance(report, dict)
        or report.get("status") != "passed"
        or report.get("attestation") != attestation
    ):
        raise ValueError("export verification report does not match its attestation")

    current_scan_pack = scan_identity.build_scan_rule_pack("ads")
    current_draft_pack = scan_identity.build_draft_rule_pack()
    profile_value = attestation.get("profile")
    if not isinstance(profile_value, str) or not profile_value:
        raise ValueError("export verification attestation has no bound profile path")
    profile_path = resolve_in_workspace(workspace, profile_value, role="write")
    current_profile = scan_identity.build_profile_identity(profile_path)
    current_runtime = {
        "scan_rule_pack_sha256": scan_identity.canonical_json_sha256(
            current_scan_pack
        ),
        "draft_rule_pack_sha256": scan_identity.canonical_json_sha256(
            current_draft_pack
        ),
        "profile": profile_value,
        **current_profile,
    }
    for field in PROVENANCE_IDENTITY_FIELDS:
        if attestation.get(field) != current_runtime.get(field):
            raise ValueError(
                f"export verification attestation has stale current-runtime {field}"
            )

    attestation_sha256 = hashlib.sha256(
        json.dumps(
            attestation,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    source = manifest.get("source")
    return {
        "verification_run_id": verify_stage["run_id"],
        "attestation_sha256": attestation_sha256,
        "report_sha256": artifacts[report_rel]["sha256"],
        "source_sha256": source.get("sha256") if isinstance(source, dict) else None,
        "decision_sha256": attestation["decision_sha256"],
        "input_sha256": current_sha256,
        **{
            field: attestation.get(field)
            for field in PROVENANCE_IDENTITY_FIELDS
        },
    }


WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
PORTABLE_PATH_LIMIT = 240


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _is_windows_reserved(value: str) -> bool:
    stem = value.split(".", 1)[0].rstrip(" .").casefold()
    return stem in WINDOWS_RESERVED_NAMES


def safe_name(value: str, max_bytes: int = 90) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .") or "novel"
    if _is_windows_reserved(value):
        value = f"_{value}"
    value = _truncate_utf8(value, max_bytes).rstrip(" .") or "novel"
    if _is_windows_reserved(value):
        value = _truncate_utf8(f"_{value}", max_bytes).rstrip(" .") or "novel"
    return value


def portable_name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).rstrip(" .").casefold()


def portable_path_key(path: Path) -> str:
    return unicodedata.normalize("NFC", str(path.resolve(strict=False))).rstrip(" .").casefold()


def _child_name_taken(parent: Path, name: str) -> bool:
    key = portable_name_key(name)
    if not parent.exists():
        return False
    return any(portable_name_key(child.name) == key for child in parent.iterdir())


def _assert_portable_paths(paths: list[Path]) -> None:
    for path in paths:
        value = str(path.resolve(strict=False))
        if max(len(value), len(value.encode("utf-8"))) > PORTABLE_PATH_LIMIT:
            raise ValueError(f"output path is too long for portable delivery: {path}")


def report_path(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def source_stem(workspace: Path) -> str:
    manifest = load_manifest(workspace)
    source = manifest.get("source")
    if isinstance(source, dict) and source.get("name"):
        return Path(str(source["name"])).stem
    name = workspace.name
    if name.endswith(".cleanwork"):
        name = name[: -len(".cleanwork")]
    return Path(name).stem


def normalize_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(r"(?:小说|txt|epub|精校版|完结版|全本|下载)", "", value)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def is_close_name(left: str, right: str) -> bool:
    l_key = normalize_key(left)
    r_key = normalize_key(right)
    if not l_key or not r_key:
        return False
    if l_key == r_key:
        return True
    shorter, longer = sorted((l_key, r_key), key=len)
    return len(shorter) >= 3 and shorter in longer


def load_profile(workspace: Path) -> dict[str, Any]:
    return load_book_profile(workspace / "meta" / "book_profile.json")


def protected_context(profile: dict[str, Any], source: str) -> bool:
    parts = [source]
    for key in ("genre", "narrative_style", "summary"):
        value = profile.get(key)
        if isinstance(value, str):
            parts.append(value)
    return bool(PROTECTED_NAME_RE.search(" ".join(parts)))


def title_in_text(title: str, text: str) -> bool:
    key = normalize_key(title)
    return bool(key and len(key) >= 3 and key in normalize_key(text[:20_000]))


def character_support(profile: dict[str, Any], text: str) -> bool:
    names = profile.get("main_characters") or []
    if not isinstance(names, list):
        return False
    head = text[:60_000]
    return any(isinstance(name, str) and len(name.strip()) >= 2 and name.strip() in head for name in names)


def resolve_export_identity(workspace: Path, config: dict[str, Any], text: str) -> dict[str, Any]:
    profile = load_profile(workspace)
    export_cfg = config.get("export", {})
    source = source_stem(workspace)
    explicit_title = str(export_cfg.get("title") or "").strip()
    profile_title = verified_title(profile)
    candidate_title = explicit_title or profile_title
    author = str(export_cfg.get("author") or profile.get("author") or "Unknown")
    decision: dict[str, Any] = {
        "source_name": source,
        "candidate_title": candidate_title,
        "title": source,
        "author": author,
        "rename_applied": False,
        "reason": "no verified title; kept source filename",
        "signals": [],
    }
    if not candidate_title:
        return decision
    if explicit_title:
        decision.update(
            {
                "title": candidate_title,
                "rename_applied": normalize_key(candidate_title) != normalize_key(source),
                "reason": "explicit export.title from config",
                "signals": ["explicit_config"],
            }
        )
        return decision
    if protected_context(profile, source):
        decision["reason"] = "source filename contains protected variant marker; kept source filename"
        return decision

    source_noisy = bool(NOISY_NAME_RE.search(source))
    source_close = is_close_name(source, candidate_title)
    verified = bool(profile_title)
    signals: list[str] = []
    if verified:
        signals.append("verified_profile")
    if source_noisy:
        signals.append("noisy_source_name")
    if source_close:
        signals.append("source_title_overlap")
    if title_in_text(candidate_title, text):
        signals.append("title_in_opening_text")
    if character_support(profile, text):
        signals.append("profile_character_in_text")

    if source_close and not source_noisy:
        decision.update(
            {
                "reason": "source filename is already semantically close; kept source filename",
                "signals": signals,
            }
        )
        return decision
    if verified and (source_noisy or "title_in_opening_text" in signals or "profile_character_in_text" in signals):
        decision.update(
            {
                "title": candidate_title,
                "rename_applied": normalize_key(candidate_title) != normalize_key(source),
                "reason": "verified profile title with local support",
                "signals": signals,
            }
        )
        return decision

    decision.update({"reason": "title not sufficiently verified for rename; kept source filename", "signals": signals})
    return decision


def timestamped_output_dir(output_root: Path, title: str) -> Path:
    now = datetime.now()
    label = safe_name(title)
    date_prefix = now.strftime("%Y%m%d")
    if not output_root.exists() or not any(child.name.startswith(date_prefix) for child in output_root.iterdir()):
        candidate = output_root / f"{date_prefix}-{label}"
        _assert_portable_paths([candidate])
        return candidate
    minute_prefix = now.strftime("%Y%m%d-%H%M")
    candidate = output_root / f"{minute_prefix}-{label}"
    if not _child_name_taken(output_root, candidate.name):
        _assert_portable_paths([candidate])
        return candidate
    second_prefix = now.strftime("%Y%m%d-%H%M%S")
    candidate = output_root / f"{second_prefix}-{label}"
    if not _child_name_taken(output_root, candidate.name):
        _assert_portable_paths([candidate])
        return candidate
    index = 2
    while _child_name_taken(output_root, f"{candidate.name}-{index}"):
        index += 1
    candidate = output_root / f"{candidate.name}-{index}"
    _assert_portable_paths([candidate])
    return candidate


def unique_child_dir(parent: Path, title: str) -> Path:
    candidate = parent / safe_name(title)
    if not _child_name_taken(parent, candidate.name):
        _assert_portable_paths([candidate])
        return candidate
    index = 2
    while _child_name_taken(parent, f"{candidate.name}-{index}"):
        index += 1
    candidate = parent / f"{candidate.name}-{index}"
    _assert_portable_paths([candidate])
    return candidate


def reserved_child_dir(parent: Path, title: str, reserved: set[str]) -> Path:
    label = safe_name(title)
    candidate = parent / label
    index = 2
    while _child_name_taken(parent, candidate.name) or portable_path_key(candidate) in reserved:
        candidate = parent / f"{label}-{index}"
        index += 1
    _assert_portable_paths([candidate])
    return candidate


MARKDOWN_CHAPTER_MARKER = "<!-- cml-novel-purifier:chapter -->"


def markdown_from_text(text: str) -> str:
    parts: list[str] = []
    for item in chapter_slices(text):
        start = int(item["start_offset"])
        end = int(item["end_offset"])
        segment = text[start:end]
        if item["kind"] == "chapter":
            parts.append(f"{MARKDOWN_CHAPTER_MARKER}\n## {segment}")
        else:
            parts.append(segment)
    return "".join(parts)


def chapter_slices(text: str) -> list[dict[str, Any]]:
    _, report = parse_chapters(text)
    slices = report.get("slices")
    if not isinstance(slices, list) or not slices:
        raise ValueError("chapter parser did not return a complete slice model")
    result: list[dict[str, Any]] = []
    for position, item in enumerate(slices, 1):
        start = int(item["start_offset"])
        heading_end = int(item.get("heading_end_offset", start))
        end = int(item["end_offset"])
        result.append(
            {
                **item,
                "position": position,
                "title": str(item.get("title") or f"Section {position}"),
                "source_heading": (
                    text[start:heading_end] if item.get("kind") == "chapter" else ""
                ),
                "body": text[heading_end:end] if item.get("kind") == "chapter" else text[start:end],
            }
        )
    return result


def xhtml_for_chapter(
    chapter: dict[str, Any],
    language: str = "zh-CN",
) -> str:
    title = html.escape(str(chapter["title"]))
    document_language = html.escape(language, quote=True)
    source_body = html.escape(str(chapter["body"]), quote=False)
    body = f'<div class="source-body" xml:space="preserve">{source_body}</div>'
    heading = ""
    if chapter.get("kind") == "chapter":
        source_heading = html.escape(
            str(chapter.get("source_heading") or f"{chapter['title']}\n"),
            quote=False,
        )
        heading = (
            f'<h1 class="source-heading" xml:space="preserve">'
            f"{source_heading}</h1>"
        )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="{document_language}" xml:lang="{document_language}">
<head><title>{title}</title><link rel="stylesheet" type="text/css" href="../Styles/style.css"/></head>
<body>{heading}{body}</body>
</html>
"""


def write_epub(path: Path, text: str, title: str, author: str, language: str) -> None:
    chapters = chapter_slices(text)
    uid = str(uuid.uuid4())
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_items = [
        '<item id="nav" href="Text/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="style" href="Styles/style.css" media-type="text/css"/>',
    ]
    spine_items = []
    nav_items = []
    for chapter in chapters:
        position = int(chapter["position"])
        name = f"chapter-{position:04d}.xhtml"
        manifest_items.append(f'<item id="c{position}" href="Text/{name}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="c{position}"/>')
        nav_items.append(f'<li><a href="{name}">{html.escape(str(chapter["title"]))}</a></li>')

    content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">urn:uuid:{uid}</dc:identifier>
<dc:title>{html.escape(title)}</dc:title>
<dc:creator>{html.escape(author)}</dc:creator>
<dc:language>{html.escape(language)}</dc:language>
<meta property="dcterms:modified">{modified}</meta>
</metadata>
<manifest>
{chr(10).join(manifest_items)}
</manifest>
<spine>
{chr(10).join(spine_items)}
</spine>
</package>
"""
    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="{html.escape(language)}" xml:lang="{html.escape(language)}">
<head><title>{html.escape(title)}</title></head>
<body>
<nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops">
<h1>{html.escape(title)}</h1>
<ol>
{chr(10).join(nav_items)}
</ol>
</nav>
</body>
</html>
"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
    style = (
        "body{line-height:1.7;}"
        ".source-body{white-space:pre-wrap;}"
        "h1.source-heading{white-space:pre-wrap;text-align:center;}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", content_opf)
        zf.writestr("OEBPS/Text/nav.xhtml", nav)
        zf.writestr("OEBPS/Styles/style.css", style)
        for chapter in chapters:
            zf.writestr(
                f"OEBPS/Text/chapter-{int(chapter['position']):04d}.xhtml",
                xhtml_for_chapter(chapter, language),
            )


def semantic_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def markdown_body_text(value: str) -> str:
    lines = value.splitlines(keepends=True)
    result: list[str] = []
    marked_heading = False
    has_markers = any(line.rstrip("\r\n") == MARKDOWN_CHAPTER_MARKER for line in lines)
    for line in lines:
        if line.rstrip("\r\n") == MARKDOWN_CHAPTER_MARKER:
            marked_heading = True
            continue
        if marked_heading:
            if not line.startswith("## "):
                raise ValueError("Markdown chapter marker is not followed by a heading")
            result.append(line[3:])
            marked_heading = False
        elif not has_markers and line.startswith("## "):
            result.append(line[3:])
        else:
            result.append(line)
    if marked_heading:
        raise ValueError("Markdown chapter marker has no heading")
    return "".join(result)


def validate_epub(path: Path, expected_text: str) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (
                not entries
                or entries[0].filename != "mimetype"
                or entries[0].compress_type != zipfile.ZIP_STORED
                or archive.read("mimetype") != b"application/epub+zip"
            ):
                raise ValueError("EPUB mimetype entry is invalid")
            if (
                "META-INF/container.xml" not in names
                or len(names) != len(set(names))
            ):
                raise ValueError("EPUB is missing required package files")
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            container_namespace = {
                "container": "urn:oasis:names:tc:opendocument:xmlns:container",
            }
            if (
                container.tag
                != "{urn:oasis:names:tc:opendocument:xmlns:container}container"
            ):
                raise ValueError("EPUB container XML root is invalid")
            rootfiles = container.findall(
                ".//container:rootfile",
                container_namespace,
            )
            if len(rootfiles) != 1:
                raise ValueError("EPUB container must declare exactly one rootfile")
            rootfile = rootfiles[0]
            opf_name = rootfile.get("full-path")
            if (
                not isinstance(opf_name, str)
                or not opf_name
                or opf_name.startswith("/")
                or "\\" in opf_name
                or re.match(r"^[A-Za-z]:", opf_name)
                or any(part in {"", ".", ".."} for part in opf_name.split("/"))
            ):
                raise ValueError("EPUB container rootfile path is invalid")
            if rootfile.get("media-type") != "application/oebps-package+xml":
                raise ValueError("EPUB container rootfile media-type is invalid")
            if opf_name not in names or archive.getinfo(opf_name).is_dir():
                raise ValueError("EPUB container rootfile is missing")
            opf = ET.fromstring(archive.read(opf_name))
            package_dir = opf_name.rpartition("/")[0]
            package_prefix = f"{package_dir}/" if package_dir else ""
            namespace = {
                "opf": "http://www.idpf.org/2007/opf",
                "dc": "http://purl.org/dc/elements/1.1/",
                "xhtml": "http://www.w3.org/1999/xhtml",
            }
            if opf.tag != "{http://www.idpf.org/2007/opf}package":
                raise ValueError("EPUB package XML root is invalid")
            metadata_sections = opf.findall("opf:metadata", namespace)
            manifests = opf.findall("opf:manifest", namespace)
            spines = opf.findall("opf:spine", namespace)
            if (
                len(metadata_sections) != 1
                or len(manifests) != 1
                or len(spines) != 1
            ):
                raise ValueError("EPUB package sections are invalid")
            languages = metadata_sections[0].findall("dc:language", namespace)
            package_language = (
                (languages[0].text or "").strip()
                if len(languages) == 1
                else ""
            )
            if not package_language:
                raise ValueError("EPUB package language is missing or invalid")
            manifest_items = manifests[0].findall("opf:item", namespace)
            manifest_ids = [item.get("id") for item in manifest_items]
            spine_ids = [
                item.get("idref")
                for item in spines[0].findall("opf:itemref", namespace)
            ]
            if (
                any(not item_id for item_id in manifest_ids)
                or len(manifest_ids) != len(set(manifest_ids))
                or not set(spine_ids).issubset(manifest_ids)
            ):
                raise ValueError("EPUB manifest or spine identifiers are invalid")
            chapter_names = sorted(
                name
                for name in names
                if re.fullmatch(
                    rf"{re.escape(package_prefix)}Text/chapter-\d{{4}}\.xhtml",
                    name,
                )
            )
            if (
                not chapter_names
                or len(chapter_names) != len(set(chapter_names))
                or len(chapter_names) != len(spine_ids)
            ):
                raise ValueError("EPUB chapter files do not match the spine")
            items_by_id = {
                str(item.get("id")): {
                    "href": item.get("href"),
                    "media_type": item.get("media-type"),
                    "properties": set((item.get("properties") or "").split()),
                }
                for item in manifest_items
            }
            expected_media_types = {
                "Text/nav.xhtml": "application/xhtml+xml",
                "Styles/style.css": "text/css",
                **{
                    name.removeprefix(package_prefix): "application/xhtml+xml"
                    for name in chapter_names
                },
            }
            manifest_hrefs = [item["href"] for item in items_by_id.values()]
            if (
                len(manifest_hrefs) != len(set(manifest_hrefs))
                or set(manifest_hrefs) != set(expected_media_types)
                or any(
                    f"{package_prefix}{href}" not in names
                    for href in expected_media_types
                )
                or any(
                    items_by_id[item_id]["media_type"] != expected_media_types[href]
                    for item_id, href in (
                        (item_id, item["href"]) for item_id, item in items_by_id.items()
                    )
                )
            ):
                raise ValueError("EPUB manifest href or media-type mapping is invalid")
            nav_items = [
                item
                for item in items_by_id.values()
                if "nav" in item["properties"]
            ]
            if len(nav_items) != 1 or nav_items[0]["href"] != "Text/nav.xhtml":
                raise ValueError("EPUB navigation manifest item is invalid")
            nav = ET.fromstring(
                archive.read(f"{package_prefix}{nav_items[0]['href']}")
            )
            if nav.tag != "{http://www.w3.org/1999/xhtml}html":
                raise ValueError("EPUB navigation XML root is invalid")
            nav_bodies = nav.findall("xhtml:body", namespace)
            if len(nav_bodies) != 1:
                raise ValueError("EPUB navigation body is invalid")
            nav_body = nav_bodies[0]
            xml_language = "{http://www.w3.org/XML/1998/namespace}lang"
            if (
                nav.get("lang") != package_language
                or nav.get(xml_language) != package_language
            ):
                raise ValueError("EPUB navigation language does not match the package")
            spine_hrefs = [items_by_id[str(item_id)]["href"] for item_id in spine_ids]
            expected_chapter_hrefs = [
                name.removeprefix(package_prefix) for name in chapter_names
            ]
            if spine_hrefs != expected_chapter_hrefs:
                raise ValueError("EPUB spine order does not match the chapter files")
            toc = [
                item
                for item in nav_body.findall(".//xhtml:nav", namespace)
                if item.get("{http://www.idpf.org/2007/ops}type") == "toc"
            ]
            nav_hrefs = (
                [item.get("href") for item in toc[0].findall(".//xhtml:a", namespace)]
                if len(toc) == 1
                else []
            )
            if nav_hrefs != [Path(href).name for href in spine_hrefs]:
                raise ValueError("EPUB navigation does not match the spine")
            extracted_parts: list[str] = []
            for name in (f"{package_prefix}{href}" for href in spine_hrefs):
                document = ET.fromstring(archive.read(name))
                if document.tag != "{http://www.w3.org/1999/xhtml}html":
                    raise ValueError(f"EPUB chapter XML root is invalid: {name}")
                if (
                    document.get("lang") != package_language
                    or document.get(xml_language) != package_language
                ):
                    raise ValueError(
                        f"EPUB chapter language does not match the package: {name}"
                    )
                bodies = document.findall(
                    "{http://www.w3.org/1999/xhtml}body"
                )
                if not bodies:
                    raise ValueError(f"EPUB chapter has no XHTML body: {name}")
                if len(bodies) != 1:
                    raise ValueError(f"EPUB chapter body is invalid: {name}")
                body = bodies[0]
                source_body = body.find(
                    "{http://www.w3.org/1999/xhtml}div[@class='source-body']"
                )
                if source_body is None:
                    extracted_parts.append("".join(body.itertext()))
                    continue
                source_heading = body.find(
                    "{http://www.w3.org/1999/xhtml}h1[@class='source-heading']"
                )
                if source_heading is not None:
                    extracted_parts.append("".join(source_heading.itertext()))
                extracted_parts.append("".join(source_body.itertext()))
    except (KeyError, OSError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise ValueError("EPUB package self-check failed") from exc
    extracted = semantic_text("".join(extracted_parts))
    expected = semantic_text(expected_text)
    if extracted != expected:
        raise ValueError("EPUB semantic text does not match the verified input")
    return {
        "passed": True,
        "chapter_count": len(chapter_names),
        "semantic_char_count": len(extracted),
    }


def export_file_names(title: str) -> dict[str, str]:
    label = safe_name(title)
    return {
        "txt": f"{label}.txt",
        "markdown": f"{label}.md",
        "epub": f"{label}.epub",
    }


def prepare_export(
    workspace: Path,
    input_value: str,
    config_path: Path | None,
    output_root: Path | None = None,
    *,
    requested_formats: Iterable[str] | None = None,
    delivery_workspaces: tuple[Path, ...] | None = None,
    reserved_output_dirs: set[str] | None = None,
) -> dict[str, Any]:
    with workspace_transaction_lock(workspace):
        return _prepare_export_locked(
            workspace,
            input_value,
            config_path,
            output_root,
            requested_formats=requested_formats,
            delivery_workspaces=delivery_workspaces,
            reserved_output_dirs=reserved_output_dirs,
        )


def _prepare_export_locked(
    workspace: Path,
    input_value: str,
    config_path: Path | None,
    output_root: Path | None = None,
    *,
    requested_formats: Iterable[str] | None = None,
    delivery_workspaces: tuple[Path, ...] | None = None,
    reserved_output_dirs: set[str] | None = None,
) -> dict[str, Any]:
    requested_formats = normalize_requested_formats(requested_formats)
    workspace, _, _ = resolve_workspace_paths(workspace)
    input_path = choose_input(workspace, input_value)
    input_rel = input_path.relative_to(workspace).as_posix()
    verification = require_export_attestation(workspace, input_path)
    config_inputs: set[Path] = set()
    config = load_config(config_path, config_inputs)
    internal_writes = {"report": "report/export_report.json"}
    if output_root is None:
        internal_writes["output_root"] = "output"
    workspace, reads, writes = resolve_workspace_paths(
        workspace,
        reads={
            "input": input_rel,
            "profile": "meta/book_profile.json",
        },
        writes=internal_writes,
        protected_paths=config_inputs,
    )
    input_path = reads["input"]
    protected_inputs = (*reads.values(), *config_inputs)
    workspaces = delivery_workspaces or (workspace,)

    external_root: Path | None = None
    if output_root is not None:
        external_root = resolve_external_output_dir(output_root, workspaces=workspaces)
        recover_external_delivery_transactions(external_root)

    text = read_utf8(input_path)
    identity = resolve_export_identity(workspace, config, text)
    title = str(identity["title"])
    all_names = export_file_names(title)
    names = {kind: all_names[kind] for kind in requested_formats}

    if external_root is None:
        internal_root = writes["output_root"]
        candidate_dir = timestamped_output_dir(internal_root, title)
        output_values = {
            "output_root": internal_root.relative_to(workspace).as_posix(),
            "output_dir": candidate_dir.relative_to(workspace).as_posix(),
            **{
                key: (candidate_dir / name).relative_to(workspace).as_posix()
                for key, name in names.items()
            },
            "report": "report/export_report.json",
        }
        workspace, reads, output_paths = resolve_workspace_paths(
            workspace,
            reads={name: path.relative_to(workspace).as_posix() for name, path in reads.items()},
            writes=output_values,
            protected_paths=config_inputs,
        )
        output_root_path = output_paths["output_root"]
    else:
        if reserved_output_dirs is None:
            candidate_dir = unique_child_dir(external_root, title)
        else:
            candidate_dir = reserved_child_dir(external_root, title, reserved_output_dirs)
        candidate_dir = resolve_external_output_dir(candidate_dir, workspaces=workspaces)
        output_values = {
            "output_dir": candidate_dir.relative_to(external_root).as_posix(),
            **{
                key: (candidate_dir / name).relative_to(external_root).as_posix()
                for key, name in names.items()
            },
        }
        output_paths = resolve_external_output_paths(
            external_root,
            writes=output_values,
            workspaces=workspaces,
            inputs=(*protected_inputs, writes["report"]),
        )
        output_root_path = external_root

    _assert_portable_paths([output_paths["output_dir"], *(output_paths[key] for key in names)])

    return {
        "workspace": workspace,
        "input_path": input_path,
        "protected_inputs": protected_inputs,
        "output_root": output_root_path,
        "output_dir": output_paths["output_dir"],
        "output_paths": {key: output_paths[key] for key in names},
        "requested_formats": requested_formats,
        "report_path": writes["report"],
        "text": text,
        "identity": identity,
        "title": title,
        "author": str(identity["author"]),
        "language": str(config.get("export", {}).get("language", "zh-CN")),
        "verification": verification,
    }


def write_export_outputs(
    plan: dict[str, Any],
    paths: dict[str, Path],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    workspace = plan["workspace"]
    final_paths = plan["output_paths"]
    text = plan["text"]
    outputs: dict[str, str] = {}
    checks: dict[str, dict[str, Any]] = {}
    if "txt" in paths:
        shutil.copyfile(plan["input_path"], paths["txt"])
        if paths["txt"].read_bytes() != plan["input_path"].read_bytes():
            raise ValueError("TXT export bytes do not match the verified current head")
        outputs["txt"] = report_path(final_paths["txt"], workspace)
        checks["txt"] = {"passed": True, "kind": "byte_exact"}
    if "markdown" in paths:
        write_utf8(paths["markdown"], markdown_from_text(text))
        if semantic_text(markdown_body_text(read_utf8(paths["markdown"]))) != semantic_text(text):
            raise ValueError("Markdown semantic text does not match the verified input")
        outputs["markdown"] = report_path(final_paths["markdown"], workspace)
        checks["markdown"] = {"passed": True, "kind": "semantic_text"}
    if "epub" in paths:
        write_epub(paths["epub"], text, plan["title"], plan["author"], plan["language"])
        outputs["epub"] = report_path(final_paths["epub"], workspace)
        checks["epub"] = validate_epub(paths["epub"], text)
        checks["epub"]["kind"] = "epub_package_and_semantic_text"
    artifacts = {
        kind: {
            "path": outputs[kind],
            "sha256": sha256_file(paths[kind]),
            "size_bytes": paths[kind].stat().st_size,
            "self_check": checks[kind],
        }
        for kind in outputs
    }
    return outputs, artifacts


def export_report(
    plan: dict[str, Any],
    outputs: dict[str, str],
    output_artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    workspace = plan["workspace"]
    requested_formats = list(plan["requested_formats"])
    produced_formats = [kind for kind in requested_formats if kind in outputs]
    if produced_formats != requested_formats or set(outputs) != set(requested_formats):
        raise ValueError("export did not produce every requested format")
    return {
        "status": "passed",
        "workspace": str(workspace),
        "input": report_path(plan["input_path"], workspace),
        "title": plan["title"],
        "author": plan["author"],
        "output_dir": report_path(plan["output_dir"], workspace),
        "output_dir_abs": str(plan["output_dir"]),
        "naming": plan["identity"],
        "verification": plan["verification"],
        "requested_formats": requested_formats,
        "produced_formats": produced_formats,
        "primary_output": outputs[requested_formats[0]],
        "outputs": outputs,
        "output_artifacts": output_artifacts,
    }


def export_stage_update(plan: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    return {
        "input": report_path(plan["input_path"], plan["workspace"]),
        "report": "report/export_report.json",
        "output_dir": report_path(plan["output_dir"], plan["workspace"]),
        "requested_formats": report["requested_formats"],
        "produced_formats": report["produced_formats"],
        "primary_output": report["primary_output"],
        "outputs": report["outputs"],
        "output_artifacts": report["output_artifacts"],
        "verification": plan["verification"],
    }


def execute_export(plan: dict[str, Any]) -> dict[str, Any]:
    workspace = plan["workspace"]
    output_root = plan["output_root"]
    output_dir = plan["output_dir"]
    output_paths = plan["output_paths"]
    try:
        output_dir.relative_to(workspace)
        internal_delivery = True
    except ValueError:
        internal_delivery = False
    if internal_delivery:
        with WorkspaceTransaction(workspace) as transaction:
            transaction.stage_directory(output_dir, require_new=True)
            staged_outputs = {key: transaction.stage_path(path) for key, path in output_paths.items()}
            outputs, output_artifacts = write_export_outputs(plan, staged_outputs)
            report = export_report(plan, outputs, output_artifacts)
            write_json(transaction.stage_path(plan["report_path"]), report)
            transaction.commit({"7_export": ("done", export_stage_update(plan, report))})
    else:
        with ExternalDeliveryTransaction(
            output_root,
            workspaces=(workspace,),
            inputs=(*plan["protected_inputs"], plan["report_path"]),
        ) as delivery:
            delivery.stage_directory(output_dir, require_new=True)
            staged_outputs = {key: delivery.stage_path(path) for key, path in output_paths.items()}
            outputs, output_artifacts = write_export_outputs(plan, staged_outputs)
            report = export_report(plan, outputs, output_artifacts)
            with WorkspaceTransaction(workspace, run_id=delivery.run_id) as transaction:
                write_json(transaction.stage_path(plan["report_path"]), report)
                delivery.publish(commits=((workspace, "7_export", "done"),))
                transaction.commit({"7_export": ("done", export_stage_update(plan, report))})
                delivery.finalize()
    return report


def run(
    workspace: Path,
    input_value: str,
    config_path: Path | None,
    output_root: Path | None = None,
    *,
    requested_formats: Iterable[str] | None = None,
) -> dict[str, Any]:
    with workspace_transaction_lock(workspace):
        return execute_export(
            prepare_export(
                workspace,
                input_value,
                config_path,
                output_root,
                requested_formats=requested_formats,
            )
        )


def common_output_root(workspaces: list[Path]) -> Path:
    parents = [str(workspace.parent.resolve()) for workspace in workspaces]
    return Path(os.path.commonpath(parents)) / "output"


def run_batch(
    workspaces: list[Path],
    input_value: str,
    config_path: Path | None,
    output_root: Path | None = None,
    *,
    requested_formats: Iterable[str] | None = None,
) -> dict[str, Any]:
    requested_formats = normalize_requested_formats(requested_formats)
    workspaces = [validate_workspace(workspace) for workspace in workspaces]
    if not workspaces:
        raise ValueError("batch export requires at least one workspace")
    if len(workspaces) != len(set(workspaces)):
        raise ValueError("batch export workspaces must be unique")
    with ExitStack() as stack:
        for workspace in sorted(set(workspaces), key=str):
            stack.enter_context(workspace_transaction_lock(workspace))
        return _run_batch_locked(
            workspaces,
            input_value,
            config_path,
            output_root,
            requested_formats=requested_formats,
        )


def _run_batch_locked(
    workspaces: list[Path],
    input_value: str,
    config_path: Path | None,
    output_root: Path | None = None,
    *,
    requested_formats: Iterable[str] | None = None,
) -> dict[str, Any]:
    requested_formats = normalize_requested_formats(requested_formats)
    workspaces = [resolve_workspace_paths(workspace)[0] for workspace in workspaces]
    root = resolve_external_output_dir(
        output_root or common_output_root(workspaces),
        workspaces=workspaces,
    )
    recover_external_delivery_transactions(root)
    batch_dir = timestamped_output_dir(root, "多本小说")
    batch_dir = resolve_external_output_dir(batch_dir, workspaces=workspaces)
    batch_dir = resolve_external_output_paths(
        root,
        writes={"batch_dir": batch_dir.relative_to(root).as_posix()},
        workspaces=workspaces,
    )["batch_dir"]

    reserved: set[str] = set()
    plans: list[dict[str, Any]] = []
    failures: dict[Path, dict[str, Any]] = {}
    for workspace in workspaces:
        try:
            plan = prepare_export(
                workspace,
                input_value,
                config_path,
                batch_dir,
                requested_formats=requested_formats,
                delivery_workspaces=tuple(workspaces),
                reserved_output_dirs=reserved,
            )
        except (OSError, ValueError) as error:
            failures[workspace] = {
                "status": "failed",
                "workspace": str(workspace),
                "phase": "preflight",
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
                "outputs": {},
            }
            continue
        reserved.add(portable_path_key(plan["output_dir"]))
        plans.append(plan)

    delivery_writes = {"batch_report": "batch_export_report.json"}
    delivery_inputs: list[Path] = []
    for index, plan in enumerate(plans, 1):
        delivery_inputs.extend(plan["protected_inputs"])
        delivery_writes[f"item_{index}_dir"] = plan["output_dir"].relative_to(batch_dir).as_posix()
        for kind, path in plan["output_paths"].items():
            delivery_writes[f"item_{index}_{kind}"] = path.relative_to(batch_dir).as_posix()

    for plan_index, plan in enumerate(plans):
        protected: list[Path] = list(delivery_inputs)
        for workspace in workspaces:
            workspace_files = workspace_protected_paths(workspace)
            protected.extend(
                workspace_files[1:] if workspace == plan["workspace"] else workspace_files
            )
        protected.extend(
            other["report_path"]
            for other_index, other in enumerate(plans)
            if other_index != plan_index
        )
        _, _, report_write = resolve_workspace_paths(
            plan["workspace"],
            writes={
                "report": plan["report_path"].relative_to(plan["workspace"]).as_posix(),
            },
            protected_paths=protected,
        )
        plan["report_path"] = report_write["report"]

    external_protected = [*delivery_inputs, *(plan["report_path"] for plan in plans)]
    delivery_paths = resolve_external_output_paths(
        batch_dir,
        writes=delivery_writes,
        workspaces=workspaces,
        inputs=external_protected,
    )

    commits = tuple((plan["workspace"], "7_export", "done") for plan in plans)
    with ExternalDeliveryTransaction(
        root,
        workspaces=workspaces,
        inputs=external_protected,
    ) as delivery:
        delivery.stage_directory(batch_dir, require_new=True)
        passed_items: dict[Path, dict[str, Any]] = {}
        for plan in plans:
            staged_outputs = {
                key: delivery.stage_path(path)
                for key, path in plan["output_paths"].items()
            }
            outputs, output_artifacts = write_export_outputs(plan, staged_outputs)
            passed_items[plan["workspace"]] = export_report(plan, outputs, output_artifacts)
        items = [
            passed_items.get(workspace) or failures[workspace]
            for workspace in workspaces
        ]
        success_count = len(passed_items)
        failure_count = len(failures)
        report = {
            "status": (
                "passed"
                if failure_count == 0
                else "failed"
                if success_count == 0
                else "partial"
            ),
            "count": len(workspaces),
            "success_count": success_count,
            "failure_count": failure_count,
            "output_dir": str(batch_dir),
            "output_dir_abs": str(batch_dir),
            "requested_formats": list(requested_formats),
            "items": items,
        }
        write_json(delivery.stage_path(delivery_paths["batch_report"]), report)

        with ExitStack() as stack:
            transactions: list[tuple[WorkspaceTransaction, dict[str, Any], dict[str, Any]]] = []
            for plan in plans:
                item = passed_items[plan["workspace"]]
                transaction = stack.enter_context(
                    WorkspaceTransaction(plan["workspace"], run_id=delivery.run_id)
                )
                write_json(transaction.stage_path(plan["report_path"]), item)
                transactions.append((transaction, plan, item))

            delivery.publish(commits=commits)
            for transaction, plan, item in transactions:
                transaction.commit(
                    {"7_export": ("done", export_stage_update(plan, item))},
                    defer_cleanup=True,
                    group_commits=commits,
                )
            for transaction, _, _ in transactions:
                transaction.finalize()
            delivery.finalize()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Export verified novel text in requested formats.")
    parser.add_argument("workspace", nargs="+", help="Path(s) to .cleanwork directories.")
    parser.add_argument("--input", default="auto")
    parser.add_argument("--config", help="Path to JSON config template.")
    parser.add_argument("--output-root", help="Optional external output root.")
    formats = parser.add_mutually_exclusive_group()
    formats.add_argument(
        "--format",
        action="append",
        choices=ALL_FORMATS,
        dest="requested_formats",
        help="Export one format; repeat to request more than one.",
    )
    formats.add_argument(
        "--all-formats",
        action="store_true",
        help="Export TXT, Markdown, and EPUB as one atomic bundle.",
    )
    args = parser.parse_args()
    workspaces = [Path(value).resolve() for value in args.workspace]
    config_path = Path(args.config).resolve() if args.config else None
    output_root = Path(args.output_root).resolve() if args.output_root else None
    requested_formats = (
        ALL_FORMATS
        if args.all_formats
        else normalize_requested_formats(args.requested_formats)
    )
    if len(workspaces) == 1:
        report = run(
            workspaces[0],
            args.input,
            config_path,
            output_root,
            requested_formats=requested_formats,
        )
    else:
        report = run_batch(
            workspaces,
            args.input,
            config_path,
            output_root,
            requested_formats=requested_formats,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "passed":
        sys.exit(1)


if __name__ == "__main__":
    main()
