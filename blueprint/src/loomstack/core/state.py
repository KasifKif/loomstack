"""
Derive task status from the combination of:
  1. .loomstack/runs/<task-id>.md frontmatter (authoritative when present)
  2. GitHub open PRs (branch matching pattern → PROPOSED)
  3. Local git branches (feature branch exists → IN_PROGRESS)
  4. Fallback → PENDING

All functions are pure (given their inputs) or async where I/O is required.
No function in this module writes to disk or mutates state.
"""

from __future__ import annotations

import asyncio
import re
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PROPOSED = "proposed"       # PR open, awaiting review/CI
    BLOCKED = "blocked"         # waiting for approval gate
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Run-file frontmatter parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<body>.*?\n)---\s*\n",
    re.DOTALL,
)
_FIELD_RE = re.compile(r"^(?P<key>\w+)\s*:\s*(?P<value>.+)$", re.MULTILINE)

_VALID_FRONTMATTER_STATUSES = {s.value for s in TaskStatus}


def _parse_run_file_status(content: str) -> TaskStatus | None:
    """
    Extract the ``status:`` field from a run-file's YAML frontmatter.
    Returns None if the frontmatter is absent or the field is missing/unknown.
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return None
    for field in _FIELD_RE.finditer(m.group("body")):
        if field.group("key") == "status":
            value = field.group("value").strip().lower()
            if value in _VALID_FRONTMATTER_STATUSES:
                return TaskStatus(value)
    return None


def read_run_file_status(run_file: Path) -> TaskStatus | None:
    """
    Read a .loomstack/runs/<task-id>.md and return the status from its
    frontmatter, or None if the file doesn't exist or has no status field.
    Pure synchronous read — callers that need async should use
    ``read_run_file_status_async``.
    """
    if not run_file.exists():
        return None
    try:
        content = run_file.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_run_file_status(content)


async def read_run_file_status_async(run_file: Path) -> TaskStatus | None:
    """Async variant of read_run_file_status."""
    if not run_file.exists():
        return None
    try:
        content = await asyncio.get_event_loop().run_in_executor(
            None, run_file.read_text, "utf-8"
        )
    except OSError:
        return None
    return _parse_run_file_status(content)


# ---------------------------------------------------------------------------
# gh CLI wrappers
# ---------------------------------------------------------------------------

_BRANCH_PR_PATTERN = re.compile(r"^(feat|fix|chore|agent)/.*")


class GhError(Exception):
    """Raised when the gh CLI returns a non-zero exit code."""


async def _run_gh(*args: str) -> str:
    """Run a gh command and return stdout. Raises GhError on failure."""
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise GhError(
            f"gh {' '.join(args)} failed (exit {proc.returncode}): "
            f"{stderr.decode().strip()}"
        )
    return stdout.decode().strip()


async def get_open_pr_for_branch(branch: str) -> str | None:
    """
    Return the PR URL if an open PR exists for the given branch, else None.
    Uses ``gh pr list`` filtered by head branch.
    """
    try:
        output = await _run_gh(
            "pr", "list",
            "--head", branch,
            "--state", "open",
            "--json", "url",
            "--jq", ".[0].url",
        )
    except GhError:
        return None
    return output if output else None


async def branch_exists_remote(branch: str) -> bool:
    """Return True if the branch exists on the remote (origin)."""
    try:
        await _run_gh("api", f"repos/{{owner}}/{{repo}}/branches/{branch}")
        return True
    except GhError:
        return False


# ---------------------------------------------------------------------------
# Local git helpers (sync — git operations are fast)
# ---------------------------------------------------------------------------


def _local_branch_exists(repo_path: Path, branch: str) -> bool:
    """Return True if a local git branch exists in repo_path."""
    ref = repo_path / ".git" / "refs" / "heads" / branch
    return ref.exists()


def _task_branch_name(task_id: str) -> str:
    """Derive the expected branch name for a task."""
    return f"feat/{task_id.lower()}"


# ---------------------------------------------------------------------------
# Core status derivation
# ---------------------------------------------------------------------------


async def derive_status(
    task_id: str,
    repo_path: Path,
    loomstack_dir: Path | None = None,
) -> TaskStatus:
    """
    Derive authoritative TaskStatus for a task by combining three sources:

    1. .loomstack/runs/<task-id>.md frontmatter ``status:`` field (most authoritative)
    2. GitHub: open PR for the task's feature branch → PROPOSED
    3. Local git: feature branch exists → IN_PROGRESS
    4. Fallback → PENDING

    Parameters
    ----------
    task_id:
        The task ID (e.g. ``MC-042``).
    repo_path:
        Absolute path to the managed project repo.
    loomstack_dir:
        Path to the ``.loomstack/`` directory. Defaults to ``repo_path / ".loomstack"``.
    """
    if loomstack_dir is None:
        loomstack_dir = repo_path / ".loomstack"

    run_file = loomstack_dir / "runs" / f"{task_id}.md"
    file_status = await read_run_file_status_async(run_file)
    if file_status is not None:
        return file_status

    branch = _task_branch_name(task_id)

    pr_url = await get_open_pr_for_branch(branch)
    if pr_url:
        return TaskStatus.PROPOSED

    if _local_branch_exists(repo_path, branch):
        return TaskStatus.IN_PROGRESS

    return TaskStatus.PENDING


async def derive_all_statuses(
    task_ids: list[str],
    repo_path: Path,
    loomstack_dir: Path | None = None,
) -> dict[str, TaskStatus]:
    """
    Derive status for multiple tasks concurrently.
    Returns a mapping of task_id → TaskStatus.
    """
    results = await asyncio.gather(
        *[derive_status(tid, repo_path, loomstack_dir) for tid in task_ids]
    )
    return dict(zip(task_ids, results, strict=False))


# ---------------------------------------------------------------------------
# Approval gate helpers
# ---------------------------------------------------------------------------


def approval_marker_path(task_id: str, loomstack_dir: Path) -> Path:
    """Return the path of the approval marker file for a task."""
    return loomstack_dir / "approvals" / task_id


def is_approved(task_id: str, loomstack_dir: Path) -> bool:
    """Return True if the architect approval marker exists for task_id."""
    return approval_marker_path(task_id, loomstack_dir).exists()
