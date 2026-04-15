"""
Tests for agents/claude_code_runner.py.

No real claude-code binary is invoked. All subprocess calls are mocked.
Recorded LLM output is simulated via fixture strings.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from loomstack.agents.claude_code_runner import (
    ClaudeCodeResult,
    _build_env,
    _parse_tail,
    _write_run_log_footer,
    _write_run_log_frontmatter,
    run_claude_code,
)
from loomstack.core.plan_parser import Task


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_task(**kwargs: object) -> Task:
    defaults: dict[str, object] = {
        "task_id": "MC-001",
        "description": "Implement config loader",
        "role": "code_worker",
        "acceptance": {"ci": "passes"},
        "notes": "Add a TOML config loader in crates/config/src/lib.rs",
    }
    defaults.update(kwargs)
    return Task.model_validate(defaults)


# Simulated claude-code output fixtures
_OUTPUT_SUCCESS = """\
Reading CLAUDE.md...
Working on: Add a TOML config loader
Creating branch feat/mc-001...
branch: feat/mc-001
Writing crates/config/src/lib.rs...
Running tests...
All tests pass.
Opening pull request...
PR created: https://github.com/owner/meshcord/pull/42
Task complete. tokens: 2150 $0.003
"""

_OUTPUT_FAILURE = """\
Reading CLAUDE.md...
Working on: Add a TOML config loader
Creating branch feat/mc-001...
Error: cannot find file crates/config/src/lib.rs
Task failed.
"""

_OUTPUT_NO_SIGNAL = """\
Some output with no recognisable success or failure signal.
Just random lines.
"""


# ---------------------------------------------------------------------------
# Unit tests for pure helpers
# ---------------------------------------------------------------------------


class TestBuildEnv:
    def test_sets_base_url(self) -> None:
        env = _build_env("http://gx10.local:8080/v1", "qwen3")
        assert env["ANTHROPIC_BASE_URL"] == "http://gx10.local:8080/v1"

    def test_sets_model(self) -> None:
        env = _build_env("http://gx10.local:8080/v1", "qwen3-coder-next")
        assert env["ANTHROPIC_MODEL"] == "qwen3-coder-next"

    def test_inherits_path(self) -> None:
        env = _build_env("http://x", "m")
        assert "PATH" in env


class TestParseTail:
    def test_success_with_pr(self) -> None:
        lines = _OUTPUT_SUCCESS.splitlines()
        success, pr_url, branch, tokens, cost = _parse_tail(lines)
        assert success is True
        assert pr_url == "https://github.com/owner/meshcord/pull/42"
        assert tokens == 2150
        assert cost == pytest.approx(0.003)

    def test_failure(self) -> None:
        lines = _OUTPUT_FAILURE.splitlines()
        success, pr_url, branch, tokens, cost = _parse_tail(lines)
        assert success is False
        assert pr_url is None

    def test_no_signal_not_success(self) -> None:
        lines = _OUTPUT_NO_SIGNAL.splitlines()
        success, *_ = _parse_tail(lines)
        assert success is False

    def test_branch_extracted(self) -> None:
        lines = _OUTPUT_SUCCESS.splitlines()
        _, _, branch, _, _ = _parse_tail(lines)
        assert branch == "feat/mc-001"

    def test_empty_lines(self) -> None:
        success, pr_url, branch, tokens, cost = _parse_tail([])
        assert success is False
        assert pr_url is None
        assert tokens == 0
        assert cost == 0.0


class TestRunLogHelpers:
    def test_frontmatter_contains_task_id(self) -> None:
        fm = _write_run_log_frontmatter("MC-001", "qwen3", "http://gx10.local/v1")
        assert "task_id: MC-001" in fm
        assert "status: in_progress" in fm

    def test_footer_success(self) -> None:
        r = ClaudeCodeResult(
            success=True, exit_code=0, pr_url="https://gh.com/pr/1",
            branch="feat/mc-001", error_summary="", token_count=100,
            cost_usd=0.001, run_log_path=Path("/tmp/x.md"),
        )
        footer = _write_run_log_footer(r)
        assert "status: done" in footer
        assert "pr_url: https://gh.com/pr/1" in footer

    def test_footer_failure(self) -> None:
        r = ClaudeCodeResult(
            success=False, exit_code=1, pr_url=None,
            branch=None, error_summary="oops", token_count=0,
            cost_usd=0.0, run_log_path=Path("/tmp/x.md"),
        )
        footer = _write_run_log_footer(r)
        assert "status: failed" in footer


# ---------------------------------------------------------------------------
# Integration tests for run_claude_code (subprocess mocked)
# ---------------------------------------------------------------------------


def _make_mock_proc(output: str, exit_code: int = 0) -> MagicMock:
    """Build a mock asyncio.Process that yields the given output."""
    lines = [line.encode() + b"\n" for line in output.splitlines()]
    # StreamReader that yields lines then EOF
    reader = MagicMock(spec=asyncio.StreamReader)
    reader.__aiter__ = MagicMock(return_value=aiter_from_list(lines))
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = exit_code
    proc.wait = AsyncMock(return_value=exit_code)
    proc.kill = MagicMock()
    return proc


def aiter_from_list(items: list[bytes]):  # type: ignore[return]
    """Return an async iterator over a list of byte strings."""
    class _Iter:
        def __init__(self) -> None:
            self._items = iter(items)

        def __aiter__(self) -> "_Iter":
            return self

        async def __anext__(self) -> bytes:
            try:
                return next(self._items)
            except StopIteration:
                raise StopAsyncIteration

    return _Iter()


class TestRunClaudeCode:
    @pytest.mark.asyncio
    async def test_success(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"
        mock_proc = _make_mock_proc(_OUTPUT_SUCCESS, exit_code=0)

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ):
            result = await run_claude_code(
                endpoint="http://gx10.local:8080/v1",
                model="qwen3-coder-next",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
            )

        assert result.success is True
        assert result.exit_code == 0
        assert result.pr_url == "https://github.com/owner/meshcord/pull/42"
        assert result.token_count == 2150
        assert result.cost_usd == pytest.approx(0.003)
        assert run_log.exists()

    @pytest.mark.asyncio
    async def test_failure_exit_code(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"
        mock_proc = _make_mock_proc(_OUTPUT_SUCCESS, exit_code=1)

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ):
            result = await run_claude_code(
                endpoint="http://gx10.local:8080/v1",
                model="qwen3-coder-next",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
            )

        assert result.success is False
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_failure_output(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"
        mock_proc = _make_mock_proc(_OUTPUT_FAILURE, exit_code=0)

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ):
            result = await run_claude_code(
                endpoint="http://gx10.local:8080/v1",
                model="qwen3-coder-next",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
            )

        assert result.success is False
        assert result.error_summary != ""

    @pytest.mark.asyncio
    async def test_run_log_written(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"
        mock_proc = _make_mock_proc(_OUTPUT_SUCCESS, exit_code=0)

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ):
            await run_claude_code(
                endpoint="http://x",
                model="m",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
            )

        content = run_log.read_text()
        assert "task_id: MC-001" in content
        assert "status: done" in content

    @pytest.mark.asyncio
    async def test_timeout_returns_failed(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"

        async def slow_stream(*args, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(9999)

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock(spec=asyncio.StreamReader)
        mock_proc.stdout.__aiter__ = MagicMock(return_value=aiter_from_list([]))
        mock_proc.returncode = -1
        mock_proc.wait = AsyncMock(return_value=-1)
        mock_proc.kill = MagicMock()

        async def _hanging_stream(stream, log_fh, ring):  # type: ignore[no-untyped-def]
            await asyncio.sleep(9999)

        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
            patch(
                "loomstack.agents.claude_code_runner._stream_to_log",
                side_effect=_hanging_stream,
            ),
        ):
            result = await run_claude_code(
                endpoint="http://x",
                model="m",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
                timeout_s=1,
            )

        assert result.success is False
        assert result.exit_code == -1
        assert "timed out" in result.error_summary.lower()
        assert run_log.exists()
        assert "status: failed" in run_log.read_text()

    @pytest.mark.asyncio
    async def test_spawn_error_returns_failed(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("claude not found")),
        ):
            result = await run_claude_code(
                endpoint="http://x",
                model="m",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
            )

        assert result.success is False
        assert "claude not found" in result.error_summary

    @pytest.mark.asyncio
    async def test_run_log_parent_created(self, tmp_path: Path) -> None:
        """Parent directories of run_log_path are created if missing."""
        task = _make_task()
        deep_log = tmp_path / "a" / "b" / "c" / "MC-001.md"
        mock_proc = _make_mock_proc(_OUTPUT_SUCCESS, exit_code=0)

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ):
            await run_claude_code(
                endpoint="http://x",
                model="m",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=deep_log,
            )

        assert deep_log.exists()
