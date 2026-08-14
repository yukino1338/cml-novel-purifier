from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    JOB_INPUT_DIR_NAME,
    JOB_INTERNAL_DIR_NAME,
    JOB_RESULT_DIR_NAME,
    JOB_WORKSPACES_DIR_NAME,
    SKILL_PUBLIC_ROOT,
)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def run(job_root: Path) -> dict[str, Any]:
    root = Path(job_root).resolve(strict=False)
    public_root = SKILL_PUBLIC_ROOT.resolve(strict=False)
    if root == root.parent:
        raise ValueError("job root cannot be a filesystem root")
    if root.name in {JOB_INPUT_DIR_NAME, JOB_RESULT_DIR_NAME, JOB_INTERNAL_DIR_NAME}:
        raise ValueError("a reserved job area cannot itself be the job root")
    if _is_relative_to(root, public_root):
        raise ValueError("job root must be outside the Skill public root")
    if root.exists() and not root.is_dir():
        raise ValueError(f"job root is not a directory: {root}")

    input_dir = root / JOB_INPUT_DIR_NAME
    result_dir = root / JOB_RESULT_DIR_NAME
    workspace_root = root / JOB_INTERNAL_DIR_NAME / JOB_WORKSPACES_DIR_NAME
    for path in (input_dir, result_dir, workspace_root):
        if path.exists() and not path.is_dir():
            raise ValueError(f"job-root area is not a directory: {path}")

    input_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    workspace_root.mkdir(parents=True, exist_ok=True)
    return {
        "schema": "cml.job-root.v1",
        "job_root": str(root),
        "input_dir": str(input_dir),
        "result_dir": str(result_dir),
        "workspace_root": str(workspace_root),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the clear input, result, and hidden workspace areas for one job root."
    )
    parser.add_argument("job_root", help="External root for one or more novel-cleaning jobs.")
    args = parser.parse_args()
    try:
        result = run(Path(args.job_root))
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "failed", "error": str(exc)},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
