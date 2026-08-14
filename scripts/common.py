from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO, Callable, Iterable, Literal, Mapping


WORKSPACE_DIRS = (
    "versions",
    "input_repair",
    "meta",
    "candidates",
    "decisions",
    "logs",
    "report",
)

SKILL_PUBLIC_ROOT = Path(__file__).resolve().parents[1]
JOB_INPUT_DIR_NAME = "待清洗_Input"
JOB_RESULT_DIR_NAME = "小说清洗结果_Novel-Purifier"
JOB_INTERNAL_DIR_NAME = ".cml-novel-purifier"
JOB_WORKSPACES_DIR_NAME = "workspaces"

MANIFEST_SCHEMA_VERSION = 2
VALID_STAGE_STATUSES = frozenset(
    {
        "pending",
        "candidates_ready",
        "draft_decisions_ready",
        "formal_decisions_ready",
        "done",
        "passed",
        "blocked",
        "incomplete",
        "failed",
        "skipped",
    }
)
_RETRYABLE_STAGE_STATUSES = frozenset(
    {
        "pending",
        "candidates_ready",
        "draft_decisions_ready",
        "formal_decisions_ready",
        "done",
        "passed",
        "blocked",
        "incomplete",
        "failed",
    }
)
LEGAL_STAGE_TRANSITIONS = {
    "pending": VALID_STAGE_STATUSES,
    "candidates_ready": _RETRYABLE_STAGE_STATUSES | {"skipped"},
    "draft_decisions_ready": _RETRYABLE_STAGE_STATUSES | {"skipped"},
    "formal_decisions_ready": _RETRYABLE_STAGE_STATUSES | {"skipped"},
    "done": _RETRYABLE_STAGE_STATUSES,
    "passed": frozenset({"pending", "passed", "blocked", "incomplete", "failed"}),
    "blocked": _RETRYABLE_STAGE_STATUSES,
    "incomplete": _RETRYABLE_STAGE_STATUSES,
    "failed": frozenset({"pending", "failed"}),
    "skipped": frozenset({"pending", "skipped"}),
}
_FIXED_STAGE_NAMES = frozenset(
    {
        "0_preprocess",
        "1_parse_structure",
        "2_ads",
        "3_titles",
        "4_blocked_words",
        "5_layout",
        "6_verify",
        "7_export",
        "dry_run",
        "review",
        "rollback_all",
        "rollback_ads",
        "rollback_ads_chapter",
        "rollback_ads_point",
    }
)
_ROLLBACK_STAGE_RE = re.compile(r"rollback_ads_(?:chapter|point)_[A-Za-z0-9_-]+\Z")
INACTIVE_STAGE_STATUSES = frozenset({"pending", "blocked", "incomplete", "failed", "skipped"})
STAGE_INVALIDATION_MATRIX: dict[str, tuple[str, ...]] = {
    "0_preprocess": (
        "1_parse_structure",
        "2_ads",
        "3_titles",
        "4_blocked_words",
        "5_layout",
        "6_verify",
        "7_export",
        "dry_run",
        "review",
    ),
    "1_parse_structure": (
        "2_ads",
        "3_titles",
        "4_blocked_words",
        "5_layout",
        "6_verify",
        "7_export",
        "dry_run",
        "review",
    ),
    "2_ads": (
        "3_titles",
        "4_blocked_words",
        "5_layout",
        "6_verify",
        "7_export",
        "dry_run",
        "review",
    ),
    "3_titles": (
        "4_blocked_words",
        "5_layout",
        "6_verify",
        "7_export",
        "dry_run",
        "review",
    ),
    "4_blocked_words": ("5_layout", "6_verify", "7_export", "dry_run", "review"),
    "5_layout": ("6_verify", "7_export", "review"),
    "6_verify": ("7_export", "review"),
    "7_export": ("review",),
}

WorkspaceRole = Literal["read", "write"]


class WorkspacePathError(ValueError):
    """Raised when a path violates the clean-workspace boundary or file role."""


class WorkspaceIdentityError(ValueError):
    """Raised when a source, snapshot, or workspace identity cannot be verified."""


class WorkspaceTransactionError(RuntimeError):
    """Raised when a staged workspace run cannot be committed safely."""


def _valid_run_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_stage_name(value: object) -> bool:
    return isinstance(value, str) and (
        value in _FIXED_STAGE_NAMES or _ROLLBACK_STAGE_RE.fullmatch(value) is not None
    )


def stage_invalidation_targets(stage: str) -> tuple[str, ...]:
    if stage in STAGE_INVALIDATION_MATRIX:
        return STAGE_INVALIDATION_MATRIX[stage]
    if stage == "rollback_all":
        return (
            "0_preprocess",
            "1_parse_structure",
            "2_ads",
            "3_titles",
            "4_blocked_words",
            "5_layout",
            "6_verify",
            "7_export",
            "dry_run",
            "review",
        )
    if re.fullmatch(r"rollback_ads(?:_(?:chapter|point)(?:_[A-Za-z0-9_-]+)?)?", stage) is None:
        return ()
    ordered = (
        "2_ads",
        "3_titles",
        "4_blocked_words",
        "5_layout",
        "6_verify",
        "7_export",
        "dry_run",
        "review",
    )
    return ordered


class _PathTransactionLock:
    """Hold a non-blocking cross-process lock for one canonical transaction root."""

    _registry_guard = threading.Lock()
    _registry: dict[str, tuple[int, BinaryIO, list[bool]]] = {}

    def __init__(self, kind: str, root: Path, *, allow_children: bool = False) -> None:
        self.kind = kind
        resolved_root = _resolve_path(root, f"{kind} lock root")
        key = f"{kind}\0{os.path.normcase(str(resolved_root))}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / "cml-novel-purifier-locks" / f"{digest}.lock"
        self.file: BinaryIO | None = None
        self._acquired = False
        self._allow_children = allow_children

    def acquire(self) -> None:
        if self._acquired:
            raise WorkspaceTransactionError(f"{self.kind} transaction lock is already held")
        key = str(self.path)
        owner = threading.get_ident()
        with self._registry_guard:
            existing = self._registry.get(key)
            if existing is not None:
                existing_owner, handle, permissions = existing
                if existing_owner != owner or not permissions[-1]:
                    raise WorkspaceTransactionError(
                        f"another {self.kind} transaction is active; retry after it finishes"
                    )
                permissions.append(self._allow_children)
                self.file = handle
                self._acquired = True
                return

            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                raise WorkspaceTransactionError(
                    f"another {self.kind} transaction is active; retry after it finishes"
                ) from exc
            self._registry[key] = (owner, handle, [self._allow_children])
            self.file = handle
            self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        key = str(self.path)
        owner = threading.get_ident()
        with self._registry_guard:
            existing = self._registry.get(key)
            if existing is None or existing[0] != owner:
                raise WorkspaceTransactionError(f"{self.kind} transaction lock owner changed")
            _, handle, permissions = existing
            permissions.pop()
            if not permissions:
                del self._registry[key]
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
            self.file = None
            self._acquired = False

    def __enter__(self) -> "_PathTransactionLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()


def workspace_transaction_lock(workspace: Path) -> _PathTransactionLock:
    return _PathTransactionLock(
        "workspace",
        validate_workspace(workspace),
        allow_children=True,
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_utf8(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, write: Callable[[BinaryIO], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(fd, "wb") as f:
            fd = None
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_parent_directory(path)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _atomic_copy_file(source: Path, target: Path) -> None:
    def copy_bytes(output: BinaryIO) -> None:
        with source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)

    _atomic_write_bytes(target, copy_bytes)


def _restore_backup_file(backup: Path, target: Path) -> None:
    _atomic_copy_file(backup, target)


def write_utf8(path: Path, text: str) -> None:
    encoded = text.encode("utf-8")
    _atomic_write_bytes(path, lambda f: f.write(encoded))


def write_bytes(path: Path, data: bytes) -> None:
    _atomic_write_bytes(path, lambda f: f.write(data))


def write_json(path: Path, data: Any) -> None:
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, lambda f: f.write(encoded))


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    def write(f: BinaryIO) -> None:
        if path.exists():
            with path.open("rb") as existing:
                shutil.copyfileobj(existing, f, length=1024 * 1024)
        f.write(encoded)

    _atomic_write_bytes(path, write)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    def write(f: BinaryIO) -> None:
        for record in records:
            encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            f.write(encoded)

    _atomic_write_bytes(path, write)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"{path}:{line_no}: JSONL record must be an object")
        records.append(item)
    return records


def load_jsonl_for_run(path: Path, run_id: str) -> list[dict[str, Any]]:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    return [record for record in load_jsonl(path) if record.get("run_id") == run_id]


def _truncate_utf8_segment(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def portable_path_segment(value: str, *, max_bytes: int = 72) -> str:
    """Return one short cross-platform display segment, never a path."""

    normalized = unicodedata.normalize("NFC", value)
    normalized = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', "_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .") or "novel"
    normalized = _truncate_utf8_segment(normalized, max_bytes).rstrip(" .") or "novel"
    stem = normalized.split(".", 1)[0].casefold()
    if stem in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }:
        normalized = _truncate_utf8_segment(f"_{normalized}", max_bytes).rstrip(" .")
    return normalized or "novel"


def source_identity_id(source_sha256: str, source_path: Path) -> str:
    """Bind a short user-facing ID to immutable bytes and canonical source path."""

    if not _is_sha256(source_sha256):
        raise ValueError("source SHA-256 is invalid")
    source_path = _resolve_path(source_path, "source file")
    content = source_sha256[:12]
    path_key = os.path.normcase(str(source_path)).encode("utf-8")
    location = hashlib.sha256(path_key).hexdigest()[:6]
    return f"{content}-{location}"


def source_delivery_id(source: Path) -> str:
    """Return the shared ID for a readable source file."""

    source = _resolve_path(source, "source file")
    return source_identity_id(sha256_file(source), source)


def workspace_name_for_source(source: Path) -> str:
    source = _resolve_path(source, "source file")
    label = portable_path_segment(source.name, max_bytes=64)
    return f"{label}--{source_delivery_id(source)}.cleanwork"


def initialized_job_root_for_source(source: Path) -> Path | None:
    source = _resolve_path(source, "source file")
    input_dir = source.parent
    if input_dir.name != JOB_INPUT_DIR_NAME:
        return None
    # The bilingual input directory is itself the stable job-root marker.  Treat a
    # partially initialized root as repairable instead of silently nesting a second
    # workspace/result tree below the user's input directory.
    return input_dir.parent


def _existing_workspace_matches_source(source: Path, workspace: Path) -> bool:
    if not workspace.is_dir():
        return False
    try:
        workspace = validate_workspace(workspace)
        manifest = load_manifest(workspace)
        _validate_workspace_identity(
            source,
            workspace,
            workspace / "versions" / "v0_original.txt",
            manifest,
        )
    except (OSError, UnicodeError, ValueError):
        return False
    return True


def _inside_skill_public_root(path: Path) -> bool:
    path = _resolve_path(path, "path")
    public_root = _resolve_path(SKILL_PUBLIC_ROOT, "Skill public root")
    return _is_relative_to(path, public_root)


def workspace_for_source(source: Path, workspace: str | None = None) -> Path:
    """Resolve new workspaces by the frozen explicit/legacy/job/hidden priority."""

    source = _resolve_path(source, "source file")
    if workspace:
        selected = _resolve_path(Path(workspace), "workspace")
        if _inside_skill_public_root(selected):
            legacy = _resolve_path(
                source.with_name(source.name + ".cleanwork"), "legacy workspace"
            )
            # The public Skill tree is code, not a general-purpose work area.  The
            # only historical exception is the exact sibling workspace made by an
            # older release, and it must still prove the complete source/v0 binding.
            if (
                selected == legacy
                and selected.is_dir()
                and _existing_workspace_matches_source(source, selected)
            ):
                return selected
            raise WorkspacePathError(
                "only a complete matching legacy workspace may be used inside the Skill public root"
            )
        return selected

    legacy = source.with_name(source.name + ".cleanwork")
    if _existing_workspace_matches_source(source, legacy):
        return legacy
    if _inside_skill_public_root(source):
        raise WorkspacePathError(
            "a new source inside the Skill public root requires an explicit external workspace"
        )

    job_root = initialized_job_root_for_source(source)
    workspace_root = (
        job_root / JOB_INTERNAL_DIR_NAME / JOB_WORKSPACES_DIR_NAME
        if job_root is not None
        else source.parent / JOB_INTERNAL_DIR_NAME / JOB_WORKSPACES_DIR_NAME
    )
    return _resolve_path(
        workspace_root / workspace_name_for_source(source),
        "workspace",
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _same_file_or_path(left: Path, right: Path) -> bool:
    if os.path.normcase(str(left)) == os.path.normcase(str(right)):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _without_windows_extended_prefix(value: str) -> str:
    if os.name != "nt":
        return value
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _resolve_path(path: Path, label: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=False)
        return Path(_without_windows_extended_prefix(str(resolved)))
    except (OSError, RuntimeError) as exc:
        raise WorkspacePathError(f"cannot resolve {label}: {path}") from exc


def _validate_internal_value(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise WorkspacePathError("internal path must be a non-empty relative string")
    if "\x00" in value:
        raise WorkspacePathError("internal path contains a NUL byte")

    native = Path(value)
    windows = PureWindowsPath(value)
    if native.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        raise WorkspacePathError(f"absolute or drive-relative internal path is forbidden: {value}")

    native_parts = native.parts
    windows_parts = windows.parts
    if ".." in native_parts or ".." in windows_parts:
        raise WorkspacePathError(f"parent traversal is forbidden in internal paths: {value}")
    for part in windows_parts:
        if part in {"", "."}:
            continue
        if ":" in part:
            raise WorkspacePathError(f"alternate data stream syntax is forbidden: {value}")
        if part.endswith((" ", ".")):
            raise WorkspacePathError(f"trailing spaces or dots are forbidden: {value}")


def _validate_workspace_tree(workspace: Path) -> None:
    if not workspace.exists():
        raise WorkspacePathError(f"workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise WorkspacePathError(f"workspace is not a directory: {workspace}")

    stack = [workspace]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise WorkspacePathError(f"cannot inspect workspace directory: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            resolved = _resolve_path(path, "workspace path")
            if not _is_relative_to(resolved, workspace):
                raise WorkspacePathError(f"workspace link escapes its boundary: {path}")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspacePathError(f"cannot inspect workspace path: {path}") from exc
            attributes = getattr(info, "st_file_attributes", 0)
            is_reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            if entry.is_dir(follow_symlinks=False) and not entry.is_symlink() and not is_reparse:
                stack.append(path)


def validate_workspace(workspace: Path) -> Path:
    """Return a canonical workspace after rejecting any outbound link in its tree."""

    canonical = _resolve_path(Path(workspace), "workspace")
    _validate_workspace_tree(canonical)
    return canonical


def ensure_workspace(workspace: Path) -> Path:
    workspace = _resolve_path(Path(workspace), "workspace")
    if workspace.exists():
        _validate_workspace_tree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    for dirname in WORKSPACE_DIRS:
        (workspace / dirname).mkdir(exist_ok=True)
    _validate_workspace_tree(workspace)
    return workspace


def manifest_path(workspace: Path) -> Path:
    return _resolve_path(Path(workspace), "workspace") / "manifest.json"


def load_manifest(workspace: Path) -> dict[str, Any]:
    workspace = validate_workspace(workspace)
    path = manifest_path(workspace)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(workspace: Path, manifest: dict[str, Any]) -> None:
    workspace = validate_workspace(workspace)
    path = manifest_path(workspace)
    v0 = workspace / "versions" / "v0_original.txt"
    source_value = manifest.get("source", {}).get("path") if isinstance(manifest.get("source"), dict) else None
    protected = [v0]
    if isinstance(source_value, str) and source_value:
        source_path = Path(source_value)
        protected.append(
            _resolve_path(source_path, "manifest source")
            if source_path.is_absolute()
            else _resolve_path(workspace / source_path, "manifest source")
        )
    if any(_same_file_or_path(path, item) for item in protected):
        raise WorkspacePathError("manifest path aliases a protected source file")
    manifest["updated_at"] = now_iso()
    write_json(path, manifest)


def default_stages() -> dict[str, dict[str, Any]]:
    return {
        "0_preprocess": {
            "status": "pending",
            "input": "versions/v0_original.txt",
            "output": "versions/v1_preprocessed.txt",
        },
        "1_parse_structure": {
            "status": "pending",
            "input": "versions/v1_preprocessed.txt",
            "output": "meta/chapters.json",
        },
        "2_ads": {"status": "pending"},
        "3_titles": {"status": "pending"},
        "4_blocked_words": {"status": "pending"},
        "5_layout": {"status": "pending"},
        "6_verify": {"status": "pending"},
        "7_export": {"status": "pending"},
    }


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _artifact_record(
    path: Path,
    relative: str,
    *,
    run_id: str,
    stage: str,
    parent_path: str | None = None,
    parent_sha256: str | None = None,
    config_sha256: str | None = None,
    decision_sha256: str | None = None,
) -> dict[str, Any]:
    if not path.is_file():
        raise WorkspaceTransactionError(f"committed artifact is missing: {relative}")
    if not _valid_run_id(run_id):
        raise WorkspaceTransactionError("artifact run_id is invalid")
    for label, value in (
        ("parent_sha256", parent_sha256),
        ("config_sha256", config_sha256),
        ("decision_sha256", decision_sha256),
    ):
        if value is not None and not _is_sha256(value):
            raise WorkspaceTransactionError(f"artifact {label} is invalid")
    return {
        "path": relative,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "parent_path": parent_path,
        "parent_sha256": parent_sha256,
        "run_id": run_id,
        "stage": stage,
        "config_sha256": config_sha256,
        "decision_sha256": decision_sha256,
    }


def _validate_manifest_v2(workspace: Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise WorkspaceIdentityError(
            "unsupported workspace manifest schema; rebuild the workspace from the trusted source"
        )
    artifacts = manifest.get("artifacts")
    current_head = manifest.get("current_head")
    stages = manifest.get("stages")
    if not isinstance(artifacts, dict) or not artifacts:
        raise WorkspaceIdentityError("manifest v2 artifact ledger is missing")
    if not isinstance(current_head, str) or not current_head:
        raise WorkspaceIdentityError("manifest v2 current_head is missing")
    if not isinstance(stages, dict):
        raise WorkspaceIdentityError("manifest v2 stages are missing")

    for stage_name, stage_data in stages.items():
        if not _valid_stage_name(stage_name) or not isinstance(stage_data, dict):
            raise WorkspaceIdentityError("manifest stage entry is invalid")
        status = stage_data.get("status")
        if status not in VALID_STAGE_STATUSES:
            raise WorkspaceIdentityError(f"manifest stage status is invalid: {stage_name}")
        stage_artifacts = stage_data.get("artifacts", [])
        if not isinstance(stage_artifacts, list) or not all(
            isinstance(value, str) and value for value in stage_artifacts
        ):
            raise WorkspaceIdentityError(f"manifest stage artifacts are invalid: {stage_name}")
        if status not in INACTIVE_STAGE_STATUSES:
            if not _valid_run_id(stage_data.get("run_id")):
                raise WorkspaceIdentityError(f"active manifest stage run_id is invalid: {stage_name}")
            external_review = (
                stage_name == "review"
                and not stage_artifacts
                and isinstance(stage_data.get("html"), str)
                and Path(stage_data["html"]).is_absolute()
            )
            if not stage_artifacts and not external_review:
                raise WorkspaceIdentityError(
                    f"active manifest stage has no committed artifacts: {stage_name}"
                )
            for relative in stage_artifacts:
                if relative not in artifacts:
                    raise WorkspaceIdentityError(
                        f"manifest stage references an untracked artifact: {stage_name}"
                    )

    for relative, record in artifacts.items():
        if not isinstance(relative, str) or not relative or not isinstance(record, dict):
            raise WorkspaceIdentityError("manifest artifact entry is invalid")
        try:
            _validate_internal_value(relative)
        except WorkspacePathError as exc:
            raise WorkspaceIdentityError("manifest artifact path is invalid") from exc
        path = _resolve_path(workspace / relative, "manifest artifact")
        if not _is_relative_to(path, workspace) or path == workspace:
            raise WorkspaceIdentityError("manifest artifact escapes the workspace")
        if record.get("path") != relative:
            raise WorkspaceIdentityError("manifest artifact path does not match its key")
        if not _is_sha256(record.get("sha256")):
            raise WorkspaceIdentityError("manifest artifact hash is invalid")
        size = record.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise WorkspaceIdentityError("manifest artifact size is invalid")
        if not _valid_run_id(record.get("run_id")):
            raise WorkspaceIdentityError("manifest artifact run_id is invalid")
        if not isinstance(record.get("stage"), str) or not record.get("stage"):
            raise WorkspaceIdentityError("manifest artifact stage is invalid")
        parent_path = record.get("parent_path")
        if parent_path is not None and (not isinstance(parent_path, str) or not parent_path):
            raise WorkspaceIdentityError("manifest artifact parent path is invalid")
        for field in ("parent_sha256", "config_sha256", "decision_sha256"):
            value = record.get(field)
            if value is not None and not _is_sha256(value):
                raise WorkspaceIdentityError(f"manifest artifact {field} is invalid")

    for stage_name, stage_data in stages.items():
        if stage_data.get("status") in INACTIVE_STAGE_STATUSES:
            continue
        for relative in stage_data.get("artifacts", []):
            record = artifacts[relative]
            if record.get("stage") != stage_name or record.get("run_id") != stage_data.get("run_id"):
                raise WorkspaceIdentityError(
                    f"active stage artifact ownership does not match: {stage_name}/{relative}"
                )
            artifact_path = _resolve_path(workspace / relative, "active stage artifact")
            if not artifact_path.is_file():
                raise WorkspaceIdentityError(
                    f"active stage artifact is missing: {stage_name}/{relative}"
                )
            if artifact_path.stat().st_size != record.get("size_bytes"):
                raise WorkspaceIdentityError(
                    f"active stage artifact size does not match: {stage_name}/{relative}"
                )
            if sha256_file(artifact_path) != record.get("sha256"):
                raise WorkspaceIdentityError(
                    f"active stage artifact content does not match: {stage_name}/{relative}"
                )

    v0_relative = "versions/v0_original.txt"
    if v0_relative not in artifacts:
        raise WorkspaceIdentityError("manifest v2 does not track the immutable v0 snapshot")
    v0_identity = manifest.get("v0")
    v0_record = artifacts[v0_relative]
    v0_path = _resolve_path(workspace / v0_relative, "manifest v0 artifact")
    if (
        not isinstance(v0_identity, dict)
        or v0_identity.get("path") != v0_relative
        or v0_record.get("sha256") != v0_identity.get("sha256")
        or v0_record.get("size_bytes") != v0_identity.get("size_bytes")
        or v0_record.get("stage") != "source_snapshot"
        or v0_record.get("parent_path") is not None
        or v0_record.get("parent_sha256") is not None
    ):
        raise WorkspaceIdentityError(
            "manifest v0 artifact ledger does not match the immutable snapshot identity"
        )
    if (
        not v0_path.is_file()
        or v0_path.stat().st_size != v0_record.get("size_bytes")
        or sha256_file(v0_path) != v0_record.get("sha256")
    ):
        raise WorkspaceIdentityError(
            "manifest v0 artifact content does not match the immutable snapshot identity"
        )
    if current_head not in artifacts:
        raise WorkspaceIdentityError("manifest current_head is not a committed artifact")

    seen: set[str] = set()
    cursor = current_head
    while True:
        if cursor in seen:
            raise WorkspaceIdentityError("manifest current_head lineage contains a cycle")
        seen.add(cursor)
        record = artifacts.get(cursor)
        if not isinstance(record, dict):
            raise WorkspaceIdentityError("manifest current_head lineage is incomplete")
        lineage_path = _resolve_path(workspace / cursor, "manifest lineage artifact")
        if not lineage_path.is_file():
            raise WorkspaceIdentityError("manifest current_head lineage artifact is missing")
        if lineage_path.stat().st_size != record.get("size_bytes"):
            raise WorkspaceIdentityError(
                "manifest current_head lineage artifact size does not match"
            )
        if sha256_file(lineage_path) != record.get("sha256"):
            raise WorkspaceIdentityError(
                "manifest current_head lineage artifact content does not match"
            )
        parent_path = record.get("parent_path")
        parent_sha256 = record.get("parent_sha256")
        if parent_path is None:
            if cursor != "versions/v0_original.txt" or parent_sha256 is not None:
                raise WorkspaceIdentityError("manifest current_head lineage has no trusted root")
            break
        parent = artifacts.get(parent_path)
        if not isinstance(parent, dict) or parent.get("sha256") != parent_sha256:
            raise WorkspaceIdentityError("manifest current_head parent lineage is inconsistent")
        cursor = parent_path

    current_path = _resolve_path(workspace / current_head, "manifest current_head")
    current_record = artifacts[current_head]
    if not current_path.is_file():
        raise WorkspaceIdentityError("manifest current_head artifact is missing")
    if current_path.stat().st_size != current_record.get("size_bytes"):
        raise WorkspaceIdentityError("manifest current_head size does not match")
    if sha256_file(current_path) != current_record.get("sha256"):
        raise WorkspaceIdentityError("manifest current_head content does not match")


def _is_empty_workspace_shell(workspace: Path) -> bool:
    if not workspace.exists():
        return True
    for entry in workspace.iterdir():
        if entry.name not in WORKSPACE_DIRS or not entry.is_dir():
            return False
        if any(entry.iterdir()):
            return False
    return True


def _identity_path_matches(value: Any, expected: Path) -> bool:
    return isinstance(value, str) and os.path.normcase(
        _without_windows_extended_prefix(value)
    ) == os.path.normcase(_without_windows_extended_prefix(str(expected)))


def _validate_bound_snapshot_identity(workspace: Path) -> None:
    manifest_file = workspace / "manifest.json"
    v0 = workspace / "versions" / "v0_original.txt"
    if not manifest_file.exists():
        raise WorkspaceIdentityError("workspace manifest is missing; create a new workspace")
    if not manifest_file.is_file():
        raise WorkspaceIdentityError("workspace manifest is not a file")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceIdentityError("workspace manifest cannot be read") from exc
    if not isinstance(manifest, dict):
        raise WorkspaceIdentityError("workspace manifest must be a JSON object")
    if not v0.exists():
        raise WorkspaceIdentityError("workspace manifest exists without its bound v0 snapshot")

    source_identity = manifest.get("source")
    v0_identity = manifest.get("v0")
    if not isinstance(source_identity, dict) or not isinstance(v0_identity, dict):
        raise WorkspaceIdentityError(
            "workspace manifest lacks a verifiable source or v0 identity; create a new workspace"
        )
    if not _identity_path_matches(manifest.get("workspace"), workspace):
        raise WorkspaceIdentityError("workspace path does not match its manifest identity")
    if v0_identity.get("path") != "versions/v0_original.txt":
        raise WorkspaceIdentityError("v0 path does not match the workspace identity")
    if not v0.is_file():
        raise WorkspaceIdentityError("the bound v0 snapshot is missing or is not a file")

    source_path_value = source_identity.get("path")
    source_hash = source_identity.get("sha256")
    v0_hash = v0_identity.get("sha256")
    if not isinstance(source_path_value, str) or not source_path_value:
        raise WorkspaceIdentityError("source path is missing from the workspace identity")
    try:
        recorded_source = Path(source_path_value)
        if not recorded_source.is_absolute():
            raise WorkspaceIdentityError("source path in the workspace identity is not absolute")
        canonical_source = _resolve_path(recorded_source, "workspace identity source")
    except (OSError, RuntimeError, WorkspacePathError) as exc:
        raise WorkspaceIdentityError("source path in the workspace identity is invalid") from exc
    if not _identity_path_matches(source_path_value, canonical_source):
        raise WorkspaceIdentityError("source path in the workspace identity is not canonical")
    source_name = source_identity.get("name")
    if not isinstance(source_name, str) or os.path.normcase(source_name) != os.path.normcase(
        canonical_source.name
    ):
        raise WorkspaceIdentityError("source name does not match the workspace identity")
    if not isinstance(source_hash, str) or source_hash != v0_hash:
        raise WorkspaceIdentityError("source and v0 hashes do not match in the workspace identity")
    if source_identity.get("size_bytes") != v0_identity.get("size_bytes"):
        raise WorkspaceIdentityError("source and v0 sizes do not match in the workspace identity")
    if canonical_source.exists() and _same_file_or_path(canonical_source, v0):
        raise WorkspaceIdentityError("source file aliases the bound v0 snapshot")

    if v0_hash != sha256_file(v0):
        raise WorkspaceIdentityError("v0 content does not match the workspace identity")
    _validate_manifest_v2(workspace, manifest)


def _validate_workspace_identity(
    source: Path,
    workspace: Path,
    v0: Path,
    manifest: dict[str, Any],
) -> None:
    source_identity = manifest.get("source")
    v0_identity = manifest.get("v0")
    if not isinstance(source_identity, dict) or not isinstance(v0_identity, dict):
        raise WorkspaceIdentityError(
            "workspace manifest lacks a verifiable source or v0 identity; create a new workspace"
        )
    if not _identity_path_matches(source_identity.get("path"), source):
        raise WorkspaceIdentityError("source path does not match the workspace identity")
    if not _identity_path_matches(manifest.get("workspace"), workspace):
        raise WorkspaceIdentityError("workspace path does not match its manifest identity")
    if v0_identity.get("path") != "versions/v0_original.txt":
        raise WorkspaceIdentityError("v0 path does not match the workspace identity")
    if not v0.is_file():
        raise WorkspaceIdentityError("the bound v0 snapshot is missing or is not a file")

    source_hash = sha256_file(source)
    v0_hash = sha256_file(v0)
    if source_identity.get("sha256") != source_hash:
        raise WorkspaceIdentityError("source content does not match the workspace identity")
    if v0_identity.get("sha256") != v0_hash:
        raise WorkspaceIdentityError("v0 content does not match the workspace identity")
    if source_hash != v0_hash:
        raise WorkspaceIdentityError("source and v0 identities do not match")
    source_name = source_identity.get("name")
    if not isinstance(source_name, str) or os.path.normcase(source_name) != os.path.normcase(
        source.name
    ):
        raise WorkspaceIdentityError("source name does not match the workspace identity")
    if source_identity.get("size_bytes") != source.stat().st_size:
        raise WorkspaceIdentityError("source size does not match the workspace identity")
    if v0_identity.get("size_bytes") != v0.stat().st_size:
        raise WorkspaceIdentityError("v0 size does not match the workspace identity")
    _validate_manifest_v2(workspace, manifest)


_SNAPSHOT_INIT_JOURNAL = ".snapshot-init.json"
_SNAPSHOT_INIT_MARKER = ".snapshot-init.marker"
_SNAPSHOT_ATOMIC_TEMP_TARGETS = (_SNAPSHOT_INIT_JOURNAL, "manifest.json")


def _snapshot_atomic_temp_target(name: str) -> str | None:
    for target in _SNAPSHOT_ATOMIC_TEMP_TARGETS:
        prefix = f".{target}."
        if (
            name.startswith(prefix)
            and name.endswith(".tmp")
            and re.fullmatch(
                r"[A-Za-z0-9_-]{8}",
                name[len(prefix) : -len(".tmp")],
            )
        ):
            return target
    return None


def _validate_snapshot_init_marker(workspace: Path) -> Path:
    marker = workspace / _SNAPSHOT_INIT_MARKER
    if (
        not marker.is_file()
        or _resolve_path(marker, "snapshot initialization marker") != marker
        or marker.stat().st_size != 0
    ):
        raise WorkspaceIdentityError("snapshot initialization marker is invalid")
    return marker


def _create_snapshot_init_marker(workspace: Path) -> Path:
    marker = workspace / _SNAPSHOT_INIT_MARKER
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(marker, flags, 0o600)
    except FileExistsError:
        return _validate_snapshot_init_marker(workspace)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_parent_directory(marker)
    return marker


def _remove_snapshot_atomic_temps(workspace: Path) -> None:
    for entry in workspace.iterdir():
        if _snapshot_atomic_temp_target(entry.name) is None:
            continue
        if (
            not entry.is_file()
            or _resolve_path(entry, "snapshot initialization temporary file") != entry
        ):
            raise WorkspaceIdentityError(
                "snapshot initialization temporary file is invalid"
            )
        entry.unlink()
        _fsync_parent_directory(entry)


def _snapshot_init_relative_paths(run_id: str) -> tuple[str, str]:
    return (
        f"versions/.v0_original.{run_id}.copying",
        f"versions/.v0_original.{run_id}.staged",
    )


def _load_snapshot_init_journal(
    source: Path,
    workspace: Path,
) -> dict[str, Any] | None:
    marker_path = workspace / _SNAPSHOT_INIT_MARKER
    journal_path = workspace / _SNAPSHOT_INIT_JOURNAL
    marker_exists = marker_path.exists()
    journal_exists = journal_path.exists()
    if not marker_exists and not journal_exists:
        return None
    if not marker_exists:
        raise WorkspaceIdentityError(
            "snapshot initialization journal has no durable marker"
        )
    _validate_snapshot_init_marker(workspace)
    if not journal_exists:
        if not (workspace / "manifest.json").exists():
            allowed_root = {*WORKSPACE_DIRS, _SNAPSHOT_INIT_MARKER}
            for entry in workspace.iterdir():
                temp_target = _snapshot_atomic_temp_target(entry.name)
                if entry.name not in allowed_root and temp_target != _SNAPSHOT_INIT_JOURNAL:
                    raise WorkspaceIdentityError(
                        "snapshot initialization workspace contains an unknown entry"
                    )
                if entry.name in WORKSPACE_DIRS:
                    if not entry.is_dir() or any(entry.iterdir()):
                        raise WorkspaceIdentityError(
                            "snapshot initialization workspace contains unrelated artifacts"
                        )
                elif temp_target is not None and (
                    not entry.is_file()
                    or _resolve_path(
                        entry,
                        "snapshot initialization temporary file",
                    )
                    != entry
                ):
                    raise WorkspaceIdentityError(
                        "snapshot initialization temporary file is invalid"
                    )
            _remove_snapshot_atomic_temps(workspace)
        return None
    if (
        not journal_path.is_file()
        or _resolve_path(journal_path, "snapshot initialization journal") != journal_path
    ):
        raise WorkspaceIdentityError("snapshot initialization journal is invalid")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceIdentityError("snapshot initialization journal cannot be read") from exc
    if not isinstance(journal, dict):
        raise WorkspaceIdentityError("snapshot initialization journal must be a JSON object")
    run_id = journal.get("run_id")
    source_identity = journal.get("source")
    copying_relative, staged_relative = (
        _snapshot_init_relative_paths(run_id) if _valid_run_id(run_id) else ("", "")
    )
    if (
        journal.get("schema_version") != 1
        or not _valid_run_id(run_id)
        or not isinstance(source_identity, dict)
        or not _identity_path_matches(journal.get("workspace"), workspace)
        or not _identity_path_matches(source_identity.get("path"), source)
        or source_identity.get("name") != source.name
        or not _is_sha256(source_identity.get("sha256"))
        or not isinstance(source_identity.get("size_bytes"), int)
        or isinstance(source_identity.get("size_bytes"), bool)
        or source_identity.get("size_bytes") < 0
        or journal.get("v0") != "versions/v0_original.txt"
        or journal.get("copying") != copying_relative
        or journal.get("staged") != staged_relative
        or not isinstance(journal.get("created_at"), str)
    ):
        raise WorkspaceIdentityError("snapshot initialization journal identity is invalid")
    if (
        source.stat().st_size != source_identity["size_bytes"]
        or sha256_file(source) != source_identity["sha256"]
    ):
        raise WorkspaceIdentityError(
            "source content does not match the interrupted snapshot initialization"
        )

    if not (workspace / "manifest.json").exists():
        allowed_root = {
            *WORKSPACE_DIRS,
            _SNAPSHOT_INIT_JOURNAL,
            _SNAPSHOT_INIT_MARKER,
        }
        allowed_versions = {
            "v0_original.txt",
            Path(copying_relative).name,
            Path(staged_relative).name,
        }
        for entry in workspace.iterdir():
            temp_target = _snapshot_atomic_temp_target(entry.name)
            if entry.name not in allowed_root and temp_target is None:
                raise WorkspaceIdentityError(
                    "snapshot initialization workspace contains an unknown entry"
                )
            if entry.name in WORKSPACE_DIRS:
                if not entry.is_dir():
                    raise WorkspaceIdentityError(
                        "snapshot initialization workspace directory is invalid"
                    )
                children = list(entry.iterdir())
                if entry.name != "versions" and children:
                    raise WorkspaceIdentityError(
                        "snapshot initialization workspace contains unrelated artifacts"
                    )
                if entry.name == "versions" and any(
                    child.name not in allowed_versions or not child.is_file()
                    for child in children
                ):
                    raise WorkspaceIdentityError(
                        "snapshot initialization workspace contains an unknown version artifact"
                    )
            elif temp_target is not None and (
                not entry.is_file()
                or _resolve_path(
                    entry,
                    "snapshot initialization temporary file",
                )
                != entry
            ):
                raise WorkspaceIdentityError(
                    "snapshot initialization temporary file is invalid"
                )
    return journal


def _snapshot_manifest(
    source: Path,
    workspace: Path,
    v0: Path,
    journal: Mapping[str, Any],
) -> dict[str, Any]:
    source_identity = journal["source"]
    run_id = journal["run_id"]
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_at": journal["created_at"],
        "updated_at": now_iso(),
        "source": {
            "path": str(source),
            "name": source.name,
            "size_bytes": source_identity["size_bytes"],
            "sha256": source_identity["sha256"],
        },
        "v0": {
            "path": "versions/v0_original.txt",
            "size_bytes": source_identity["size_bytes"],
            "sha256": source_identity["sha256"],
        },
        "workspace": str(workspace),
        "current_head": "versions/v0_original.txt",
        "artifacts": {
            "versions/v0_original.txt": _artifact_record(
                v0,
                "versions/v0_original.txt",
                run_id=run_id,
                stage="source_snapshot",
            )
        },
        "stages": default_stages(),
    }


def _copy_snapshot_source(source: Path, target: Path) -> None:
    with source.open("rb") as input_file, target.open("wb") as output_file:
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        output_file.flush()
        os.fsync(output_file.fileno())
    _fsync_parent_directory(target)


def _finish_snapshot_initialization(
    source: Path,
    workspace: Path,
    journal: Mapping[str, Any],
) -> dict[str, Any]:
    source_identity = journal["source"]
    expected_hash = source_identity["sha256"]
    expected_size = source_identity["size_bytes"]
    copying = workspace / journal["copying"]
    staged = workspace / journal["staged"]
    v0 = workspace / journal["v0"]
    manifest_file = workspace / "manifest.json"

    if manifest_file.exists():
        if not v0.is_file():
            raise WorkspaceIdentityError(
                "snapshot manifest exists without its interrupted v0 snapshot"
            )
        manifest = load_manifest(workspace)
        _validate_workspace_identity(source, workspace, v0, manifest)
        _remove_snapshot_atomic_temps(workspace)
    else:
        if v0.exists():
            if not v0.is_file() or staged.exists() or copying.exists():
                raise WorkspaceIdentityError(
                    "interrupted snapshot publication state is ambiguous"
                )
        else:
            if staged.exists() and copying.exists():
                raise WorkspaceIdentityError(
                    "interrupted snapshot staging state is ambiguous"
                )
            if staged.exists():
                if (
                    not staged.is_file()
                    or staged.stat().st_size != expected_size
                    or sha256_file(staged) != expected_hash
                ):
                    raise WorkspaceIdentityError(
                        "interrupted snapshot staged content is invalid"
                    )
            else:
                if copying.exists() and not copying.is_file():
                    raise WorkspaceIdentityError(
                        "interrupted snapshot copy state is invalid"
                    )
                _copy_snapshot_source(source, copying)
                if (
                    source.stat().st_size != expected_size
                    or sha256_file(source) != expected_hash
                    or copying.stat().st_size != expected_size
                    or sha256_file(copying) != expected_hash
                ):
                    raise WorkspaceIdentityError(
                        "source changed while the v0 snapshot was being created"
                    )
                os.replace(copying, staged)
                _fsync_parent_directory(staged)
            if sha256_file(source) != expected_hash:
                raise WorkspaceIdentityError(
                    "source changed while the v0 snapshot was being published"
                )
            os.replace(staged, v0)
            _fsync_parent_directory(v0)

        if (
            v0.stat().st_size != expected_size
            or sha256_file(v0) != expected_hash
            or sha256_file(source) != expected_hash
        ):
            raise WorkspaceIdentityError("the v0 snapshot does not match its source identity")
        _remove_snapshot_atomic_temps(workspace)
        try:
            v0.chmod(stat.S_IREAD)
        except OSError:
            pass
        manifest = _snapshot_manifest(source, workspace, v0, journal)
        save_manifest(workspace, manifest)

    journal_path = workspace / _SNAPSHOT_INIT_JOURNAL
    try:
        journal_path.unlink()
        _fsync_parent_directory(journal_path)
    except OSError:
        pass
    marker_path = workspace / _SNAPSHOT_INIT_MARKER
    try:
        marker_path.unlink()
        _fsync_parent_directory(marker_path)
    except OSError:
        pass
    return manifest


def init_workspace_from_source(source: Path, workspace: Path) -> dict[str, Any]:
    source = _resolve_path(source, "source file")
    if not source.exists():
        raise FileNotFoundError(f"source file not found: {source}")
    if not source.is_file():
        raise ValueError(f"source path is not a file: {source}")

    workspace = _resolve_path(Path(workspace), "workspace")
    prospective_v0 = workspace / "versions" / "v0_original.txt"
    prospective_manifest = workspace / "manifest.json"
    if _same_file_or_path(source, prospective_v0) or _same_file_or_path(source, prospective_manifest):
        raise WorkspacePathError("source file conflicts with a protected workspace path")
    if workspace.exists():
        workspace = validate_workspace(workspace)

    with _PathTransactionLock("workspace", workspace, allow_children=True):
        manifest_file = workspace / "manifest.json"
        v0 = workspace / "versions" / "v0_original.txt"
        marker_path = workspace / _SNAPSHOT_INIT_MARKER
        journal = (
            _load_snapshot_init_journal(source, workspace)
            if workspace.exists()
            else None
        )
        if journal is not None:
            return _finish_snapshot_initialization(source, workspace, journal)

        manifest_exists = manifest_file.exists()
        v0_exists = v0.exists()
        if manifest_exists or v0_exists:
            if not manifest_exists or not v0_exists:
                raise WorkspaceIdentityError(
                    "workspace identity is incomplete; create a new workspace instead of reusing it"
                )
            if not manifest_file.is_file():
                raise WorkspaceIdentityError("workspace manifest is not a file")
            try:
                manifest = load_manifest(workspace)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise WorkspaceIdentityError("workspace manifest cannot be read") from exc
            if not isinstance(manifest, dict):
                raise WorkspaceIdentityError("workspace manifest must be a JSON object")
            _validate_workspace_identity(source, workspace, v0, manifest)
            if marker_path.exists():
                _validate_snapshot_init_marker(workspace)
                _remove_snapshot_atomic_temps(workspace)
                try:
                    marker_path.unlink()
                    _fsync_parent_directory(marker_path)
                except OSError:
                    pass
            ensure_workspace(workspace)
            return manifest

        marker_pending = marker_path.exists()
        if not marker_pending and not _is_empty_workspace_shell(workspace):
            raise WorkspaceIdentityError(
                "non-empty workspace has no verifiable identity; create a new workspace"
            )

        workspace = ensure_workspace(workspace)
        if marker_pending:
            _validate_snapshot_init_marker(workspace)
        else:
            _create_snapshot_init_marker(workspace)
        source_hash = sha256_file(source)
        source_size = source.stat().st_size
        if sha256_file(source) != source_hash or source.stat().st_size != source_size:
            raise WorkspaceIdentityError(
                "source changed before snapshot initialization could be recorded"
            )
        run_id = uuid.uuid4().hex
        copying_relative, staged_relative = _snapshot_init_relative_paths(run_id)
        journal = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": now_iso(),
            "workspace": str(workspace),
            "source": {
                "path": str(source),
                "name": source.name,
                "size_bytes": source_size,
                "sha256": source_hash,
            },
            "v0": "versions/v0_original.txt",
            "copying": copying_relative,
            "staged": staged_relative,
        }
        write_json(workspace / _SNAPSHOT_INIT_JOURNAL, journal)
        return _finish_snapshot_initialization(source, workspace, journal)


def update_stage(
    workspace: Path,
    stage: str,
    status: str,
    **extra: Any,
) -> dict[str, Any]:
    return update_stages(workspace, {stage: (status, extra)})


def update_stages(
    workspace: Path,
    updates: Mapping[str, tuple[str, Mapping[str, Any]]],
    *,
    _published_artifacts: bool = False,
) -> dict[str, Any]:
    if not updates:
        raise ValueError("at least one stage update is required")
    manifest = load_manifest(workspace)
    workspace = validate_workspace(workspace)
    if not _published_artifacts:
        _validate_manifest_v2(workspace, manifest)
    manifest.setdefault("stages", default_stages())
    explicit_stages = set(updates)
    invalidation_events: list[tuple[str, str | None, list[str]]] = []
    for stage, (status, extra) in updates.items():
        if not _valid_stage_name(stage):
            raise ValueError(f"unsupported stage name: {stage}")
        if status not in VALID_STAGE_STATUSES:
            raise ValueError(f"unsupported stage status: {status}")
        stage_data = manifest["stages"].setdefault(stage, {})
        previous = stage_data.get("status", "pending")
        if previous not in VALID_STAGE_STATUSES:
            raise ValueError(f"existing stage status is invalid: {previous}")
        if status not in LEGAL_STAGE_TRANSITIONS[previous]:
            raise ValueError(f"illegal stage status transition: {previous} -> {status}")

        clean_extra = dict(extra)
        artifact_records = clean_extra.pop("_artifact_records", None)
        deleted_artifacts = clean_extra.pop("_deleted_artifacts", None)
        current_head = clean_extra.pop("_current_head", None)
        if artifact_records is not None:
            if not isinstance(artifact_records, dict):
                raise ValueError("transaction artifact records are invalid")
            replaced_paths = set(artifact_records)
            changed_paths = sorted(
                relative
                for relative, record in artifact_records.items()
                if not isinstance(manifest["artifacts"].get(relative), dict)
                or manifest["artifacts"][relative].get("sha256") != record.get("sha256")
            )
            if status != "pending" and changed_paths:
                run_id = clean_extra.get("run_id")
                invalidation_events.append(
                    (stage, run_id if isinstance(run_id, str) else None, changed_paths)
                )
            for other_stage, other_data in manifest["stages"].items():
                if other_stage == stage or not isinstance(other_data, dict):
                    continue
                owned = other_data.get("artifacts", [])
                if isinstance(owned, list) and replaced_paths.intersection(owned):
                    other_data["status"] = "pending"
                    other_data["invalidated_by"] = stage
                    if isinstance(clean_extra.get("run_id"), str):
                        other_data["invalidated_by_run_id"] = clean_extra["run_id"]
                    other_data["invalidated_artifacts"] = sorted(replaced_paths.intersection(owned))
                    other_data["updated_at"] = now_iso()
                    other_data.pop("artifacts", None)
                    other_data.pop("attestation", None)
                    other_data.pop("active_run_id", None)
                    other_data.pop("run_id", None)
            manifest["artifacts"].update(artifact_records)
        if deleted_artifacts is not None:
            if not isinstance(deleted_artifacts, list) or not all(
                isinstance(value, str) for value in deleted_artifacts
            ):
                raise ValueError("deleted artifact list is invalid")
            for relative in deleted_artifacts:
                if relative == manifest.get("current_head"):
                    raise ValueError("cannot delete the current_head artifact")
                manifest["artifacts"].pop(relative, None)
        if current_head is not None:
            if not isinstance(current_head, str) or current_head not in manifest["artifacts"]:
                raise ValueError("transaction current_head is not a committed artifact")
            manifest["current_head"] = current_head

        if status in INACTIVE_STAGE_STATUSES:
            for key in ("artifacts", "attestation", "active_run_id"):
                stage_data.pop(key, None)
        stage_data.update(clean_extra)
        stage_data["status"] = status
        stage_data["updated_at"] = now_iso()

    for trigger, run_id, changed_paths in invalidation_events:
        for target in stage_invalidation_targets(trigger):
            if target in explicit_stages:
                continue
            stage_data = manifest["stages"].get(target)
            if not isinstance(stage_data, dict):
                continue
            stage_data["status"] = "pending"
            stage_data["invalidated_by"] = trigger
            if run_id is not None:
                stage_data["invalidated_by_run_id"] = run_id
            stage_data["invalidated_artifacts"] = changed_paths
            stage_data["updated_at"] = now_iso()
            for key in ("artifacts", "attestation", "active_run_id", "run_id"):
                stage_data.pop(key, None)
    _validate_manifest_v2(validate_workspace(workspace), manifest)
    save_manifest(workspace, manifest)
    return manifest


@dataclass
class _TransactionEntry:
    target: Path
    staged: Path | None = None
    delete: bool = False
    existed: bool = False
    backed_up: bool = False
    published: bool = False


def _workspace_transaction_target(workspace: Path, target: Path) -> Path:
    target = _resolve_path(Path(target), "transaction target")
    if not _is_relative_to(target, workspace) or target == workspace:
        raise WorkspacePathError("transaction target escapes the workspace")
    relative = target.relative_to(workspace)
    if relative.parts[0] == ".runs":
        raise WorkspacePathError("transaction cannot stage its recovery namespace")
    if any(_same_file_or_path(target, path) for path in workspace_protected_paths(workspace)):
        raise WorkspacePathError("transaction cannot stage a protected workspace file")
    return target


class WorkspaceTransaction:
    """Stage a workspace run and publish its artifacts before one final manifest update."""

    def __init__(self, workspace: Path, *, run_id: str | None = None) -> None:
        self.workspace = validate_workspace(workspace)
        self.run_id = run_id or uuid.uuid4().hex
        if not _valid_run_id(self.run_id):
            raise WorkspaceTransactionError("transaction run_id must be 32 lowercase hex characters")
        self.root = self.workspace / ".runs" / self.run_id
        self.files_root = self.root / "files"
        self.backups_root = self.root / "backups"
        self._entries: dict[Path, _TransactionEntry] = {}
        self._directories: set[Path] = set()
        self._atomic_directories: set[Path] = set()
        self._committed = False
        self._deferred = False
        self._finalized = False
        self._retain_for_recovery = False
        self._changed: list[_TransactionEntry] = []
        self._created_directories: list[Path] = []
        self._group_commits: tuple[tuple[Path, str, str], ...] = ()
        self._lock = _PathTransactionLock("workspace", self.workspace)

    def __enter__(self) -> "WorkspaceTransaction":
        self._lock.acquire()
        try:
            recover_workspace_transactions(self.workspace, _lock_held=True)
            self.files_root.mkdir(parents=True, exist_ok=False)
            write_utf8(self.root / "run.marker", self.run_id)
        except Exception:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        try:
            if self._committed and self._deferred and not self._finalized:
                if _transaction_group_is_committed(self._journal_group_state()):
                    self._finalized = True
                    _cleanup_transaction_root(self.root)
                else:
                    self.rollback()
            if not self._committed and not self._retain_for_recovery and self.root.exists():
                shutil.rmtree(self.root)
            runs_dir = self.root.parent
            if runs_dir.exists() and not any(runs_dir.iterdir()):
                runs_dir.rmdir()
        finally:
            self._lock.release()

    def _target_path(self, target: Path) -> Path:
        return _workspace_transaction_target(self.workspace, target)

    def _entry_for(self, target: Path) -> _TransactionEntry:
        target = self._target_path(target)
        entry = self._entries.get(target)
        if entry is None:
            entry = _TransactionEntry(target=target)
            self._entries[target] = entry
        return entry

    def stage_path(self, target: Path, *, copy_existing: bool = False) -> Path:
        entry = self._entry_for(target)
        if entry.target.exists() and not entry.target.is_file():
            raise WorkspaceTransactionError("transaction file target is not a file")
        if entry.delete:
            raise WorkspaceTransactionError("a deleted target cannot also be staged for writing")
        if entry.staged is None:
            entry.staged = self.files_root / entry.target.relative_to(self.workspace)
            entry.staged.parent.mkdir(parents=True, exist_ok=True)
            if copy_existing and entry.target.exists():
                shutil.copy2(entry.target, entry.staged)
        return entry.staged

    def stage_delete(self, target: Path) -> None:
        entry = self._entry_for(target)
        if entry.target.exists() and not entry.target.is_file():
            raise WorkspaceTransactionError("transaction delete target is not a file")
        if entry.staged is not None:
            raise WorkspaceTransactionError("a staged target cannot also be deleted")
        entry.delete = True

    def discard_unwritten_stage(self, target: Path) -> None:
        target = self._target_path(target)
        entry = self._entries.get(target)
        if entry is None or entry.staged is None:
            return
        if entry.staged.exists():
            return
        del self._entries[target]

    def stage_directory(self, directory: Path, *, require_new: bool = False) -> None:
        directory = self._target_path(directory)
        if directory in self._entries:
            raise WorkspaceTransactionError("a file target cannot also be staged as a directory")
        if directory.exists() and not directory.is_dir():
            raise WorkspaceTransactionError("transaction directory target is not a directory")
        if require_new and directory.exists():
            raise WorkspaceTransactionError("transaction directory target already exists")
        if require_new:
            if any(
                _is_relative_to(directory, existing) or _is_relative_to(existing, directory)
                for existing in self._atomic_directories
            ):
                raise WorkspaceTransactionError("atomic transaction directories cannot overlap")
            self._atomic_directories.add(directory)
        self._directories.add(directory)
        parent = directory.parent
        while parent != self.workspace and not parent.exists():
            self._directories.add(parent)
            parent = parent.parent

    def _backup_path(self, entry: _TransactionEntry) -> Path:
        return self.backups_root / entry.target.relative_to(self.workspace)

    def _rollback(self, changed: list[_TransactionEntry]) -> None:
        atomic_entries = {
            entry.target
            for entry in changed
            if any(_is_relative_to(entry.target, directory) for directory in self._atomic_directories)
        }
        for directory in sorted(self._atomic_directories, key=str):
            if not directory.exists():
                continue
            hidden = self.root / "atomic-rollback" / directory.relative_to(self.workspace)
            hidden.parent.mkdir(parents=True, exist_ok=True)
            os.replace(directory, hidden)
        for entry in reversed(changed):
            if entry.target in atomic_entries:
                continue
            backup = self._backup_path(entry)
            if entry.backed_up:
                if not backup.exists():
                    raise WorkspaceTransactionError("transaction rollback backup is missing")
                _restore_backup_file(backup, entry.target)
            elif entry.published and entry.target.exists():
                entry.target.unlink()

    def _write_journal(
        self,
        updates: Mapping[str, tuple[str, Mapping[str, Any]]],
        *,
        deferred: bool,
        group_commits: Iterable[tuple[Path, str, str]],
    ) -> None:
        write_json(
            self.root / "journal.json",
            {
                "schema_version": 2,
                "run_id": self.run_id,
                "deferred": deferred,
                "manifest_backup_sha256": (
                    sha256_file(self.root / "manifest.backup.json") if deferred else None
                ),
                "updates": [
                    {"stage": stage, "status": status}
                    for stage, (status, _) in updates.items()
                ],
                "entries": [
                    {
                        "target": entry.target.relative_to(self.workspace).as_posix(),
                        "delete": entry.delete,
                        "existed": entry.existed,
                        "backup_sha256": (
                            sha256_file(self._backup_path(entry))
                            if entry.backed_up
                            else None
                        ),
                        "backup_size_bytes": (
                            self._backup_path(entry).stat().st_size
                            if entry.backed_up
                            else None
                        ),
                        "staged_sha256": (
                            sha256_file(entry.staged)
                            if entry.staged is not None and entry.staged.is_file()
                            else None
                        ),
                        "staged_size_bytes": (
                            entry.staged.stat().st_size
                            if entry.staged is not None and entry.staged.is_file()
                            else None
                        ),
                    }
                    for entry in self._entries.values()
                ],
                "directories": [
                    {
                        "path": directory.relative_to(self.workspace).as_posix(),
                        "created": not directory.exists(),
                    }
                    for directory in sorted(self._directories, key=str)
                ],
                "atomic_directories": [
                    directory.relative_to(self.workspace).as_posix()
                    for directory in sorted(self._atomic_directories, key=str)
                ],
                "group_commits": [
                    {
                        "workspace": str(validate_workspace(workspace)),
                        "stage": stage,
                        "status": status,
                    }
                    for workspace, stage, status in group_commits
                ],
            },
        )

    def _verify_prebackup_target_identities(
        self,
        identities: Mapping[Path, tuple[bool, str | None, int | None]],
    ) -> None:
        for entry in self._entries.values():
            existed, expected_sha256, expected_size = identities[entry.target]
            if not existed:
                if entry.target.exists():
                    raise WorkspaceTransactionError(
                        "transaction target changed after backup"
                    )
                continue
            backup = self._backup_path(entry)
            if (
                not entry.target.is_file()
                or not backup.is_file()
                or expected_sha256 is None
                or expected_size is None
                or entry.target.stat().st_size != expected_size
                or backup.stat().st_size != expected_size
                or sha256_file(entry.target) != expected_sha256
                or sha256_file(backup) != expected_sha256
            ):
                raise WorkspaceTransactionError(
                    "transaction target changed after backup"
                )

    def _journal_group_state(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "group_commits": [
                {"workspace": str(workspace), "stage": stage, "status": status}
                for workspace, stage, status in self._group_commits
            ],
        }

    def _attach_lineage_metadata(
        self,
        updates: Mapping[str, tuple[str, Mapping[str, Any]]],
    ) -> dict[str, tuple[str, dict[str, Any]]]:
        committed = {
            stage: (status, {**extra, "run_id": self.run_id})
            for stage, (status, extra) in updates.items()
        }
        active = [
            (stage, status, extra)
            for stage, (status, extra) in committed.items()
            if status not in INACTIVE_STAGE_STATUSES
        ]
        owner_stage, _, owner_extra = active[0] if active else next(
            (stage, status, extra) for stage, (status, extra) in committed.items()
        )
        manifest = load_manifest(self.workspace)
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise WorkspaceTransactionError("manifest artifact ledger is unavailable")

        input_value = owner_extra.get("input") or owner_extra.get("source")
        if isinstance(input_value, str):
            input_value = input_value.replace("\\", "/")
        parent_path = input_value if isinstance(input_value, str) and input_value in artifacts else None
        parent_record = artifacts.get(parent_path) if parent_path is not None else None
        parent_sha256 = parent_record.get("sha256") if isinstance(parent_record, dict) else None

        config_sha256 = owner_extra.get("config_sha256")
        if config_sha256 is not None and not _is_sha256(config_sha256):
            raise WorkspaceTransactionError("stage config_sha256 is invalid")
        decision_sha256 = owner_extra.get("decision_sha256")
        decisions_value = owner_extra.get("decisions") or owner_extra.get("filtered_decisions")
        if isinstance(decisions_value, str):
            decisions_value = decisions_value.replace("\\", "/")
        if decision_sha256 is None and isinstance(decisions_value, str):
            decision_path = self.workspace / decisions_value
            if decision_path.is_file():
                decision_sha256 = sha256_file(decision_path)
        if decision_sha256 is not None and not _is_sha256(decision_sha256):
            raise WorkspaceTransactionError("stage decision_sha256 is invalid")

        records: dict[str, dict[str, Any]] = {}
        deleted: list[str] = []
        artifact_paths: list[str] = []
        output_value = owner_extra.get("output")
        if isinstance(output_value, str):
            output_value = output_value.replace("\\", "/")
        current_head: str | None = None
        for entry in self._entries.values():
            relative = entry.target.relative_to(self.workspace).as_posix()
            if entry.delete:
                deleted.append(relative)
                continue
            if not entry.published:
                continue
            is_version_text = relative.startswith("versions/") and entry.target.suffix == ".txt"
            entry_parent_path = parent_path
            entry_parent_sha256 = parent_sha256
            existing_record = artifacts.get(relative)
            if is_version_text and entry_parent_path is None and isinstance(existing_record, dict):
                entry_parent_path = existing_record.get("parent_path")
                entry_parent_sha256 = existing_record.get("parent_sha256")
            if is_version_text and entry_parent_path is None:
                raise WorkspaceTransactionError(
                    f"text artifact has no committed parent lineage: {relative}"
                )
            records[relative] = _artifact_record(
                entry.target,
                relative,
                run_id=self.run_id,
                stage=owner_stage,
                parent_path=entry_parent_path if is_version_text else None,
                parent_sha256=entry_parent_sha256 if is_version_text else None,
                config_sha256=config_sha256 if is_version_text else None,
                decision_sha256=decision_sha256 if is_version_text else None,
            )
            artifact_paths.append(relative)
            if is_version_text and output_value == relative:
                current_head = relative

        owner_extra["artifacts"] = sorted(artifact_paths)
        owner_extra["_artifact_records"] = records
        owner_extra["_deleted_artifacts"] = sorted(deleted)
        if current_head is not None:
            owner_extra["_current_head"] = current_head
        return committed

    def commit(
        self,
        updates: Mapping[str, tuple[str, Mapping[str, Any]]],
        *,
        defer_cleanup: bool = False,
        group_commits: Iterable[tuple[Path, str, str]] = (),
    ) -> dict[str, Any]:
        if self._committed:
            raise WorkspaceTransactionError("transaction is already committed")
        _validate_manifest_v2(self.workspace, load_manifest(self.workspace))
        self._group_commits = tuple(group_commits)
        if not self._entries and not (defer_cleanup and self._group_commits):
            raise WorkspaceTransactionError("transaction has no staged artifacts")
        for entry in self._entries.values():
            target = _workspace_transaction_target(self.workspace, entry.target)
            if target != entry.target:
                raise WorkspaceTransactionError("transaction target changed after staging")
            if target.exists() and not target.is_file():
                raise WorkspaceTransactionError("transaction file target is not a file")
            if not entry.delete:
                if entry.staged is None:
                    raise WorkspaceTransactionError(f"staged artifact is missing: {entry.target}")
                staged = _resolve_path(entry.staged, "staged transaction artifact")
                if (
                    staged != entry.staged
                    or not _is_relative_to(staged, self.files_root)
                    or not staged.is_file()
                ):
                    raise WorkspaceTransactionError(f"staged artifact is invalid: {entry.target}")
        for directory in self._directories:
            validated = _workspace_transaction_target(self.workspace, directory)
            if validated != directory:
                raise WorkspaceTransactionError("transaction directory changed after staging")
            if directory.exists() and not directory.is_dir():
                raise WorkspaceTransactionError("transaction directory target is not a directory")

        entries_by_atomic_directory: dict[Path, list[_TransactionEntry]] = {}
        atomic_targets: set[Path] = set()
        for directory in self._atomic_directories:
            entries = [
                entry
                for entry in self._entries.values()
                if _is_relative_to(entry.target, directory)
            ]
            if not entries or any(entry.delete for entry in entries):
                raise WorkspaceTransactionError(
                    "atomic transaction directory has invalid staged artifacts"
                )
            if directory.exists():
                raise WorkspaceTransactionError("atomic transaction directory target already exists")
            raw_staged_directory = self.files_root / directory.relative_to(self.workspace)
            staged_directory = _resolve_path(
                raw_staged_directory,
                "atomic staged transaction directory",
            )
            if staged_directory != raw_staged_directory or not staged_directory.is_dir():
                raise WorkspaceTransactionError("atomic staged transaction directory is invalid")
            entries_by_atomic_directory[directory] = entries
            atomic_targets.update(entry.target for entry in entries)

        changed: list[_TransactionEntry] = []
        created_directories: list[Path] = []
        self._deferred = defer_cleanup
        target_identities: dict[Path, tuple[bool, str | None, int | None]] = {}
        for entry in self._entries.values():
            entry.existed = entry.target.exists()
            target_identities[entry.target] = (
                entry.existed,
                sha256_file(entry.target) if entry.existed else None,
                entry.target.stat().st_size if entry.existed else None,
            )
        try:
            if defer_cleanup:
                _atomic_copy_file(
                    self.workspace / "manifest.json",
                    self.root / "manifest.backup.json",
                )
            for entry in self._entries.values():
                if entry.existed:
                    _atomic_copy_file(entry.target, self._backup_path(entry))
                    entry.backed_up = True
            self._verify_prebackup_target_identities(target_identities)
            self._write_journal(
                updates,
                deferred=defer_cleanup,
                group_commits=self._group_commits,
            )
            self._verify_prebackup_target_identities(target_identities)
            for directory in sorted(self._directories, key=lambda item: (len(item.parts), str(item))):
                if any(_is_relative_to(directory, atomic) for atomic in self._atomic_directories):
                    continue
                if not directory.exists():
                    directory.mkdir(parents=True, exist_ok=False)
                    created_directories.append(directory)
            for directory in sorted(entries_by_atomic_directory, key=str):
                entries = entries_by_atomic_directory[directory]
                changed.extend(entries)
                staged_directory = self.files_root / directory.relative_to(self.workspace)
                directory.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_directory, directory)
                for entry in entries:
                    entry.published = True
            for entry in self._entries.values():
                if entry.target in atomic_targets:
                    continue
                changed.append(entry)
                if entry.delete:
                    if entry.target.exists():
                        entry.target.unlink()
                        entry.published = True
                else:
                    entry.target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(entry.staged, entry.target)
                    entry.published = True
            committed_updates = self._attach_lineage_metadata(updates)
            manifest = update_stages(
                self.workspace,
                committed_updates,
                _published_artifacts=True,
            )
        except Exception:
            journal = {
                "run_id": self.run_id,
                "updates": [
                    {"stage": stage, "status": status}
                    for stage, (status, _) in updates.items()
                ],
            }
            if _transaction_manifest_is_committed(self.workspace, journal):
                self._committed = True
                self._changed = changed
                self._created_directories = created_directories
                if not defer_cleanup:
                    self._finalized = True
                    _cleanup_transaction_root(self.root)
                return load_manifest(self.workspace)
            try:
                self._rollback(changed)
                for directory in reversed(created_directories):
                    directory.rmdir()
            except Exception as rollback_error:
                self._retain_for_recovery = True
                raise WorkspaceTransactionError(
                    "transaction rollback could not complete; retry to recover the staged run"
                ) from rollback_error
            raise

        self._committed = True
        self._changed = changed
        self._created_directories = created_directories
        if not defer_cleanup:
            self._finalized = True
            _cleanup_transaction_root(self.root)
        return manifest

    def rollback(self) -> None:
        if not self._committed or not self._deferred or self._finalized:
            return
        try:
            self._rollback(self._changed)
            for directory in reversed(self._created_directories):
                directory.rmdir()
            backup = self.root / "manifest.backup.json"
            if backup.exists():
                _restore_backup_file(backup, self.workspace / "manifest.json")
        except Exception as exc:
            self._retain_for_recovery = True
            raise WorkspaceTransactionError(
                "deferred transaction rollback could not complete; retry to recover the staged run"
            ) from exc
        self._committed = False
        self._finalized = True
        _cleanup_transaction_root(self.root)

    def finalize(self) -> None:
        if not self._committed or not self._deferred:
            raise WorkspaceTransactionError("only a deferred committed transaction can be finalized")
        if self._finalized:
            return
        marker = self.root / "commit.marker"
        try:
            write_utf8(marker, self.run_id)
        except Exception:
            marker_committed = marker.is_file() and marker.read_text(encoding="utf-8") == self.run_id
            if not marker_committed and not _transaction_group_is_committed(
                self._journal_group_state()
            ):
                raise
        self._finalized = True
        _cleanup_transaction_root(self.root)


def _cleanup_transaction_root(root: Path) -> None:
    try:
        shutil.rmtree(root)
    except OSError:
        return
    runs_dir = root.parent
    try:
        runs_dir.rmdir()
    except OSError:
        pass


def _transaction_manifest_is_committed(workspace: Path, journal: Mapping[str, Any]) -> bool:
    run_id = journal.get("run_id")
    updates = journal.get("updates")
    if not isinstance(run_id, str) or not run_id or not isinstance(updates, list) or not updates:
        return False
    try:
        manifest = load_manifest(workspace)
    except (OSError, UnicodeError, json.JSONDecodeError, WorkspacePathError):
        return False
    stages = manifest.get("stages") if isinstance(manifest, dict) else None
    if not isinstance(stages, dict):
        return False
    for update in updates:
        if not isinstance(update, dict):
            return False
        stage = update.get("stage")
        status = update.get("status")
        stage_data = stages.get(stage) if isinstance(stage, str) else None
        if not isinstance(stage_data, dict):
            return False
        if stage_data.get("run_id") != run_id or stage_data.get("status") != status:
            return False
    return True


def _transaction_group_is_committed(journal: Mapping[str, Any]) -> bool:
    run_id = journal.get("run_id")
    commits = journal.get("group_commits")
    if not isinstance(run_id, str) or not run_id or not isinstance(commits, list) or not commits:
        return False
    for item in commits:
        if not isinstance(item, dict):
            return False
        workspace_value = item.get("workspace")
        stage = item.get("stage")
        status = item.get("status")
        if not all(isinstance(value, str) and value for value in (workspace_value, stage, status)):
            return False
        try:
            manifest = load_manifest(Path(workspace_value))
        except (OSError, UnicodeError, json.JSONDecodeError, WorkspacePathError):
            return False
        if not isinstance(manifest, dict):
            return False
        stages = manifest.get("stages")
        if not isinstance(stages, dict):
            return False
        stage_data = stages.get(stage)
        if not isinstance(stage_data, dict):
            return False
        if stage_data.get("run_id") != run_id or stage_data.get("status") != status:
            return False
    return True


def recover_workspace_transactions(workspace: Path, *, _lock_held: bool = False) -> None:
    """Restore interrupted uncommitted runs before a workspace is read or written again."""

    workspace = validate_workspace(workspace)
    if not _lock_held:
        with _PathTransactionLock("workspace", workspace):
            recover_workspace_transactions(workspace, _lock_held=True)
        return
    raw_runs_dir = workspace / ".runs"
    runs_dir = _resolve_path(raw_runs_dir, "workspace transaction root")
    if runs_dir != raw_runs_dir or not _is_relative_to(runs_dir, workspace):
        raise WorkspaceTransactionError("workspace transaction root is redirected")
    if not runs_dir.exists():
        return
    if not runs_dir.is_dir():
        raise WorkspaceTransactionError("workspace transaction root is not a directory")
    for raw_root in sorted(runs_dir.iterdir()):
        root = _resolve_path(raw_root, "workspace transaction run")
        if root != raw_root or not _is_relative_to(root, runs_dir) or not root.is_dir():
            raise WorkspaceTransactionError("workspace transaction root contains a non-directory entry")
        if not _valid_run_id(root.name):
            raise WorkspaceTransactionError("workspace transaction root contains an unknown run")
        marker = root / "run.marker"
        if (
            not marker.is_file()
            or _resolve_path(marker, "workspace transaction marker") != marker
            or marker.read_text(encoding="utf-8") != root.name
        ):
            raise WorkspaceTransactionError("workspace transaction run marker is invalid")
        journal_path = root / "journal.json"
        if not journal_path.exists():
            shutil.rmtree(root)
            continue
        try:
            if not journal_path.is_file() or _resolve_path(
                journal_path,
                "workspace transaction journal",
            ) != journal_path:
                raise WorkspaceTransactionError("workspace transaction journal is redirected")
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if not isinstance(journal, dict):
                raise WorkspaceTransactionError("transaction journal must be a JSON object")
            if journal.get("schema_version") != 2 or journal.get("run_id") != root.name:
                raise WorkspaceTransactionError("transaction journal identity is invalid")
            deferred = bool(journal.get("deferred"))
            commit_marker = root / "commit.marker"
            marker_committed = False
            if commit_marker.exists():
                if (
                    not commit_marker.is_file()
                    or _resolve_path(commit_marker, "workspace transaction commit marker")
                    != commit_marker
                    or commit_marker.read_text(encoding="utf-8") != root.name
                ):
                    raise WorkspaceTransactionError("workspace transaction commit marker is invalid")
                marker_committed = True
            committed = marker_committed or (
                not deferred and _transaction_manifest_is_committed(workspace, journal)
            ) or (deferred and _transaction_group_is_committed(journal))
            if committed:
                _cleanup_transaction_root(root)
                continue
            entries = journal.get("entries")
            group_commits = journal.get("group_commits")
            manifest_only_group = (
                deferred
                and isinstance(group_commits, list)
                and bool(group_commits)
                and all(
                    isinstance(item, dict)
                    and all(
                        isinstance(item.get(key), str) and item.get(key)
                        for key in ("workspace", "stage", "status")
                    )
                    for item in group_commits
                )
            )
            if not isinstance(entries, list) or (not entries and not manifest_only_group):
                raise WorkspaceTransactionError("transaction journal has no artifact entries")
            validated_entries: list[tuple[dict[str, Any], Path, Path, Path]] = []
            seen_targets: set[Path] = set()
            for item in entries:
                required_identity_fields = {
                    "backup_sha256",
                    "backup_size_bytes",
                    "staged_sha256",
                    "staged_size_bytes",
                }
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("target"), str)
                    or not isinstance(item.get("existed"), bool)
                    or not isinstance(item.get("delete"), bool)
                ):
                    raise WorkspaceTransactionError("transaction journal contains an invalid artifact entry")
                if not required_identity_fields.issubset(item):
                    raise WorkspaceTransactionError(
                        "transaction journal identity fields are invalid"
                    )
                existed = item["existed"]
                delete = item["delete"]
                backup_sha256 = item["backup_sha256"]
                backup_size = item["backup_size_bytes"]
                staged_sha256 = item["staged_sha256"]
                staged_size = item["staged_size_bytes"]
                valid_backup_identity = (
                    _is_sha256(backup_sha256)
                    and isinstance(backup_size, int)
                    and not isinstance(backup_size, bool)
                    and backup_size >= 0
                )
                valid_staged_identity = (
                    _is_sha256(staged_sha256)
                    and isinstance(staged_size, int)
                    and not isinstance(staged_size, bool)
                    and staged_size >= 0
                )
                if (
                    (existed and not valid_backup_identity)
                    or (
                        not existed
                        and (backup_sha256 is not None or backup_size is not None)
                    )
                    or (not delete and not valid_staged_identity)
                    or (
                        delete
                        and (staged_sha256 is not None or staged_size is not None)
                    )
                ):
                    raise WorkspaceTransactionError(
                        "transaction journal identity fields are invalid"
                    )
                target_value = item["target"]
                _validate_internal_value(target_value)
                raw_target = workspace / target_value
                target = _workspace_transaction_target(workspace, raw_target)
                if target != raw_target:
                    raise WorkspaceTransactionError("transaction recovery target is redirected")
                if target in seen_targets:
                    raise WorkspaceTransactionError("transaction journal contains duplicate targets")
                if target.exists() and not target.is_file():
                    raise WorkspaceTransactionError("transaction recovery target is not a file")
                seen_targets.add(target)
                raw_backup = root / "backups" / target_value
                raw_staged = root / "files" / target_value
                backup = _resolve_path(raw_backup, "transaction recovery backup")
                staged = _resolve_path(raw_staged, "transaction recovery staged file")
                if (
                    backup != raw_backup
                    or staged != raw_staged
                    or not _is_relative_to(backup, root / "backups")
                    or not _is_relative_to(staged, root / "files")
                ):
                    raise WorkspaceTransactionError("transaction recovery state is redirected")
                if (backup.exists() and not backup.is_file()) or (
                    staged.exists() and not staged.is_file()
                ):
                    raise WorkspaceTransactionError("transaction recovery state is not a file")
                if existed and not backup.exists():
                    raise WorkspaceTransactionError(
                        "transaction recovery backup is missing for an existing target"
                    )
                if not existed and backup.exists():
                    raise WorkspaceTransactionError(
                        "transaction recovery has an unexpected backup"
                    )
                if delete and staged.exists():
                    raise WorkspaceTransactionError(
                        "transaction recovery has an unexpected staged deletion"
                    )
                expected_backup_sha = backup_sha256
                expected_backup_size = backup_size
                if expected_backup_sha is not None:
                    if (
                        not backup.is_file()
                        or backup.stat().st_size != expected_backup_size
                        or sha256_file(backup) != expected_backup_sha
                    ):
                        raise WorkspaceTransactionError(
                            "transaction recovery backup checksum is invalid"
                        )
                expected_staged_sha = staged_sha256
                expected_staged_size = staged_size
                if staged.exists() and expected_staged_sha is not None and (
                    staged.stat().st_size != expected_staged_size
                    or sha256_file(staged) != expected_staged_sha
                ):
                    raise WorkspaceTransactionError(
                        "transaction recovery staged checksum is invalid"
                    )
                if target.exists() and (
                    expected_backup_sha is not None or expected_staged_sha is not None
                ):
                    target_sha = sha256_file(target)
                    target_size = target.stat().st_size
                    allowed_identities = {
                        (value_sha, value_size)
                        for value_sha, value_size in (
                            (expected_backup_sha, expected_backup_size),
                            (expected_staged_sha, expected_staged_size),
                        )
                        if value_sha is not None
                    }
                    if (target_sha, target_size) not in allowed_identities:
                        raise WorkspaceTransactionError(
                            "transaction recovery target content is unrecognized"
                        )
                validated_entries.append((item, target, backup, staged))

            directories = journal.get("directories", [])
            atomic_directories = journal.get("atomic_directories", [])
            if not isinstance(directories, list):
                raise WorkspaceTransactionError("transaction journal contains invalid directories")
            if not isinstance(atomic_directories, list) or not all(
                isinstance(item, str) for item in atomic_directories
            ):
                raise WorkspaceTransactionError(
                    "transaction journal contains invalid atomic directories"
                )
            validated_directories: list[tuple[Path, bool]] = []
            for item in directories:
                if isinstance(item, str):
                    value, created = item, True
                elif (
                    isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and ("created" not in item or isinstance(item["created"], bool))
                ):
                    value, created = item["path"], bool(item.get("created"))
                else:
                    raise WorkspaceTransactionError("transaction journal contains invalid directories")
                _validate_internal_value(value)
                raw_directory = workspace / value
                directory = _workspace_transaction_target(workspace, raw_directory)
                if directory != raw_directory:
                    raise WorkspaceTransactionError("transaction recovery directory is redirected")
                if directory in seen_targets:
                    raise WorkspaceTransactionError(
                        "transaction directory conflicts with an artifact target"
                    )
                if directory.exists() and not directory.is_dir():
                    raise WorkspaceTransactionError("transaction recovery directory is not a directory")
                validated_directories.append((directory, created))

            validated_atomic_directories: list[tuple[Path, Path]] = []
            validated_directory_paths = {directory for directory, _ in validated_directories}
            for value in atomic_directories:
                _validate_internal_value(value)
                raw_directory = workspace / value
                directory = _workspace_transaction_target(workspace, raw_directory)
                if directory != raw_directory or directory not in validated_directory_paths:
                    raise WorkspaceTransactionError(
                        "transaction atomic directory is invalid"
                    )
                if any(
                    _is_relative_to(directory, existing) or _is_relative_to(existing, directory)
                    for existing, _ in validated_atomic_directories
                ):
                    raise WorkspaceTransactionError("transaction atomic directories overlap")
                matching_entries = [
                    item
                    for item, target, _, _ in validated_entries
                    if _is_relative_to(target, directory)
                ]
                if not matching_entries or any(bool(item.get("delete")) for item in matching_entries):
                    raise WorkspaceTransactionError(
                        "transaction atomic directory has invalid artifacts"
                    )
                raw_hidden = root / "atomic-rollback" / value
                hidden = _resolve_path(raw_hidden, "transaction atomic rollback")
                if (
                    hidden != raw_hidden
                    or not _is_relative_to(hidden, root / "atomic-rollback")
                    or (hidden.exists() and not hidden.is_dir())
                ):
                    raise WorkspaceTransactionError("transaction atomic rollback is invalid")
                if directory.exists() and hidden.exists():
                    raise WorkspaceTransactionError(
                        "transaction atomic rollback state is ambiguous"
                    )
                validated_atomic_directories.append((directory, hidden))

            atomic_targets = {
                target
                for _, target, _, _ in validated_entries
                if any(
                    _is_relative_to(target, directory)
                    for directory, _ in validated_atomic_directories
                )
            }

            manifest_backup: Path | None = None
            if deferred:
                raw_manifest_backup = root / "manifest.backup.json"
                manifest_backup = _resolve_path(
                    raw_manifest_backup,
                    "transaction recovery manifest backup",
                )
                if manifest_backup != raw_manifest_backup or not manifest_backup.is_file():
                    raise WorkspaceTransactionError(
                        "transaction recovery manifest backup is invalid"
                    )
                expected_backup_sha = journal.get("manifest_backup_sha256")
                if (
                    not isinstance(expected_backup_sha, str)
                    or len(expected_backup_sha) != 64
                    or any(character not in "0123456789abcdef" for character in expected_backup_sha)
                    or sha256_file(manifest_backup) != expected_backup_sha
                ):
                    raise WorkspaceTransactionError(
                        "transaction recovery manifest backup checksum is invalid"
                    )
                backup_manifest = json.loads(manifest_backup.read_text(encoding="utf-8"))
                if not isinstance(backup_manifest, dict):
                    raise WorkspaceTransactionError(
                        "transaction recovery manifest backup is invalid"
                    )

            for directory, hidden in validated_atomic_directories:
                if directory.exists():
                    hidden.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(directory, hidden)

            for item, target, backup, staged in validated_entries:
                if target in atomic_targets:
                    continue
                if backup.exists():
                    _restore_backup_file(backup, target)
                elif item.get("existed") is True:
                    if not staged.exists():
                        raise WorkspaceTransactionError(
                            "transaction recovery backup is missing for an existing target"
                        )
                elif item.get("existed") is False and not bool(item.get("delete")) and target.exists() and not staged.exists():
                    target.unlink()
                elif "existed" not in item and target.exists() and not staged.exists():
                    raise WorkspaceTransactionError(
                        "legacy transaction journal cannot safely classify a published target"
                    )
            for directory, created in sorted(
                validated_directories,
                key=lambda item: (len(item[0].parts), str(item[0])),
                reverse=True,
            ):
                if not created:
                    continue
                if directory.exists():
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
            if manifest_backup is not None:
                _restore_backup_file(manifest_backup, workspace / "manifest.json")
            shutil.rmtree(root)
        except (OSError, UnicodeError, json.JSONDecodeError, WorkspacePathError) as exc:
            raise WorkspaceTransactionError(f"cannot recover interrupted transaction: {root}") from exc
    if runs_dir.exists() and not any(runs_dir.iterdir()):
        runs_dir.rmdir()


def _manifest_source_path(workspace: Path) -> Path | None:
    path = workspace / "manifest.json"
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspacePathError(f"cannot read workspace manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise WorkspacePathError(f"workspace manifest must be a JSON object: {path}")
    source = manifest.get("source")
    value = source.get("path") if isinstance(source, dict) else None
    if not isinstance(value, str) or not value:
        return None
    source_path = Path(value)
    if source_path.is_absolute():
        return _resolve_path(source_path, "manifest source")
    return _resolve_path(workspace / source_path, "manifest source")


def _resolve_in_validated_workspace(
    workspace: Path,
    value: str,
    role: WorkspaceRole,
    inputs: Iterable[Path],
    protected_paths: Iterable[Path],
) -> Path:
    if role not in {"read", "write"}:
        raise WorkspacePathError(f"unsupported workspace path role: {role}")
    _validate_internal_value(value)
    if Path(value).parts[0] == ".runs":
        raise WorkspacePathError("the workspace transaction namespace is reserved")
    candidate = _resolve_path(workspace / value, f"internal path {value}")
    if not _is_relative_to(candidate, workspace):
        raise WorkspacePathError(f"internal path escapes the workspace: {value}")

    if role == "write":
        protected = [
            workspace / "manifest.json",
            workspace / "versions" / "v0_original.txt",
            *[_resolve_path(Path(path), "run input") for path in inputs],
            *[_resolve_path(Path(path), "protected path") for path in protected_paths],
        ]
        source = _manifest_source_path(workspace)
        if source is not None:
            protected.append(source)
        if candidate == workspace or any(_same_file_or_path(candidate, path) for path in protected):
            raise WorkspacePathError(f"write target conflicts with a protected input: {value}")
    return candidate


def resolve_current_head(workspace: Path) -> Path:
    """Return the only committed current text artifact after verifying its lineage."""

    workspace = validate_workspace(workspace)
    recover_workspace_transactions(workspace)
    _validate_bound_snapshot_identity(workspace)
    manifest = load_manifest(workspace)
    current_head = manifest.get("current_head")
    if not isinstance(current_head, str) or not current_head.startswith("versions/"):
        raise WorkspaceIdentityError("manifest current_head is not a versioned text artifact")
    path = _resolve_in_validated_workspace(workspace, current_head, "read", (), ())
    if path.suffix != ".txt" or not path.is_file():
        raise WorkspaceIdentityError("manifest current_head is not a readable text artifact")
    return path


def resolve_in_workspace(
    workspace: Path,
    value: str,
    *,
    role: WorkspaceRole,
    inputs: Iterable[Path] = (),
    protected_paths: Iterable[Path] = (),
) -> Path:
    """Resolve one internal path and enforce its read/write role."""

    workspace = validate_workspace(workspace)
    recover_workspace_transactions(workspace)
    _validate_bound_snapshot_identity(workspace)
    return _resolve_in_validated_workspace(workspace, value, role, inputs, protected_paths)


def resolve_workspace_paths(
    workspace: Path,
    *,
    reads: Mapping[str, str] | None = None,
    writes: Mapping[str, str] | None = None,
    protected_paths: Iterable[Path] = (),
    allow_missing_workspace: bool = False,
) -> tuple[Path, dict[str, Path], dict[str, Path]]:
    """Preflight all paths for one run before the caller performs its first write."""

    workspace = _resolve_path(Path(workspace), "workspace")
    if workspace.exists():
        workspace = validate_workspace(workspace)
        pending_snapshot_init = (
            allow_missing_workspace
            and (
                (workspace / _SNAPSHOT_INIT_MARKER).exists()
                or (workspace / _SNAPSHOT_INIT_JOURNAL).exists()
            )
        )
        if not pending_snapshot_init:
            recover_workspace_transactions(workspace)
        if not (
            allow_missing_workspace
            and (_is_empty_workspace_shell(workspace) or pending_snapshot_init)
        ):
            _validate_bound_snapshot_identity(workspace)
    elif not allow_missing_workspace:
        raise WorkspacePathError(f"workspace does not exist: {workspace}")
    read_paths = {
        name: _resolve_in_validated_workspace(workspace, value, "read", (), ())
        for name, value in (reads or {}).items()
    }
    all_inputs = tuple(read_paths.values())
    protected_paths = tuple(_resolve_path(Path(path), "protected path") for path in protected_paths)
    manifest = workspace / "manifest.json"
    if any(_same_file_or_path(manifest, path) for path in (*all_inputs, *protected_paths)):
        raise WorkspacePathError("manifest update target aliases a run input")
    write_paths = {
        name: _resolve_in_validated_workspace(
            workspace,
            value,
            "write",
            all_inputs,
            protected_paths,
        )
        for name, value in (writes or {}).items()
    }
    for write_name, write_path in write_paths.items():
        for read_name, read_path in read_paths.items():
            if read_path.is_dir() and _is_relative_to(write_path, read_path):
                raise WorkspacePathError(
                    f"write target {write_name} is inside read directory {read_name}"
                )
    values = list(write_paths.items())
    for index, (left_name, left) in enumerate(values):
        for right_name, right in values[index + 1 :]:
            if _same_file_or_path(left, right):
                raise WorkspacePathError(f"write targets conflict: {left_name} and {right_name}")
    return workspace, read_paths, write_paths


def resolve_external_output_dir(value: str | Path, *, workspaces: Iterable[Path] = ()) -> Path:
    """Validate a delivery directory that is intentionally outside every workspace."""

    try:
        expanded = Path(value).expanduser()
    except RuntimeError as exc:
        raise WorkspacePathError(f"cannot resolve external output directory: {value}") from exc
    path = _resolve_path(expanded, "external output directory")
    if path.exists() and not path.is_dir():
        raise WorkspacePathError(f"external output path is not a directory: {path}")
    if path == path.parent:
        raise WorkspacePathError("filesystem roots cannot be external output directories")
    for workspace_value in workspaces:
        workspace = validate_workspace(workspace_value)
        if _is_relative_to(path, workspace) or _is_relative_to(workspace, path):
            raise WorkspacePathError(f"external output directory overlaps a workspace: {path}")
    return path


def workspace_protected_paths(
    workspace: Path,
    *,
    inputs: Iterable[Path] = (),
) -> tuple[Path, ...]:
    """Return the files that no delivery or cross-workspace run may overwrite."""

    workspace = validate_workspace(workspace)
    protected = [
        workspace / "manifest.json",
        workspace / "versions" / "v0_original.txt",
        *[_resolve_path(Path(item), "run input") for item in inputs],
    ]
    source = _manifest_source_path(workspace)
    if source is not None:
        protected.append(source)
    return tuple(protected)


def _external_protected_paths(
    workspaces: Iterable[Path],
    inputs: Iterable[Path],
) -> tuple[Path, ...]:
    protected = [_resolve_path(Path(item), "run input") for item in inputs]
    for workspace_value in workspaces:
        protected.extend(workspace_protected_paths(workspace_value))
    return tuple(protected)


def _resolve_external_output_child(root: Path, value: str, protected: Iterable[Path]) -> Path:
    _validate_internal_value(value)
    path = _resolve_path(root / value, f"external output child {value}")
    if not _is_relative_to(path, root):
        raise WorkspacePathError(f"external output child escapes its root: {value}")
    if any(_same_file_or_path(path, item) for item in protected):
        raise WorkspacePathError(f"external output aliases a protected input: {value}")
    return path


def resolve_external_output_path(
    root: Path,
    value: str,
    *,
    workspaces: Iterable[Path] = (),
    inputs: Iterable[Path] = (),
) -> Path:
    """Resolve one generated child and protect every workspace source and input."""

    workspaces = tuple(workspaces)
    root = resolve_external_output_dir(root, workspaces=workspaces)
    protected = _external_protected_paths(workspaces, inputs)
    return _resolve_external_output_child(root, value, protected)


def resolve_external_output_paths(
    root: Path,
    *,
    writes: Mapping[str, str],
    workspaces: Iterable[Path] = (),
    inputs: Iterable[Path] = (),
) -> dict[str, Path]:
    """Preflight a complete external delivery set before its first write."""

    workspaces = tuple(workspaces)
    root = resolve_external_output_dir(root, workspaces=workspaces)
    protected = _external_protected_paths(workspaces, inputs)
    paths = {
        name: _resolve_external_output_child(root, value, protected)
        for name, value in writes.items()
    }
    values = list(paths.items())
    for index, (left_name, left) in enumerate(values):
        for right_name, right in values[index + 1 :]:
            if _same_file_or_path(left, right):
                raise WorkspacePathError(f"external write targets conflict: {left_name} and {right_name}")
    return paths


@dataclass
class _DeliveryEntry:
    target: Path
    staged: Path
    existed: bool = False
    backed_up: bool = False
    published: bool = False


def _external_transaction_target(
    root: Path,
    target: Path,
    protected: Iterable[Path],
) -> Path:
    target = _resolve_path(Path(target), "external transaction target")
    if not _is_relative_to(target, root) or target == root:
        raise WorkspacePathError("external transaction target escapes its delivery root")
    relative = target.relative_to(root)
    _validate_internal_value(relative.as_posix())
    if relative.parts[0] == ExternalDeliveryTransaction.RUNS_DIR:
        raise WorkspacePathError("external transaction cannot overwrite its recovery state")
    if any(_same_file_or_path(target, path) for path in protected):
        raise WorkspacePathError("external transaction target aliases a protected input")
    return target


class ExternalDeliveryTransaction:
    """Publish a guarded external file bundle, with same-filesystem recovery state."""

    RUNS_DIR = ".delivery-runs"

    def __init__(
        self,
        root: Path,
        *,
        workspaces: Iterable[Path] = (),
        inputs: Iterable[Path] = (),
    ) -> None:
        self.workspaces = tuple(validate_workspace(path) for path in workspaces)
        self.root = resolve_external_output_dir(root, workspaces=self.workspaces)
        self.inputs = tuple(_resolve_path(Path(path), "run input") for path in inputs)
        self.protected = _external_protected_paths(self.workspaces, self.inputs)
        self.run_id = uuid.uuid4().hex
        self.run_root = self.root / self.RUNS_DIR / self.run_id
        self.files_root = self.run_root / "files"
        self.backups_root = self.run_root / "backups"
        self._entries: dict[Path, _DeliveryEntry] = {}
        self._directories: set[Path] = set()
        self._atomic_directories: set[Path] = set()
        self._root_existed = self.root.exists()
        self._published = False
        self._finalized = False
        self._retain_for_recovery = False
        self._commits: tuple[tuple[Path, str, str], ...] = ()
        self._lock = _PathTransactionLock("external delivery", self.root)

    def __enter__(self) -> "ExternalDeliveryTransaction":
        self._lock.acquire()
        try:
            recover_external_delivery_transactions(self.root, _lock_held=True)
            self.files_root.mkdir(parents=True, exist_ok=False)
            write_utf8(self.run_root / "run.marker", self.run_id)
        except Exception:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        try:
            if self._published and not self._finalized:
                if _delivery_manifest_committed(self._journal_commit_state()):
                    self._finalized = True
                    _cleanup_delivery_run(self.run_root)
                else:
                    self.rollback()
            elif not self._retain_for_recovery and not self._finalized:
                _cleanup_delivery_run(self.run_root)
            self._remove_empty_new_root()
        finally:
            self._lock.release()

    def _remove_empty_new_root(self) -> None:
        if not self._root_existed and self.root.exists():
            try:
                self.root.rmdir()
            except OSError:
                pass

    def _target_path(self, target: Path) -> Path:
        target = _external_transaction_target(self.root, target, self.protected)
        if target.exists() and not target.is_file():
            raise WorkspaceTransactionError("external transaction file target is not a file")
        return target

    def _record_missing_parents(self, directory: Path) -> None:
        while directory != self.root and not directory.exists():
            self._directories.add(directory)
            directory = directory.parent

    def stage_path(self, target: Path) -> Path:
        target = self._target_path(target)
        entry = self._entries.get(target)
        if entry is None:
            staged = self.files_root / target.relative_to(self.root)
            staged.parent.mkdir(parents=True, exist_ok=True)
            entry = _DeliveryEntry(target=target, staged=staged)
            self._entries[target] = entry
            self._record_missing_parents(target.parent)
        return entry.staged

    def stage_directory(self, directory: Path, *, require_new: bool = False) -> None:
        directory = _external_transaction_target(self.root, directory, self.protected)
        if directory.exists() and not directory.is_dir():
            raise WorkspaceTransactionError("external transaction directory target is not a directory")
        if require_new and directory.exists():
            raise WorkspaceTransactionError("external transaction directory target already exists")
        if require_new:
            if any(
                _is_relative_to(directory, existing) or _is_relative_to(existing, directory)
                for existing in self._atomic_directories
            ):
                raise WorkspaceTransactionError("atomic delivery directories cannot overlap")
            self._atomic_directories.add(directory)
        self._record_missing_parents(directory)

    def _backup_path(self, entry: _DeliveryEntry) -> Path:
        return self.backups_root / entry.target.relative_to(self.root)

    def _write_journal(self, commits: Iterable[tuple[Path, str, str]]) -> None:
        write_json(
            self.run_root / "journal.json",
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "root": str(self.root),
                "workspaces": [str(path) for path in self.workspaces],
                "inputs": [str(path) for path in self.inputs],
                "entries": [
                    {
                        "target": entry.target.relative_to(self.root).as_posix(),
                        "existed": entry.existed,
                    }
                    for entry in self._entries.values()
                ],
                "directories": [
                    directory.relative_to(self.root).as_posix()
                    for directory in sorted(self._directories, key=str)
                ],
                "atomic_directories": [
                    directory.relative_to(self.root).as_posix()
                    for directory in sorted(self._atomic_directories, key=str)
                ],
                "commits": [
                    {"workspace": str(validate_workspace(workspace)), "stage": stage, "status": status}
                    for workspace, stage, status in commits
                ],
            },
        )

    def _journal_commit_state(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "commits": [
                {"workspace": str(workspace), "stage": stage, "status": status}
                for workspace, stage, status in self._commits
            ],
        }

    def _rollback(self, changed: Iterable[_DeliveryEntry]) -> None:
        changed = list(changed)
        atomic_entries = {
            entry.target
            for entry in changed
            if any(_is_relative_to(entry.target, directory) for directory in self._atomic_directories)
        }
        for directory in sorted(self._atomic_directories, key=str):
            if not directory.exists():
                continue
            hidden = self.run_root / "atomic-rollback" / directory.relative_to(self.root)
            hidden.parent.mkdir(parents=True, exist_ok=True)
            os.replace(directory, hidden)
        for entry in reversed(changed):
            if entry.target in atomic_entries:
                continue
            backup = self._backup_path(entry)
            if entry.backed_up:
                if not backup.exists():
                    raise WorkspaceTransactionError("external delivery rollback backup is missing")
                _restore_backup_file(backup, entry.target)
            elif entry.published and entry.target.exists():
                entry.target.unlink()
        for directory in sorted(
            self._directories,
            key=lambda item: (len(item.parts), str(item)),
            reverse=True,
        ):
            if directory.exists():
                directory.rmdir()

    def publish(self, *, commits: Iterable[tuple[Path, str, str]] = ()) -> None:
        if self._published:
            raise WorkspaceTransactionError("external delivery is already published")
        if not self._entries:
            raise WorkspaceTransactionError("external delivery has no staged artifacts")
        for entry in self._entries.values():
            target = _external_transaction_target(self.root, entry.target, self.protected)
            if target != entry.target:
                raise WorkspaceTransactionError("external delivery target changed after staging")
            if target.exists() and not target.is_file():
                raise WorkspaceTransactionError("external delivery target is not a file")
            staged = _resolve_path(entry.staged, "staged external delivery artifact")
            if (
                staged != entry.staged
                or not _is_relative_to(staged, self.files_root)
                or not staged.is_file()
            ):
                raise WorkspaceTransactionError(
                    f"staged delivery artifact is invalid: {entry.target}"
                )
        for directory in self._directories:
            validated = _external_transaction_target(self.root, directory, self.protected)
            if validated != directory:
                raise WorkspaceTransactionError(
                    "external delivery directory changed after staging"
                )
            if directory.exists() and not directory.is_dir():
                raise WorkspaceTransactionError(
                    "external delivery directory target is not a directory"
                )

        entries_by_atomic_directory: dict[Path, list[_DeliveryEntry]] = {}
        atomic_targets: set[Path] = set()
        for directory in self._atomic_directories:
            entries = [
                entry
                for entry in self._entries.values()
                if _is_relative_to(entry.target, directory)
            ]
            if not entries:
                raise WorkspaceTransactionError("atomic delivery directory has no staged artifacts")
            if directory.exists():
                raise WorkspaceTransactionError("atomic delivery directory target already exists")
            raw_staged_directory = self.files_root / directory.relative_to(self.root)
            staged_directory = _resolve_path(
                raw_staged_directory,
                "atomic staged delivery directory",
            )
            if staged_directory != raw_staged_directory or not staged_directory.is_dir():
                raise WorkspaceTransactionError("atomic staged delivery directory is invalid")
            entries_by_atomic_directory[directory] = entries
            atomic_targets.update(entry.target for entry in entries)

        self._commits = tuple(commits)
        for entry in self._entries.values():
            entry.existed = entry.target.exists()
        changed: list[_DeliveryEntry] = []
        try:
            self._write_journal(self._commits)
            for directory in sorted(
                self._directories,
                key=lambda item: (len(item.parts), str(item)),
            ):
                if any(_is_relative_to(directory, atomic) for atomic in self._atomic_directories):
                    continue
                directory.mkdir(parents=False, exist_ok=True)
            for directory, entries in entries_by_atomic_directory.items():
                changed.extend(entries)
                staged_directory = self.files_root / directory.relative_to(self.root)
                directory.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_directory, directory)
                for entry in entries:
                    entry.published = True
            for entry in self._entries.values():
                if entry.target in atomic_targets:
                    continue
                changed.append(entry)
                backup = self._backup_path(entry)
                if entry.target.exists():
                    _atomic_copy_file(entry.target, backup)
                    entry.backed_up = True
                entry.target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(entry.staged, entry.target)
                entry.published = True
        except Exception:
            try:
                self._rollback(changed)
            except Exception as rollback_error:
                self._retain_for_recovery = True
                raise WorkspaceTransactionError(
                    "external delivery rollback could not complete; retry with the same output root"
                ) from rollback_error
            raise
        self._published = True

    def rollback(self) -> None:
        if not self._published or self._finalized:
            return
        try:
            self._rollback(self._entries.values())
        except Exception as exc:
            self._retain_for_recovery = True
            raise WorkspaceTransactionError(
                "external delivery rollback could not complete; retry with the same output root"
            ) from exc
        self._published = False
        self._finalized = True
        _cleanup_delivery_run(self.run_root)

    def finalize(self) -> None:
        if not self._published:
            raise WorkspaceTransactionError("external delivery must be published before finalization")
        if self._finalized:
            return
        marker = self.run_root / "commit.marker"
        try:
            write_utf8(marker, self.run_id)
        except Exception:
            marker_committed = (
                marker.is_file() and marker.read_text(encoding="utf-8") == self.run_id
            )
            if not marker_committed and not _delivery_manifest_committed(
                self._journal_commit_state()
            ):
                raise
        self._finalized = True
        _cleanup_delivery_run(self.run_root)


def _cleanup_delivery_run(run_root: Path) -> None:
    try:
        shutil.rmtree(run_root)
    except OSError:
        return
    try:
        run_root.parent.rmdir()
    except OSError:
        pass


def _delivery_manifest_committed(journal: Mapping[str, Any]) -> bool:
    run_id = journal.get("run_id")
    commits = journal.get("commits")
    if not isinstance(run_id, str) or not isinstance(commits, list) or not commits:
        return False
    for item in commits:
        if not isinstance(item, dict):
            return False
        workspace_value = item.get("workspace")
        stage = item.get("stage")
        status = item.get("status")
        if not all(isinstance(value, str) and value for value in (workspace_value, stage, status)):
            return False
        try:
            manifest = load_manifest(Path(workspace_value))
        except (OSError, UnicodeError, json.JSONDecodeError, WorkspacePathError):
            return False
        if not isinstance(manifest, dict):
            return False
        stages = manifest.get("stages")
        if not isinstance(stages, dict):
            return False
        stage_data = stages.get(stage, {})
        if not isinstance(stage_data, dict):
            return False
        if stage_data.get("run_id") != run_id or stage_data.get("status") != status:
            return False
    return True


def recover_external_delivery_transactions(root: Path, *, _lock_held: bool = False) -> None:
    """Restore unfinished external bundles before reusing their explicit output root."""

    root = resolve_external_output_dir(root)
    if not _lock_held:
        with _PathTransactionLock("external delivery", root):
            recover_external_delivery_transactions(root, _lock_held=True)
        return
    raw_runs_dir = root / ExternalDeliveryTransaction.RUNS_DIR
    runs_dir = _resolve_path(raw_runs_dir, "external delivery recovery root")
    if runs_dir != raw_runs_dir or not _is_relative_to(runs_dir, root):
        raise WorkspaceTransactionError("external delivery recovery root is redirected")
    if not runs_dir.exists():
        return
    if not runs_dir.is_dir():
        raise WorkspaceTransactionError("external delivery recovery root is not a directory")
    for raw_run_root in sorted(runs_dir.iterdir()):
        run_root = _resolve_path(raw_run_root, "external delivery run")
        if (
            run_root != raw_run_root
            or not _is_relative_to(run_root, runs_dir)
            or not run_root.is_dir()
            or not _valid_run_id(run_root.name)
        ):
            raise WorkspaceTransactionError("external delivery recovery root contains a non-directory")
        marker = run_root / "run.marker"
        journal_path = run_root / "journal.json"
        if (
            not marker.is_file()
            or _resolve_path(marker, "external delivery run marker") != marker
            or marker.read_text(encoding="utf-8") != run_root.name
        ):
            raise WorkspaceTransactionError("external delivery run marker is invalid")
        if not journal_path.exists():
            _cleanup_delivery_run(run_root)
            continue
        try:
            if not journal_path.is_file() or _resolve_path(
                journal_path,
                "external delivery journal",
            ) != journal_path:
                raise WorkspaceTransactionError("external delivery journal is redirected")
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if not isinstance(journal, dict):
                raise WorkspaceTransactionError("external delivery journal must be a JSON object")
            run_id = journal.get("run_id")
            if (
                journal.get("schema_version") != 1
                or not isinstance(run_id, str)
                or run_root.name != run_id
                or not marker.is_file()
                or _resolve_path(marker, "external delivery run marker") != marker
                or marker.read_text(encoding="utf-8") != run_id
                or journal.get("root") != str(root)
            ):
                raise WorkspaceTransactionError("external delivery journal identity is invalid")
            workspace_values = journal.get("workspaces")
            input_values = journal.get("inputs")
            if (
                not isinstance(workspace_values, list)
                or not all(isinstance(value, str) and value for value in workspace_values)
                or not isinstance(input_values, list)
                or not all(isinstance(value, str) and value for value in input_values)
            ):
                raise WorkspaceTransactionError("external delivery journal protections are invalid")
            workspaces = tuple(validate_workspace(Path(value)) for value in workspace_values)
            inputs = tuple(_resolve_path(Path(value), "recorded run input") for value in input_values)
            if any(
                os.path.normcase(str(path)) != os.path.normcase(value)
                for path, value in (*zip(workspaces, workspace_values), *zip(inputs, input_values))
            ):
                raise WorkspaceTransactionError("external delivery journal protections are redirected")
            resolve_external_output_dir(root, workspaces=workspaces)
            protected = _external_protected_paths(workspaces, inputs)
            commit_marker = run_root / "commit.marker"
            marker_committed = False
            if commit_marker.exists():
                if (
                    not commit_marker.is_file()
                    or _resolve_path(commit_marker, "external delivery commit marker")
                    != commit_marker
                    or commit_marker.read_text(encoding="utf-8") != run_id
                ):
                    raise WorkspaceTransactionError("external delivery commit marker is invalid")
                marker_committed = True
            if marker_committed or _delivery_manifest_committed(journal):
                _cleanup_delivery_run(run_root)
                continue

            entries = journal.get("entries")
            directories = journal.get("directories", [])
            atomic_directories = journal.get("atomic_directories", [])
            if not isinstance(entries, list) or not entries:
                raise WorkspaceTransactionError("external delivery journal has no artifact entries")
            if not isinstance(directories, list) or not all(isinstance(item, str) for item in directories):
                raise WorkspaceTransactionError("external delivery journal has invalid directories")
            if not isinstance(atomic_directories, list) or not all(
                isinstance(item, str) for item in atomic_directories
            ):
                raise WorkspaceTransactionError(
                    "external delivery journal has invalid atomic directories"
                )
            validated_entries: list[tuple[dict[str, Any], Path, Path, Path]] = []
            seen_targets: set[Path] = set()
            for item in entries:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("target"), str)
                    or ("existed" in item and not isinstance(item["existed"], bool))
                ):
                    raise WorkspaceTransactionError("external delivery journal has an invalid entry")
                value = item["target"]
                _validate_internal_value(value)
                raw_target = root / value
                target = _external_transaction_target(root, raw_target, protected)
                if target != raw_target:
                    raise WorkspaceTransactionError(
                        "external delivery recovery target is redirected"
                    )
                if target in seen_targets:
                    raise WorkspaceTransactionError("external delivery journal has duplicate targets")
                if target.exists() and not target.is_file():
                    raise WorkspaceTransactionError("external delivery recovery target is not a file")
                seen_targets.add(target)
                raw_backup = run_root / "backups" / value
                raw_staged = run_root / "files" / value
                backup = _resolve_path(raw_backup, "external delivery recovery backup")
                staged = _resolve_path(raw_staged, "external delivery recovery staged file")
                if (
                    backup != raw_backup
                    or staged != raw_staged
                    or not _is_relative_to(backup, run_root / "backups")
                    or not _is_relative_to(staged, run_root / "files")
                ):
                    raise WorkspaceTransactionError("external delivery recovery state is redirected")
                if (backup.exists() and not backup.is_file()) or (
                    staged.exists() and not staged.is_file()
                ):
                    raise WorkspaceTransactionError("external delivery recovery state is not a file")
                if not backup.exists() and item.get("existed") is True and (
                    not staged.exists() or not target.exists()
                ):
                    raise WorkspaceTransactionError(
                        "external delivery backup is missing for an existing target"
                    )
                if (
                    "existed" not in item
                    and target.exists()
                    and not staged.exists()
                ):
                    raise WorkspaceTransactionError(
                        "legacy delivery journal cannot safely classify a published target"
                    )
                validated_entries.append((item, target, backup, staged))

            validated_directories: list[Path] = []
            for value in directories:
                _validate_internal_value(value)
                raw_directory = root / value
                directory = _external_transaction_target(root, raw_directory, protected)
                if directory != raw_directory:
                    raise WorkspaceTransactionError(
                        "external delivery recovery directory is redirected"
                    )
                if directory in seen_targets:
                    raise WorkspaceTransactionError(
                        "external delivery directory conflicts with an artifact target"
                    )
                if directory.exists() and not directory.is_dir():
                    raise WorkspaceTransactionError(
                        "external delivery recovery directory is not a directory"
                    )
                validated_directories.append(directory)

            validated_atomic_directories: list[tuple[Path, Path]] = []
            for value in atomic_directories:
                _validate_internal_value(value)
                raw_directory = root / value
                directory = _external_transaction_target(root, raw_directory, protected)
                if directory != raw_directory or directory not in validated_directories:
                    raise WorkspaceTransactionError(
                        "external delivery atomic directory is invalid"
                    )
                if any(
                    _is_relative_to(directory, existing) or _is_relative_to(existing, directory)
                    for existing, _ in validated_atomic_directories
                ):
                    raise WorkspaceTransactionError(
                        "external delivery atomic directories overlap"
                    )
                if not any(_is_relative_to(target, directory) for target in seen_targets):
                    raise WorkspaceTransactionError(
                        "external delivery atomic directory has no artifacts"
                    )
                raw_hidden = run_root / "atomic-rollback" / value
                hidden = _resolve_path(raw_hidden, "external delivery atomic rollback")
                if (
                    hidden != raw_hidden
                    or not _is_relative_to(hidden, run_root / "atomic-rollback")
                    or (hidden.exists() and not hidden.is_dir())
                ):
                    raise WorkspaceTransactionError(
                        "external delivery atomic rollback is invalid"
                    )
                if directory.exists() and hidden.exists():
                    raise WorkspaceTransactionError(
                        "external delivery atomic rollback state is ambiguous"
                    )
                validated_atomic_directories.append((directory, hidden))

            atomic_targets = {
                target
                for target in seen_targets
                if any(
                    _is_relative_to(target, directory)
                    for directory, _ in validated_atomic_directories
                )
            }

            for directory, hidden in validated_atomic_directories:
                if directory.exists():
                    hidden.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(directory, hidden)

            for item, target, backup, staged in reversed(validated_entries):
                if target in atomic_targets:
                    continue
                if backup.exists():
                    _restore_backup_file(backup, target)
                elif item.get("existed") is True:
                    if not staged.exists():
                        raise WorkspaceTransactionError(
                            "external delivery backup is missing for an existing target"
                        )
                elif item.get("existed") is False and target.exists() and not staged.exists():
                    target.unlink()
                elif "existed" not in item and target.exists() and not staged.exists():
                    raise WorkspaceTransactionError(
                        "legacy delivery journal cannot safely classify a published target"
                    )
            for directory in sorted(
                validated_directories,
                key=lambda item: (len(item.parts), str(item)),
                reverse=True,
            ):
                if directory.exists():
                    directory.rmdir()
            shutil.rmtree(run_root)
        except (OSError, UnicodeError, json.JSONDecodeError, WorkspacePathError) as exc:
            raise WorkspaceTransactionError(
                f"cannot recover interrupted external delivery: {run_root}"
            ) from exc
    if runs_dir.exists() and not any(runs_dir.iterdir()):
        runs_dir.rmdir()
