"""
Thin async wrapper around git and gh CLI for branch/PR operations.

All functions use ``asyncio.create_subprocess_exec`` and raise ``GitError``
on non-zero exit codes. The dispatcher and agents call these instead of
shelling out directly.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GitError(Exception):
    """Raised when a git or gh CLI command fails."""

    def __init__(self, cmd: str, exit_code: int, stderr: str) -> None:
        self.cmd = cmd
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"{cmd} failed (exit {exit_code}): {stderr}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _run(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
) -> tuple[str, str]:
    """
    Run a command and return (stdout, stderr) decoded as UTF-8.

    Raises ``GitError`` if ``check`` is True and the exit code is non-zero.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode(errors="replace").strip()
    stderr = stderr_bytes.decode(errors="replace").strip()

    if check and proc.returncode != 0:
        cmd_str = " ".join(args)
        log.error(
            "github.cmd_failed",
            cmd=cmd_str,
            exit_code=proc.returncode,
            stderr=stderr,
        )
        raise GitError(cmd_str, proc.returncode or 1, stderr)

    return stdout, stderr


# ---------------------------------------------------------------------------
# Branch operations
# ---------------------------------------------------------------------------


async def create_branch(repo_path: Path, branch_name: str) -> None:
    """Create and switch to a new local branch from current HEAD."""
    await _run("git", "checkout", "-b", branch_name, cwd=repo_path)
    log.info("github.branch_created", branch=branch_name)


async def checkout_branch(repo_path: Path, branch_name: str) -> None:
    """Switch to an existing local branch."""
    await _run("git", "checkout", branch_name, cwd=repo_path)
    log.info("github.branch_checked_out", branch=branch_name)


# ---------------------------------------------------------------------------
# Commit and push
# ---------------------------------------------------------------------------


async def commit_and_push(
    repo_path: Path,
    branch: str,
    message: str,
) -> None:
    """Stage all changes, commit, and push to origin."""
    await _run("git", "add", "-A", cwd=repo_path)

    # git diff --cached --quiet exits 0 when nothing staged, 1 when there are diffs
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--cached",
        "--quiet",
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    if proc.returncode == 0:
        log.info("github.nothing_to_commit", branch=branch)
        return

    await _run("git", "commit", "-m", message, cwd=repo_path)
    await _run("git", "push", "-u", "origin", branch, cwd=repo_path)
    log.info("github.committed_and_pushed", branch=branch, message=message)


# ---------------------------------------------------------------------------
# Pull request operations
# ---------------------------------------------------------------------------


async def open_pr(
    repo_path: Path,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
) -> str:
    """Open a pull request via ``gh pr create``. Returns the PR URL."""
    stdout, _ = await _run(
        "gh",
        "pr",
        "create",
        "--head",
        branch,
        "--base",
        base,
        "--title",
        title,
        "--body",
        body,
        cwd=repo_path,
    )
    log.info("github.pr_opened", branch=branch, base=base, url=stdout)
    return stdout


async def get_pr_status(repo_path: Path, pr_url: str) -> dict[str, Any]:
    """
    Get PR status including state, mergeability, and CI check results.

    Returns parsed JSON with keys: state, mergeable, statusCheckRollup.
    """
    stdout, _ = await _run(
        "gh",
        "pr",
        "view",
        pr_url,
        "--json",
        "state,mergeable,statusCheckRollup",
        cwd=repo_path,
    )
    result: dict[str, Any] = json.loads(stdout)
    return result


async def get_pr_diff(repo_path: Path, pr_url: str) -> str:
    """Return the unified diff for a pull request."""
    stdout, _ = await _run(
        "gh",
        "pr",
        "diff",
        pr_url,
        cwd=repo_path,
    )
    return stdout


async def list_open_prs(repo_path: Path) -> list[dict[str, Any]]:
    """List all open PRs with number, title, branch, URL, and state."""
    stdout, _ = await _run(
        "gh",
        "pr",
        "list",
        "--json",
        "number,title,headRefName,url,state",
        cwd=repo_path,
    )
    result: list[dict[str, Any]] = json.loads(stdout) if stdout else []
    return result
