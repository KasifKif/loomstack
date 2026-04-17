"""
claude_code_runner — core subprocess primitive for all Claude-Code-backed tiers.

Every tier that wraps claude-code (Code Worker, Mac Worker, Content Worker,
Architect) uses run_claude_code(). It:
  1. Spawns claude-code as an async subprocess with the correct env vars.
  2. Streams stdout/stderr line-by-line to .loomstack/runs/<task-id>.md.
  3. Parses the tail of the output for success/failure signals.
  4. Returns a ClaudeCodeResult with token counts and cost estimate.

Tiers wrap this function and translate ClaudeCodeResult → AgentResult.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiofiles
import structlog

if TYPE_CHECKING:
    from pathlib import Path

    from loomstack.core.plan_parser import Task

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

# Patterns used to detect outcome in the tail of claude-code output
_SUCCESS_PATTERNS = [
    re.compile(r"(?i)\bpr\s+(opened|created)\b"),
    re.compile(r"(?i)\bpull request\b.*\bhttps://github\.com\b"),
    re.compile(r"(?i)\btask\s+complete\b"),
]
_FAILURE_PATTERNS = [
    re.compile(r"(?i)\btask\s+failed\b"),
    re.compile(r"(?i)\berror:\s"),
    re.compile(r"(?i)\bfatal:\s"),
]
_PR_URL_RE = re.compile(r"https://github\.com/\S+/pull/\d+")
_BRANCH_RE = re.compile(r"(?i)branch[:\s]+([a-zA-Z0-9/_.\-]+[a-zA-Z0-9/_\-])")

# Token count patterns emitted by claude-code's summary line
_TOKENS_RE = re.compile(r"tokens?[:\s]+(\d+)", re.IGNORECASE)
_COST_RE = re.compile(r"\$\s*([\d.]+)")

# How many lines from the end to scan for signals
_TAIL_LINES = 50


@dataclass
class ClaudeCodeResult:
    """Raw result from the claude-code subprocess."""

    success: bool
    exit_code: int
    pr_url: str | None
    branch: str | None
    error_summary: str
    token_count: int
    cost_usd: float
    # Full path to the run log written during execution
    run_log_path: Path
    # Last N lines of output (for retry context)
    tail: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_env(endpoint: str, model: str) -> dict[str, str]:
    """
    Build the subprocess environment. Inherits the current process env and
    overrides the two variables claude-code uses for routing.
    """
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = endpoint
    env["ANTHROPIC_MODEL"] = model
    return env


def _parse_tail(lines: deque[str] | list[str]) -> tuple[bool, str | None, str | None, int, float]:
    """
    Scan the tail lines for outcome signals.

    Returns (success, pr_url, branch, token_count, cost_usd).
    """
    tail_text = "\n".join(lines)

    pr_url: str | None = None
    branch: str | None = None
    token_count = 0
    cost_usd = 0.0

    m = _PR_URL_RE.search(tail_text)
    if m:
        pr_url = m.group(0)

    m2 = _BRANCH_RE.search(tail_text)
    if m2:
        branch = m2.group(1).strip()

    m3 = _TOKENS_RE.search(tail_text)
    if m3:
        token_count = int(m3.group(1))

    m4 = _COST_RE.search(tail_text)
    if m4:
        cost_usd = float(m4.group(1))

    success = any(p.search(tail_text) for p in _SUCCESS_PATTERNS)

    return success, pr_url, branch, token_count, cost_usd


async def _stream_to_log(
    stream: asyncio.StreamReader,
    log_fh: aiofiles.threadpool.AsyncTextIOWrapper,  # type: ignore[name-defined]
    ring: deque[str],
) -> None:
    """Read lines from stream, write to log file, keep last N in ring (deque maxlen)."""
    async for raw_line in stream:
        line = raw_line.decode(errors="replace")
        await log_fh.write(line)
        ring.append(line.rstrip())


def _write_run_log_frontmatter(task_id: str, model: str, endpoint: str) -> str:
    return (
        f"---\ntask_id: {task_id}\nmodel: {model}\nendpoint: {endpoint}\n"
        f"status: in_progress\n---\n\n"
    )


def _write_run_log_footer(result: ClaudeCodeResult) -> str:
    status = "done" if result.success else "failed"
    return (
        f"\n---\nstatus: {status}\n"
        f"exit_code: {result.exit_code}\n"
        f"pr_url: {result.pr_url or ''}\n"
        f"branch: {result.branch or ''}\n"
        f"token_count: {result.token_count}\n"
        f"cost_usd: {result.cost_usd}\n"
        f"---\n"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_claude_code(
    endpoint: str,
    model: str,
    repo_path: Path,
    task: Task,
    claude_md_path: Path,
    run_log_path: Path,
    timeout_s: int = 1800,
) -> ClaudeCodeResult:
    """
    Spawn claude-code as a subprocess and run it against the given task.

    Parameters
    ----------
    endpoint:
        Base URL for the LLM endpoint, e.g. ``http://gx10.local:8080/v1``.
        Injected as ``ANTHROPIC_BASE_URL``.
    model:
        Model name, e.g. ``qwen3-coder-next``.
        Injected as ``ANTHROPIC_MODEL``.
    repo_path:
        Absolute path to the managed project repo. Used as cwd.
    task:
        The task to execute. The task ID is used to name the run log.
    claude_md_path:
        Path to the repo's CLAUDE.md. Passed to claude-code as a pre-load
        context file via ``--context``.
    run_log_path:
        Path to write streaming output. Caller is responsible for ensuring
        the parent directory exists.
    timeout_s:
        Subprocess timeout in seconds. On timeout: process is killed and
        result is marked failed with retry context.

    Returns
    -------
    ClaudeCodeResult
        Always returns (never raises). Failures are encoded in the result.
    """
    env = _build_env(endpoint, model)
    ring: deque[str] = deque(maxlen=_TAIL_LINES)

    run_log_path.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "claude_code_runner.start",
        task_id=task.task_id,
        model=model,
        endpoint=endpoint,
        timeout_s=timeout_s,
    )

    cmd = [
        "claude",
        "--context",
        str(claude_md_path),
        "--print",
        "--dangerously-skip-permissions",
        task.notes or task.description,
    ]

    try:
        async with aiofiles.open(run_log_path, "w", encoding="utf-8") as log_fh:
            await log_fh.write(_write_run_log_frontmatter(task.task_id, model, endpoint))
            await log_fh.flush()

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=repo_path,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            if proc.stdout is None:
                raise OSError("subprocess stdout is None — cannot stream output")

            try:
                await asyncio.wait_for(
                    _stream_to_log(proc.stdout, log_fh, ring),
                    timeout=timeout_s,
                )
                await proc.wait()
                exit_code = proc.returncode or 0
            except TimeoutError:
                proc.kill()
                await proc.wait()
                await log_fh.write(f"\n[loomstack] TIMEOUT after {timeout_s}s — process killed\n")
                result = ClaudeCodeResult(
                    success=False,
                    exit_code=-1,
                    pr_url=None,
                    branch=None,
                    error_summary=f"Subprocess timed out after {timeout_s}s",
                    token_count=0,
                    cost_usd=0.0,
                    run_log_path=run_log_path,
                    tail=list(ring),
                )
                await log_fh.write(_write_run_log_footer(result))
                log.warning(
                    "claude_code_runner.timeout",
                    task_id=task.task_id,
                    timeout_s=timeout_s,
                )
                return result

            success, pr_url, branch, token_count, cost_usd = _parse_tail(ring)

            # Exit code 0 is necessary but not sufficient for success
            if exit_code != 0:
                success = False

            error_summary = ""
            if not success:
                error_lines = [
                    line for line in ring if any(p.search(line) for p in _FAILURE_PATTERNS)
                ]
                error_summary = (
                    "\n".join(error_lines[-5:]) if error_lines else ring[-1] if ring else ""
                )

            result = ClaudeCodeResult(
                success=success,
                exit_code=exit_code,
                pr_url=pr_url,
                branch=branch,
                error_summary=error_summary,
                token_count=token_count,
                cost_usd=cost_usd,
                run_log_path=run_log_path,
                tail=list(ring),
            )
            await log_fh.write(_write_run_log_footer(result))

    except OSError as exc:
        # claude-code binary not found or other spawn error
        log.error("claude_code_runner.spawn_error", task_id=task.task_id, error=str(exc))
        result = ClaudeCodeResult(
            success=False,
            exit_code=-1,
            pr_url=None,
            branch=None,
            error_summary=f"Failed to spawn claude-code: {exc}",
            token_count=0,
            cost_usd=0.0,
            run_log_path=run_log_path,
            tail=[],
        )
        # Best-effort log write
        try:
            async with aiofiles.open(run_log_path, "a", encoding="utf-8") as log_fh:
                await log_fh.write(_write_run_log_footer(result))
        except OSError as log_exc:
            log.warning(
                "claude_code_runner.log_write_failed",
                task_id=task.task_id,
                error=str(log_exc),
            )
        return result

    log.info(
        "claude_code_runner.done",
        task_id=task.task_id,
        success=result.success,
        exit_code=result.exit_code,
        pr_url=result.pr_url,
        cost_usd=result.cost_usd,
    )
    return result
