"""
aider_runner — subprocess primitive for tiers backed by aider.

aider is a low-context coding assistant well-suited to local LLMs (Qwen,
Llama) where the 32k–128k context budget rules out claude-code's whole-repo
loading model. This module mirrors the shape of claude_code_runner.run so
the two can be swapped per tier from config.

Differences from claude_code_runner:
  * aider does not open PRs. It edits files; loomstack.core.github handles
    branching and PRs in the agent layer. AiderResult carries
    files_modified, never pr_url.
  * aider is configured via OPENAI_API_BASE / OPENAI_API_KEY env vars and
    expects model names prefixed ``openai/``.
  * Output parsing keys on aider's own summary lines ("Applied edit to X",
    "Tokens: ... sent, ... received", "Cost: $...").

The function never raises — failures are encoded in the result.
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
# Output parsing
# ---------------------------------------------------------------------------

# aider prints "Applied edit to <path>" once per file it modifies
_FILE_EDIT_RE = re.compile(r"^Applied edit to (\S+)", re.MULTILINE)

# Token / cost summary lines
# e.g. "Tokens: 1.2k sent, 240 received."
_TOKENS_SENT_RE = re.compile(
    r"Tokens:\s*([\d.]+)\s*([kKmM]?)\s+sent",
)
_TOKENS_RECV_RE = re.compile(
    r"([\d.]+)\s*([kKmM]?)\s+received",
)
# e.g. "Cost: $0.0024 message, $0.0024 session."
_COST_RE = re.compile(r"Cost:\s*\$([\d.]+)")

_FAILURE_PATTERNS = [
    re.compile(r"(?i)\berror:"),
    re.compile(r"(?i)\bexception:"),
    re.compile(r"(?i)\btraceback\b"),
    # aider says "No changes made to any files" when the LLM produced no edits
    re.compile(r"(?i)no changes (made|to apply)"),
]

# How many lines from the end to scan for signals
_TAIL_LINES = 80


@dataclass
class AiderResult:
    """Raw result from the aider subprocess."""

    success: bool
    exit_code: int
    files_modified: list[str]
    error_summary: str
    token_count: int
    cost_usd: float
    run_log_path: Path
    tail: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_env(endpoint: str, api_key: str) -> dict[str, str]:
    """
    Build the subprocess environment.

    OPENAI_API_BASE / OPENAI_API_KEY are the variables aider's OpenAI client
    reads. A non-empty key is required even when the local server (llama.cpp,
    vLLM) ignores it.
    """
    env = dict(os.environ)
    env["OPENAI_API_BASE"] = endpoint
    env["OPENAI_API_KEY"] = api_key
    return env


def _scale(value: str, suffix: str) -> int:
    """Convert ('1.2', 'k') → 1200; ('800', '') → 800."""
    n = float(value)
    s = suffix.lower()
    if s == "k":
        n *= 1_000
    elif s == "m":
        n *= 1_000_000
    return int(n)


def _parse_tail(lines: deque[str] | list[str]) -> tuple[list[str], int, float]:
    """
    Scan tail lines for files_modified, token_count, cost_usd.

    Token count is sent + received (approximate proxy for spend).
    """
    tail_text = "\n".join(lines)

    files_modified = sorted(set(_FILE_EDIT_RE.findall(tail_text)))

    token_count = 0
    sent = _TOKENS_SENT_RE.search(tail_text)
    if sent:
        token_count += _scale(sent.group(1), sent.group(2))
        # Search for "received" only inside the same line as "sent" to avoid
        # picking up unrelated numbers earlier in the log.
        line_with_sent = next((line for line in lines if "sent" in line.lower()), "")
        recv = _TOKENS_RECV_RE.search(line_with_sent)
        if recv:
            token_count += _scale(recv.group(1), recv.group(2))

    cost_usd = 0.0
    cost = _COST_RE.search(tail_text)
    if cost:
        cost_usd = float(cost.group(1))

    return files_modified, token_count, cost_usd


async def _stream_to_log(
    stream: asyncio.StreamReader,
    log_fh: aiofiles.threadpool.AsyncTextIOWrapper,  # type: ignore[name-defined]
    ring: deque[str],
) -> None:
    """Read lines, write to log, keep last N in ring."""
    async for raw_line in stream:
        line = raw_line.decode(errors="replace")
        await log_fh.write(line)
        ring.append(line.rstrip())


def _write_run_log_frontmatter(task_id: str, model: str, endpoint: str) -> str:
    return (
        f"---\ntask_id: {task_id}\nrunner: aider\nmodel: {model}\n"
        f"endpoint: {endpoint}\nstatus: in_progress\n---\n\n"
    )


def _write_run_log_footer(result: AiderResult) -> str:
    status = "done" if result.success else "failed"
    files_yaml = "[]" if not result.files_modified else "[" + ", ".join(result.files_modified) + "]"
    return (
        f"\n---\nstatus: {status}\n"
        f"exit_code: {result.exit_code}\n"
        f"files_modified: {files_yaml}\n"
        f"token_count: {result.token_count}\n"
        f"cost_usd: {result.cost_usd}\n"
        f"---\n"
    )


def _build_cmd(
    model: str,
    task: Task,
    repo_path: Path,
    claude_md_path: Path,
) -> list[str]:
    """
    Construct the aider command line.

    Files passed positionally are added to aider's chat at startup. We add
    CLAUDE.md (project rules) plus everything in task.context_files. Paths
    are resolved against repo_path and only included if they exist on disk
    — aider treats nonexistent paths as files-to-create, which is rarely
    what we want for context.
    """
    cmd = [
        "aider",
        "--model",
        f"openai/{model}",
        "--no-auto-commits",
        "--yes-always",
        "--no-pretty",
        "--no-stream",
        "--no-check-update",
        "--no-show-model-warnings",
        "--message",
        task.notes or task.description,
    ]

    if claude_md_path.exists():
        cmd.append(str(claude_md_path))

    for rel in task.context_files:
        abs_path = repo_path / rel
        if abs_path.exists():
            cmd.append(str(abs_path))

    return cmd


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_aider(
    endpoint: str,
    model: str,
    repo_path: Path,
    task: Task,
    claude_md_path: Path,
    run_log_path: Path,
    api_key: str = "sk-local",
    timeout_s: int = 1800,
) -> AiderResult:
    """
    Spawn aider as a subprocess and run it against the given task.

    Parameters
    ----------
    endpoint:
        Base URL for the OpenAI-compatible LLM endpoint, e.g.
        ``http://gx10.local:8081/v1``. Injected as ``OPENAI_API_BASE``.
    model:
        Model name without the ``openai/`` prefix — the prefix is added
        when constructing the aider ``--model`` arg.
    repo_path:
        Absolute path to the managed project repo. Used as cwd.
    task:
        The task to execute. ``notes`` (falling back to ``description``) is
        passed as aider's ``--message``. ``context_files`` are added as
        positional file args so aider includes them in the chat at startup.
    claude_md_path:
        Path to the repo's CLAUDE.md. Added as a context file if it exists.
    run_log_path:
        Path to write streaming output. Parents are created if missing.
    api_key:
        Token sent as ``OPENAI_API_KEY``. Local servers ignore the value
        but the OpenAI client requires non-empty.
    timeout_s:
        Subprocess timeout in seconds. On timeout: process is killed and
        the result is marked failed.

    Returns
    -------
    AiderResult
        Always returns (never raises). Failures are encoded in the result.
    """
    env = _build_env(endpoint, api_key)
    ring: deque[str] = deque(maxlen=_TAIL_LINES)

    run_log_path.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "aider_runner.start",
        task_id=task.task_id,
        model=model,
        endpoint=endpoint,
        timeout_s=timeout_s,
    )

    cmd = _build_cmd(model, task, repo_path, claude_md_path)

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
                result = AiderResult(
                    success=False,
                    exit_code=-1,
                    files_modified=[],
                    error_summary=f"Subprocess timed out after {timeout_s}s",
                    token_count=0,
                    cost_usd=0.0,
                    run_log_path=run_log_path,
                    tail=list(ring),
                )
                await log_fh.write(_write_run_log_footer(result))
                log.warning("aider_runner.timeout", task_id=task.task_id, timeout_s=timeout_s)
                return result

            files_modified, token_count, cost_usd = _parse_tail(ring)

            # Success requires: clean exit, at least one file modified,
            # and no failure markers in the tail.
            tail_text = "\n".join(ring)
            has_failure_marker = any(p.search(tail_text) for p in _FAILURE_PATTERNS)
            success = exit_code == 0 and bool(files_modified) and not has_failure_marker

            error_summary = ""
            if not success:
                error_lines = [
                    line for line in ring if any(p.search(line) for p in _FAILURE_PATTERNS)
                ]
                if error_lines:
                    error_summary = "\n".join(error_lines[-5:])
                elif exit_code != 0:
                    error_summary = f"aider exited with code {exit_code}"
                elif not files_modified:
                    error_summary = "aider produced no file edits"
                elif ring:
                    error_summary = ring[-1]

            result = AiderResult(
                success=success,
                exit_code=exit_code,
                files_modified=files_modified,
                error_summary=error_summary,
                token_count=token_count,
                cost_usd=cost_usd,
                run_log_path=run_log_path,
                tail=list(ring),
            )
            await log_fh.write(_write_run_log_footer(result))

    except OSError as exc:
        log.error("aider_runner.spawn_error", task_id=task.task_id, error=str(exc))
        result = AiderResult(
            success=False,
            exit_code=-1,
            files_modified=[],
            error_summary=f"Failed to spawn aider: {exc}",
            token_count=0,
            cost_usd=0.0,
            run_log_path=run_log_path,
            tail=[],
        )
        try:
            async with aiofiles.open(run_log_path, "a", encoding="utf-8") as log_fh:
                await log_fh.write(_write_run_log_footer(result))
        except OSError as log_exc:
            log.warning(
                "aider_runner.log_write_failed",
                task_id=task.task_id,
                error=str(log_exc),
            )
        return result

    log.info(
        "aider_runner.done",
        task_id=task.task_id,
        success=result.success,
        exit_code=result.exit_code,
        files_modified=result.files_modified,
        cost_usd=result.cost_usd,
    )
    return result
