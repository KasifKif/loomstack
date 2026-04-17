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
from dataclasses import dataclass
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
    PROPOSED = "proposed"  # PR open, awaiting review/CI
    BLOCKED = "blocked"  # waiting for approval gate
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# RunMeta — structured read of run-file frontmatter
# ---------------------------------------------------------------------------


@dataclass
class RunMeta:
    """
    Parsed metadata from a ``.loomstack/runs/<task-id>.md`` file.

    Fields are populated from *all* ``---`` frontmatter blocks in the file,
    with later blocks overriding earlier ones. This supports the append-only
    pattern where claude_code_runner writes an initial block (status: in_progress)
    and a footer block (status: done/failed) without rewriting the file.

    The dispatcher uses ``status``, ``tier``, ``retry_count``, ``last_error``,
    and ``last_diff`` to drive the escalation ladder.
    """

    status: TaskStatus | None = None
    task_id: str | None = None
    tier: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    last_diff: str | None = None
    model: str | None = None
    endpoint: str | None = None
    exit_code: int | None = None
    pr_url: str | None = None
    branch: str | None = None
    token_count: int | None = None
    cost_usd: float | None = None


# ---------------------------------------------------------------------------
# Run-file frontmatter parsing
# ---------------------------------------------------------------------------

# Matches any ``---`` fenced block, not just the first one.
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<body>.*?\n)---\s*\n",
    re.DOTALL | re.MULTILINE,
)
_FIELD_RE = re.compile(r"^(?P<key>\w+)\s*:\s*(?P<value>.+)$", re.MULTILINE)

_VALID_FRONTMATTER_STATUSES = {s.value for s in TaskStatus}


def _parse_run_meta(content: str) -> RunMeta:
    """
    Parse all ``---`` frontmatter blocks in a run file and merge them into
    a single RunMeta. Later blocks override earlier ones.
    """
    merged: dict[str, str] = {}
    for m in _FRONTMATTER_RE.finditer(content):
        for fld in _FIELD_RE.finditer(m.group("body")):
            merged[fld.group("key")] = fld.group("value").strip()

    if not merged:
        return RunMeta()

    status: TaskStatus | None = None
    raw_status = merged.get("status", "").lower()
    if raw_status in _VALID_FRONTMATTER_STATUSES:
        status = TaskStatus(raw_status)

    def _safe_int(key: str, default: int | None = None) -> int | None:
        if key not in merged:
            return default
        try:
            return int(merged[key])
        except (ValueError, TypeError):
            return default

    def _safe_float(key: str, default: float | None = None) -> float | None:
        if key not in merged:
            return default
        try:
            return float(merged[key])
        except (ValueError, TypeError):
            return default

    return RunMeta(
        status=status,
        task_id=merged.get("task_id"),
        tier=merged.get("tier"),
        retry_count=_safe_int("retry_count", 0) or 0,
        last_error=merged.get("last_error"),
        last_diff=merged.get("last_diff"),
        model=merged.get("model"),
        endpoint=merged.get("endpoint"),
        exit_code=_safe_int("exit_code"),
        pr_url=merged.get("pr_url") or None,
        branch=merged.get("branch") or None,
        token_count=_safe_int("token_count"),
        cost_usd=_safe_float("cost_usd"),
    )


def read_run_meta(run_file: Path) -> RunMeta:
    """
    Read a ``.loomstack/runs/<task-id>.md`` and return parsed RunMeta.
    Returns a default (empty) RunMeta if the file doesn't exist or can't be read.
    """
    if not run_file.exists():
        return RunMeta()
    try:
        content = run_file.read_text(encoding="utf-8")
    except OSError:
        return RunMeta()
    return _parse_run_meta(content)


async def read_run_meta_async(run_file: Path) -> RunMeta:
    """Async variant of read_run_meta."""
    if not run_file.exists():
        return RunMeta()
    try:
        content = await asyncio.to_thread(run_file.read_text, "utf-8")
    except OSError:
        return RunMeta()
    return _parse_run_meta(content)


# ---------------------------------------------------------------------------
# gh CLI wrappers
# ---------------------------------------------------------------------------

_BRANCH_PR_PATTERN = re.compile(r"^(feat|fix|chore|agent)/.*")


class GhError(Exception):
    """Raised when the gh CLI returns a non-zero exit code."""


async def _run_gh(*args: str) -> str:
    """Run a gh command and return stdout. Raises GhError on failure."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise GhError(
            f"gh {' '.join(args)} failed (exit {proc.returncode}): {stderr.decode().strip()}"
        )
    return stdout.decode().strip()


async def get_open_pr_for_branch(branch: str) -> str | None:
    """
    Return the PR URL if an open PR exists for the given branch, else None.
    Uses ``gh pr list`` filtered by head branch.
    """
    try:
        output = await _run_gh(
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url",
            "--jq",
            ".[0].url",
        )
    except GhError:
        return None
    return output if output else None


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
    meta = await derive_run_meta(task_id, repo_path, loomstack_dir)
    if meta.status is not None:
        return meta.status

    branch = _task_branch_name(task_id)

    pr_url = await get_open_pr_for_branch(branch)
    if pr_url:
        return TaskStatus.PROPOSED

    if _local_branch_exists(repo_path, branch):
        return TaskStatus.IN_PROGRESS

    return TaskStatus.PENDING


async def derive_run_meta(
    task_id: str,
    repo_path: Path,
    loomstack_dir: Path | None = None,
) -> RunMeta:
    """
    Read the RunMeta for a task from its run file.

    Returns a default (empty) RunMeta if no run file exists.
    Used by the dispatcher to get retry_count, tier, last_error, etc.
    """
    if loomstack_dir is None:
        loomstack_dir = repo_path / ".loomstack"
    run_file = loomstack_dir / "runs" / f"{task_id}.md"
    return await read_run_meta_async(run_file)


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
    return dict(zip(task_ids, results, strict=True))


# ---------------------------------------------------------------------------
# Approval gate helpers
# ---------------------------------------------------------------------------


def approval_marker_path(task_id: str, loomstack_dir: Path) -> Path:
    """Return the path of the approval marker file for a task."""
    return loomstack_dir / "approvals" / task_id


def is_approved(task_id: str, loomstack_dir: Path) -> bool:
    """Return True if the architect approval marker exists for task_id."""
    return approval_marker_path(task_id, loomstack_dir).exists()
