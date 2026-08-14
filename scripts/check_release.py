from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml

from ad_rules import SITE_SPECS


FRONTMATTER_KEYS = {"name", "description"}
OPENAI_INTERFACE_KEYS = {
    "display_name",
    "short_description",
    "default_prompt",
}
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PRIVATE_ABSOLUTE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\s\"'<>]+|"
    r"/(?:Users|home)/[^/\s]+/[^\s\"'<>]+)",
    re.IGNORECASE,
)
GENERIC_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9:])(?:[A-Za-z]:[\\/]|\\\\|//)[^\s\"'<>`]+"
)
GENERIC_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![\w:/><])/(?!/)[^\s\"'<>`]+"
)
GENERIC_FILE_URI = re.compile(r"(?i)(?<![A-Za-z0-9])file:/+[^\s\"'<>`]+")
STRICT_PATH_PRIVACY_FILES = frozenset({"AGENTS.md", "README.md", "SKILL.md"})
STRICT_PATH_PRIVACY_DIRS = frozenset({"agents", "references"})
MARKDOWN_LINK = re.compile(
    r"(!?)\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^)\s]+)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)]*\)))?\s*\)"
)
URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
MAX_PUBLIC_FILE_BYTES = 2 * 1024 * 1024
POLYFORM_NONCOMMERCIAL_BODY_SHA256 = (
    "df23779e856a6e0136819f4072e903a9edcd091cbe4526e59e0fa6c4ec44bdd1"
)
POLYFORM_REQUIRED_NOTICE_PREFIX = "Required Notice: "
POLYFORM_REQUIRED_NOTICE_MARKER = "\n\n" + POLYFORM_REQUIRED_NOTICE_PREFIX
PUBLIC_TOP_LEVEL_FILES = frozenset(
    {
        ".gitattributes",
        ".gitignore",
        "AGENTS.md",
        "LICENSE.txt",
        "README.md",
        "SKILL.md",
        "pyproject.toml",
        "requirements-dev.txt",
    }
)
PUBLIC_TOP_LEVEL_DIRS = frozenset(
    {".github", "agents", "assets", "docs", "references", "scripts", "tests"}
)
PUBLIC_TOP_LEVEL = PUBLIC_TOP_LEVEL_FILES | PUBLIC_TOP_LEVEL_DIRS
PUBLIC_TEXT_SUFFIXES = frozenset(
    {".css", ".js", ".json", ".jsonl", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"}
)
PUBLIC_TEXT_NAMES = frozenset({".gitattributes", ".gitignore"})
NON_UTF8_PUBLIC_PATHS = frozenset(
    {
        "tests/fixtures/encodings/big5.txt",
        "tests/fixtures/encodings/gb18030.txt",
    }
)
REQUIRED_RELEASE_PATHS = (
    ".gitattributes",
    ".gitignore",
    ".github/workflows/ci.yml",
    "AGENTS.md",
    "LICENSE.txt",
    "README.md",
    "SKILL.md",
    "agents/openai.yaml",
    "docs/images/hero.webp",
    "docs/images/review-desktop.webp",
    "docs/images/review-mobile.webp",
    "pyproject.toml",
    "requirements-dev.txt",
)
LOCAL_TOP_LEVEL = frozenset(
    {
        ".experiment-work",
        ".git",
        ".local-design",
        ".ruff_cache",
        ".venv",
        ".vscode",
        "1小说",
        "benchmarks",
        "output",
    }
)
EXCLUDED_DIRS = {
    ".git",
    ".experiment-work",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
SITE_SIGNATURE_SOURCE_PATHS = frozenset(
    {"scripts/ad_rules.py", "scripts/export_outputs.py"}
)
SITE_SIGNATURE_TOKENS = tuple(
    sorted(
        {
            token.strip().casefold()
            for spec in SITE_SPECS
            for token in (spec.label, *spec.aliases, *spec.domain_fragments)
            if token.strip()
        },
        key=lambda token: (-len(token), token),
    )
)


def is_local_release_path(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = relative.parts
    if not parts:
        return False
    if (
        parts[0] in LOCAL_TOP_LEVEL
        or any(part in EXCLUDED_DIRS or part.endswith(".cleanwork") for part in parts)
    ):
        return True
    name = path.name.casefold()
    return (
        name == ".coverage"
        or (name.startswith("coverage") and name.endswith(".json"))
        or path.suffix.casefold() in {".pyc", ".pstats"}
    )


def is_allowed_public_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = relative.parts
    if len(parts) == 1:
        return parts[0] in PUBLIC_TOP_LEVEL_FILES
    top = parts[0]
    suffix = path.suffix.casefold()
    if top == ".github":
        return relative.as_posix() == ".github/workflows/ci.yml"
    if top == "agents":
        return relative.as_posix() == "agents/openai.yaml"
    if top == "assets":
        return len(parts) == 3 and (
            (parts[1] == "config-templates" and suffix in {".json", ".jsonl"})
            or (parts[1] == "review" and suffix in {".css", ".js"})
        )
    if top == "docs":
        return len(parts) == 3 and parts[1] == "images" and suffix == ".webp"
    if top == "references":
        return len(parts) == 2 and suffix == ".md"
    if top == "scripts":
        return len(parts) == 2 and suffix == ".py"
    if top == "tests":
        portable = relative.as_posix()
        return (
            (len(parts) == 2 and suffix == ".py")
            or portable == "tests/forward_trials_summary.json"
            or (
                len(parts) >= 3
                and parts[1] == "fixtures"
                and suffix in {".json", ".jsonl", ".txt"}
            )
            or portable
            in {
                "tests/performance/scan_baseline_ci.json",
                "tests/performance/scan_baseline_full.json",
                "tests/performance/review_projection_baseline.json",
            }
        )
    return False


def is_public_utf8_text(path: Path, root: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    return relative not in NON_UTF8_PUBLIC_PATHS and (
        path.name in PUBLIC_TEXT_NAMES or path.suffix.casefold() in PUBLIC_TEXT_SUFFIXES
    )


def validate_non_utf8_fixture_attributes(
    root: Path,
    public_relatives: set[str],
) -> list[str]:
    """Require Git to preserve the raw bytes of declared encoding fixtures."""

    present = sorted(NON_UTF8_PUBLIC_PATHS.intersection(public_relatives))
    if not present:
        return []
    attributes_path = root / ".gitattributes"
    try:
        lines = attributes_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return [f".gitattributes cannot be read for encoding fixtures: {exc}"]

    no_text_paths: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 2:
            continue
        pattern, attributes = fields[0], set(fields[1:])
        if "binary" in attributes or "-text" in attributes:
            no_text_paths.add(pattern)
    errors = [
        f"{relative} must be marked binary or -text in .gitattributes"
        for relative in present
        if relative not in no_text_paths
    ]
    if not (root / ".git").exists():
        return errors

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "check-attr",
                "-z",
                "binary",
                "text",
                "--",
                *present,
            ],
            capture_output=True,
            timeout=20,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return errors + [f"Git attribute verification failed: {exc}"]
    if result.returncode != 0:
        return errors + [
            f"Git attribute verification failed with exit code {result.returncode}"
        ]
    try:
        fields = result.stdout.decode("utf-8").split("\0")
    except UnicodeError as exc:
        return errors + [f"Git attribute output is not valid UTF-8: {exc}"]
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) != len(present) * 2 * 3:
        return errors + ["Git attribute output has an unexpected shape"]

    effective: dict[str, dict[str, str]] = {relative: {} for relative in present}
    for index in range(0, len(fields), 3):
        relative, attribute, value = fields[index : index + 3]
        if relative not in effective or attribute not in {"binary", "text"}:
            return errors + ["Git attribute output contains an unexpected fixture record"]
        effective[relative][attribute] = value
    for relative in present:
        attributes = effective[relative]
        # A later `text` rule overrides the conversion behavior even when an
        # earlier `binary` rule still appears in Git's attribute listing.
        # Raw-byte fixtures therefore need an effective text=unset, not merely
        # an earlier binary token in the file.
        if attributes.get("text") != "unset":
            errors.append(
                f"{relative} is converted as Git text despite its raw-byte fixture contract"
            )
    return errors


def requires_strict_path_privacy(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return (
        len(relative.parts) == 1 and relative.name in STRICT_PATH_PRIVACY_FILES
    ) or relative.parts[0] in STRICT_PATH_PRIVACY_DIRS


def is_link_or_junction(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def public_file_inventory(root: Path) -> tuple[list[Path], list[str]]:
    """List public regular files without following links or Windows junctions."""

    root = root.resolve()
    files: list[Path] = []
    errors: list[str] = []

    def inspect_directory(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            errors.append(f"{display_path(directory, root)} cannot be inspected: {exc}")
            return
        for entry in entries:
            path = Path(entry.path)
            if is_local_release_path(path, root):
                continue
            if is_link_or_junction(path):
                errors.append(
                    f"{display_path(path, root)} is a release-tree link or junction"
                )
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    inspect_directory(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(path)
                else:
                    errors.append(
                        f"{display_path(path, root)} is not a regular release-tree file or directory"
                    )
            except OSError as exc:
                errors.append(f"{display_path(path, root)} cannot be inspected: {exc}")

    try:
        children = sorted(os.scandir(root), key=lambda entry: entry.name.casefold())
    except OSError as exc:
        return [], [f"release root cannot be inspected: {exc}"]
    for entry in children:
        child = Path(entry.path)
        if is_local_release_path(child, root):
            continue
        if entry.name not in PUBLIC_TOP_LEVEL:
            errors.append(f"{entry.name} is not in the top-level release allowlist")
            continue
        if is_link_or_junction(child):
            errors.append(
                f"{display_path(child, root)} is a release-tree link or junction"
            )
            continue
        try:
            is_file = entry.is_file(follow_symlinks=False)
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError as exc:
            errors.append(f"{entry.name} cannot be inspected: {exc}")
            continue
        if entry.name in PUBLIC_TOP_LEVEL_FILES:
            if not is_file:
                errors.append(f"{entry.name} must be a release-tree file")
            else:
                files.append(child)
            continue
        if not is_dir:
            errors.append(f"{entry.name} must be a release-tree directory")
            continue
        inspect_directory(child)
    return files, errors


def validate_tracked_paths(root: Path, relative_paths: Iterable[str]) -> list[str]:
    """Reject tracked entries that are outside the public release allowlist."""

    root = root.resolve()
    errors: list[str] = []
    for raw_path in relative_paths:
        if (
            not raw_path
            or "\\" in raw_path
            or "\0" in raw_path
            or re.match(r"^[A-Za-z]:", raw_path)
        ):
            errors.append("Git tracks a non-portable release path")
            continue
        portable = PurePosixPath(raw_path)
        if not portable.parts or portable.is_absolute() or ".." in portable.parts:
            errors.append("Git tracks a path outside the release root")
            continue
        path = root.joinpath(*portable.parts)
        label = portable.as_posix()
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            errors.append(f"{label} is a tracked path outside the release root")
            continue
        if not is_allowed_public_file(path, root):
            errors.append(f"{label} is tracked but not allowed in the public release")
            continue
        if is_link_or_junction(path):
            errors.append(f"{label} is a tracked release-tree link or junction")
        elif not path.is_file():
            errors.append(f"{label} is tracked but is not a regular release-tree file")
    return errors


def validate_git_tracked_inventory(root: Path) -> list[str]:
    """Require one committed public tree and cross-check its tracked inventory."""

    root = root.resolve()
    if not (root / ".git").exists():
        return []
    environment = os.environ.copy()
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            timeout=20,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"Git HEAD verification failed: {exc}"]
    if head.returncode != 0:
        return ["Git checkout has no resolvable HEAD commit"]
    try:
        head_value = head.stdout.decode("ascii").strip()
    except UnicodeError as exc:
        return [f"Git HEAD is not valid ASCII: {exc}"]
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head_value):
        return ["Git HEAD did not resolve to a commit object ID"]

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            timeout=20,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"Git tracked-file inventory failed: {exc}"]
    if result.returncode != 0:
        return [
            "Git tracked-file inventory failed with exit code "
            f"{result.returncode}"
        ]
    try:
        output = result.stdout.decode("utf-8")
    except UnicodeError as exc:
        return [f"Git tracked-file inventory is not valid UTF-8: {exc}"]
    tracked = [item for item in output.split("\0") if item]
    errors = validate_tracked_paths(root, tracked)
    if not tracked:
        errors.append("Git checkout has no tracked public files")

    public_files, _ = public_file_inventory(root)
    public_relatives = {
        path.relative_to(root).as_posix()
        for path in public_files
        if is_allowed_public_file(path, root)
    }
    tracked_relatives = set(tracked)
    for relative in sorted(public_relatives - tracked_relatives):
        errors.append(f"{relative} is public but not tracked by Git")
    return errors


def validate_polyform_noncommercial_license(license_text: str) -> list[str]:
    """Require the unmodified official license body plus project notice lines."""
    trimmed = license_text.rstrip("\r\n")
    if POLYFORM_REQUIRED_NOTICE_MARKER not in trimmed:
        return [
            "LICENSE.txt must contain the complete official PolyForm Noncommercial "
            "1.0.0 text and at least one Required Notice line"
        ]

    official_text, notice_suffix = trimmed.split(POLYFORM_REQUIRED_NOTICE_MARKER, 1)
    official_sha256 = hashlib.sha256(official_text.encode("utf-8")).hexdigest()
    if official_sha256 != POLYFORM_NONCOMMERCIAL_BODY_SHA256:
        return [
            "LICENSE.txt must contain the complete official PolyForm Noncommercial "
            "1.0.0 text"
        ]

    notices = (POLYFORM_REQUIRED_NOTICE_PREFIX + notice_suffix).splitlines()
    if not notices or any(
        not line.startswith(POLYFORM_REQUIRED_NOTICE_PREFIX)
        or not line.removeprefix(POLYFORM_REQUIRED_NOTICE_PREFIX).strip()
        for line in notices
    ):
        return [
            "LICENSE.txt must contain only non-empty Required Notice lines after "
            "the official PolyForm Noncommercial 1.0.0 text"
        ]
    return []


def validate_public_tree(root: Path) -> list[str]:
    root = root.resolve()
    public_files, errors = public_file_inventory(root)
    public_relatives = {path.relative_to(root).as_posix() for path in public_files}
    errors.extend(validate_non_utf8_fixture_attributes(root, public_relatives))
    for relative in REQUIRED_RELEASE_PATHS:
        if relative not in public_relatives:
            errors.append(f"{relative} is missing from the release tree")

    license_path = root / "LICENSE.txt"
    if "LICENSE.txt" in public_relatives:
        try:
            license_text = license_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"LICENSE.txt is not valid UTF-8: {exc}")
        else:
            errors.extend(validate_polyform_noncommercial_license(license_text))

    root_tokens = {
        str(root).casefold(),
        root.as_posix().casefold(),
    }
    for path in public_files:
        label = display_path(path, root)
        if not is_allowed_public_file(path, root):
            errors.append(f"{label} is not allowed by the public release file rules")
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            errors.append(f"{label} cannot be inspected: {exc}")
            continue
        if size > MAX_PUBLIC_FILE_BYTES:
            errors.append(
                f"{label} exceeds the public file size limit of {MAX_PUBLIC_FILE_BYTES} bytes"
            )
        if not is_public_utf8_text(path, root):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{label} is not valid UTF-8 text: {exc}")
            continue
        folded = text.casefold()
        if (
            label not in SITE_SIGNATURE_SOURCE_PATHS
            and any(token in folded for token in SITE_SIGNATURE_TOKENS)
        ):
            errors.append(
                f"{label} repeats a production site signature outside approved rule sources"
            )
        has_private_path = any(token and token in folded for token in root_tokens) or bool(
            PRIVATE_ABSOLUTE_PATH.search(text)
        )
        if requires_strict_path_privacy(path, root):
            has_private_path = has_private_path or bool(
                GENERIC_WINDOWS_ABSOLUTE_PATH.search(text)
                or GENERIC_POSIX_ABSOLUTE_PATH.search(text)
                or GENERIC_FILE_URI.search(text)
            )
        if has_private_path:
            errors.append(f"{label} contains a private absolute path")
    return errors


def display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def load_yaml_mapping(path: Path, root: Path) -> tuple[dict[str, Any] | None, str | None]:
    label = display_path(path, root)
    if is_link_or_junction(path):
        return None, f"{label} is a release-tree link or junction"
    if not path.is_file():
        return None, f"{label} is missing"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return None, f"{label} is not valid UTF-8 YAML: {exc}"
    if not isinstance(data, dict):
        return None, f"{label} root must be a YAML object"
    return data, None


def validate_openai_interface(root: Path) -> list[str]:
    """Keep optional UI metadata portable across Skill hosts."""

    path = root / "agents" / "openai.yaml"
    data, error = load_yaml_mapping(path, root)
    if error:
        return [error]
    if set(data) != {"interface"}:
        return ["agents/openai.yaml keys must be exactly interface"]
    interface = data.get("interface")
    if not isinstance(interface, dict):
        return ["agents/openai.yaml interface must be a YAML object"]
    if set(interface) != OPENAI_INTERFACE_KEYS:
        return [
            "agents/openai.yaml interface keys must be exactly "
            + ", ".join(sorted(OPENAI_INTERFACE_KEYS))
        ]
    errors: list[str] = []
    for key in sorted(OPENAI_INTERFACE_KEYS):
        value = interface.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"agents/openai.yaml interface.{key} must be a non-empty string")
    prompt = interface.get("default_prompt")
    if isinstance(prompt, str) and "$cml-novel-purifier" not in prompt:
        errors.append("agents/openai.yaml default_prompt must invoke $cml-novel-purifier")
    return errors


def validate_frontmatter(root: Path) -> list[str]:
    path = root / "SKILL.md"
    if is_link_or_junction(path):
        return ["SKILL.md is a release-tree link or junction"]
    if not path.is_file():
        return ["SKILL.md is missing"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return [f"SKILL.md is not valid UTF-8: {exc}"]
    if not lines or lines[0] != "---":
        return ["SKILL.md must start with YAML frontmatter"]
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return ["SKILL.md frontmatter has no closing delimiter"]
    try:
        data = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError as exc:
        return [f"SKILL.md frontmatter is invalid YAML: {exc}"]
    if not isinstance(data, dict):
        return ["SKILL.md frontmatter must be a YAML object"]

    errors: list[str] = []
    keys = set(data)
    if keys != FRONTMATTER_KEYS:
        errors.append(
            "SKILL.md frontmatter keys must be exactly name and description; found: "
            + (", ".join(sorted(map(str, keys))) or "none")
        )
    name = data.get("name")
    if not isinstance(name, str) or not SKILL_NAME.fullmatch(name) or len(name) > 64:
        errors.append(
            "SKILL.md name must use at most 64 lowercase letters, digits, and hyphens"
        )
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("SKILL.md description must be a non-empty string")
    elif len(description) > 1024:
        errors.append("SKILL.md description must be at most 1024 characters")
    elif "<" in description or ">" in description:
        errors.append("SKILL.md description cannot contain angle brackets")
    return errors


def validate_markdown_links(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    public_files, _ = public_file_inventory(root)
    paths = sorted(
        (
            path
            for path in public_files
            if path.suffix.casefold() == ".md" and is_allowed_public_file(path, root)
        ),
        key=lambda path: path.as_posix(),
    )
    for path in paths:
        label = display_path(path, root)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            errors.append(f"{label} cannot be checked for Markdown links: {exc}")
            continue

        fence: str | None = None
        for line_number, line in enumerate(lines, 1):
            stripped = line.lstrip()
            marker = stripped[:3]
            if marker in {"```", "~~~"}:
                fence = None if fence == marker else marker if fence is None else fence
                continue
            if fence is not None:
                continue
            for match in MARKDOWN_LINK.finditer(line):
                is_image, alt_text, raw_target = match.groups()
                target = raw_target[1:-1] if raw_target.startswith("<") else raw_target
                if is_image and not alt_text.strip():
                    errors.append(f"{label}:{line_number} image alt text is empty")
                if (
                    not target
                    or target.startswith(("#", "//"))
                    or URL_SCHEME.match(target)
                ):
                    continue
                local_target = target.split("#", 1)[0].split("?", 1)[0]
                if not local_target:
                    continue
                candidate = (path.parent / local_target).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    errors.append(
                        f"{label}:{line_number} local Markdown target leaves the release root: "
                        f"{target}"
                    )
                    continue
                if not candidate.exists():
                    errors.append(
                        f"{label}:{line_number} local Markdown target does not exist: "
                        f"{target}"
                    )
    return errors


def excluded_json(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = relative.parts
    if is_local_release_path(path, root) or not parts or parts[0] not in PUBLIC_TOP_LEVEL:
        return True
    return len(parts) >= 3 and parts[:3] == ("tests", "fixtures", "malformed")


def validate_json_files(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    public_files, _ = public_file_inventory(root)
    paths = sorted(
        (
            path
            for path in public_files
            if path.suffix.casefold() in {".json", ".jsonl"}
        ),
        key=lambda path: path.as_posix(),
    )
    for path in paths:
        if excluded_json(path, root):
            continue
        label = display_path(path, root)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{label} is not valid UTF-8: {exc}")
            continue
        if path.suffix.casefold() == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append(f"{label}:{exc.lineno}:{exc.colno} is invalid JSON: {exc.msg}")
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{label}:{line_number} is invalid JSONL: {exc.msg}")
    return errors


def is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    return (
        isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def discover_cli_scripts(root: Path) -> tuple[list[Path], list[str]]:
    scripts: list[Path] = []
    errors: list[str] = []
    root = root.resolve()
    public_files, _ = public_file_inventory(root)
    paths = (
        path
        for path in public_files
        if path.parent == root / "scripts" and path.suffix.casefold() == ".py"
    )
    for path in sorted(paths, key=lambda item: item.name):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            errors.append(f"{display_path(path, root)} cannot be inspected for --help: {exc}")
            continue
        if any(isinstance(node, ast.If) and is_main_guard(node) for node in tree.body):
            scripts.append(path)
    return scripts, errors


def validate_cli_help(root: Path) -> list[str]:
    scripts, errors = discover_cli_scripts(root)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for path in scripts:
        label = display_path(path, root)
        try:
            result = subprocess.run(
                [sys.executable, str(path), "--help"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{label} --help failed: {exc}")
            continue
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            errors.append(f"{label} --help exited with {result.returncode}{suffix}")
    return errors


def validate_release(root: Path) -> list[str]:
    root = root.resolve()
    errors = validate_public_tree(root)
    errors.extend(validate_git_tracked_inventory(root))
    errors.extend(validate_frontmatter(root))
    errors.extend(validate_markdown_links(root))
    errors.extend(validate_openai_interface(root))
    for relative in (Path(".github/workflows/ci.yml"),):
        _, error = load_yaml_mapping(root / relative, root)
        if error:
            errors.append(error)
    errors.extend(validate_json_files(root))
    errors.extend(validate_cli_help(root))
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate repository release contracts.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to this script's parent repository)",
    )
    args = parser.parse_args()
    errors = validate_release(args.root)
    if errors:
        print("release gate failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    print("release gate passed")


if __name__ == "__main__":
    main()
