"""
Tests for agents/aider_runner.py.

No real aider binary is invoked. Subprocess calls are mocked; aider output
is simulated via fixture strings recorded from real runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from loomstack.agents.aider_runner import (
    AiderResult,
    _build_cmd,
    _build_env,
    _parse_tail,
    _write_run_log_footer,
    _write_run_log_frontmatter,
    run_aider,
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


# Recorded aider --no-pretty --no-stream output, success case
_OUTPUT_SUCCESS = """\
Aider v0.86.2
Model: openai/qwen3-coder-next
Git repo: .git with 47 files
Repo-map: using 1024 tokens
Added crates/config/src/lib.rs to the chat
Added CLAUDE.md to the chat (read-only)

> Add a TOML config loader in crates/config/src/lib.rs

Applied edit to crates/config/src/lib.rs
Applied edit to crates/config/Cargo.toml

Tokens: 3.2k sent, 480 received.
Cost: $0.0042 message, $0.0042 session.
"""

# Failure: aider returns no edits
_OUTPUT_NO_EDITS = """\
Aider v0.86.2
Model: openai/qwen3-coder-next
Added CLAUDE.md to the chat (read-only)

> Add a TOML config loader

I cannot make edits without seeing crates/config/src/lib.rs.
No changes made to any files.

Tokens: 1.1k sent, 80 received.
Cost: $0.0010 message, $0.0010 session.
"""

# Failure: aider raises an error
_OUTPUT_ERROR = """\
Aider v0.86.2
Connecting to model...
Error: 401 Unauthorized — invalid API key.
Traceback (most recent call last):
  File "aider/main.py", line 42, in <module>
    raise APIError("invalid key")
"""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestBuildEnv:
    def test_sets_api_base(self) -> None:
        env = _build_env("http://gx10.local:8081/v1", "sk-x")
        assert env["OPENAI_API_BASE"] == "http://gx10.local:8081/v1"

    def test_sets_api_key(self) -> None:
        env = _build_env("http://x", "sk-secret")
        assert env["OPENAI_API_KEY"] == "sk-secret"

    def test_inherits_path(self) -> None:
        env = _build_env("http://x", "sk-x")
        assert "PATH" in env


class TestBuildCmd:
    def test_includes_model_with_openai_prefix(self) -> None:
        task = _make_task()
        cmd = _build_cmd("qwen3-coder-next", task, Path("/repo"), Path("/nonexistent.md"))
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "openai/qwen3-coder-next"

    def test_message_is_notes_when_present(self) -> None:
        task = _make_task(notes="please do X")
        cmd = _build_cmd("m", task, Path("/repo"), Path("/nonexistent.md"))
        assert cmd[cmd.index("--message") + 1] == "please do X"

    def test_message_falls_back_to_description(self) -> None:
        task = _make_task(notes="")
        cmd = _build_cmd("m", task, Path("/repo"), Path("/nonexistent.md"))
        assert cmd[cmd.index("--message") + 1] == "Implement config loader"

    def test_includes_existing_claude_md(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# rules")
        task = _make_task()
        cmd = _build_cmd("m", task, tmp_path, claude_md)
        assert str(claude_md) in cmd

    def test_skips_missing_claude_md(self) -> None:
        task = _make_task()
        cmd = _build_cmd("m", task, Path("/repo"), Path("/does/not/exist.md"))
        assert "/does/not/exist.md" not in cmd

    def test_includes_existing_context_files(self, tmp_path: Path) -> None:
        ctx = tmp_path / "src" / "x.rs"
        ctx.parent.mkdir(parents=True)
        ctx.write_text("// stub")
        task = _make_task(context_files=["src/x.rs"])
        cmd = _build_cmd("m", task, tmp_path, Path("/none"))
        assert str(ctx) in cmd

    def test_skips_missing_context_files(self, tmp_path: Path) -> None:
        task = _make_task(context_files=["does/not/exist.rs"])
        cmd = _build_cmd("m", task, tmp_path, Path("/none"))
        assert not any("does/not/exist.rs" in arg for arg in cmd)

    def test_disables_auto_commits_and_prompts(self) -> None:
        task = _make_task()
        cmd = _build_cmd("m", task, Path("/repo"), Path("/none"))
        assert "--no-auto-commits" in cmd
        assert "--yes-always" in cmd
        assert "--no-pretty" in cmd
        assert "--no-stream" in cmd


class TestParseTail:
    def test_success_extracts_files_and_tokens(self) -> None:
        lines = _OUTPUT_SUCCESS.splitlines()
        files, tokens, cost = _parse_tail(lines)
        assert files == ["crates/config/Cargo.toml", "crates/config/src/lib.rs"]
        # 3.2k + 480 = 3680
        assert tokens == 3680
        assert cost == pytest.approx(0.0042)

    def test_no_edits_returns_empty_files(self) -> None:
        lines = _OUTPUT_NO_EDITS.splitlines()
        files, tokens, cost = _parse_tail(lines)
        assert files == []
        assert tokens == 1180
        assert cost == pytest.approx(0.0010)

    def test_empty_input(self) -> None:
        files, tokens, cost = _parse_tail([])
        assert files == []
        assert tokens == 0
        assert cost == 0.0

    def test_dedupes_repeated_edit_lines(self) -> None:
        lines = [
            "Applied edit to a.py",
            "Applied edit to a.py",
            "Applied edit to b.py",
        ]
        files, _, _ = _parse_tail(lines)
        assert files == ["a.py", "b.py"]


class TestRunLogHelpers:
    def test_frontmatter_marks_runner(self) -> None:
        fm = _write_run_log_frontmatter("MC-001", "qwen3", "http://x/v1")
        assert "runner: aider" in fm
        assert "task_id: MC-001" in fm
        assert "status: in_progress" in fm

    def test_footer_success(self) -> None:
        r = AiderResult(
            success=True,
            exit_code=0,
            files_modified=["a.py", "b.py"],
            error_summary="",
            token_count=100,
            cost_usd=0.001,
            run_log_path=Path("/tmp/x.md"),
        )
        footer = _write_run_log_footer(r)
        assert "status: done" in footer
        assert "files_modified: [a.py, b.py]" in footer

    def test_footer_failure_empty_files(self) -> None:
        r = AiderResult(
            success=False,
            exit_code=1,
            files_modified=[],
            error_summary="oops",
            token_count=0,
            cost_usd=0.0,
            run_log_path=Path("/tmp/x.md"),
        )
        footer = _write_run_log_footer(r)
        assert "status: failed" in footer
        assert "files_modified: []" in footer


# ---------------------------------------------------------------------------
# Subprocess integration (mocked)
# ---------------------------------------------------------------------------


def aiter_from_list(items: list[bytes]):  # type: ignore[no-untyped-def]
    """Async iterator wrapping a list of byte strings."""

    class _Iter:
        def __init__(self) -> None:
            self._items = iter(items)

        def __aiter__(self) -> _Iter:
            return self

        async def __anext__(self) -> bytes:
            try:
                return next(self._items)
            except StopIteration:
                raise StopAsyncIteration from None

    return _Iter()


def _make_mock_proc(output: str, exit_code: int = 0) -> MagicMock:
    lines = [line.encode() + b"\n" for line in output.splitlines()]
    reader = MagicMock(spec=asyncio.StreamReader)
    reader.__aiter__ = MagicMock(return_value=aiter_from_list(lines))
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = exit_code
    proc.wait = AsyncMock(return_value=exit_code)
    proc.kill = MagicMock()
    return proc


class TestRunAider:
    @pytest.mark.asyncio
    async def test_success(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"
        mock_proc = _make_mock_proc(_OUTPUT_SUCCESS, exit_code=0)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = await run_aider(
                endpoint="http://gx10.local:8081/v1",
                model="qwen3-coder-next",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
            )

        assert result.success is True
        assert result.exit_code == 0
        assert result.files_modified == [
            "crates/config/Cargo.toml",
            "crates/config/src/lib.rs",
        ]
        assert result.token_count == 3680
        assert result.cost_usd == pytest.approx(0.0042)
        assert run_log.exists()

    @pytest.mark.asyncio
    async def test_no_edits_is_failure(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"
        mock_proc = _make_mock_proc(_OUTPUT_NO_EDITS, exit_code=0)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = await run_aider(
                endpoint="http://x",
                model="m",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
            )

        assert result.success is False
        assert result.files_modified == []
        assert result.error_summary != ""

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_failure(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"
        mock_proc = _make_mock_proc(_OUTPUT_ERROR, exit_code=1)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = await run_aider(
                endpoint="http://x",
                model="m",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
            )

        assert result.success is False
        assert result.exit_code == 1
        assert "401" in result.error_summary or "invalid" in result.error_summary.lower()

    @pytest.mark.asyncio
    async def test_run_log_written_with_runner_marker(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"
        mock_proc = _make_mock_proc(_OUTPUT_SUCCESS, exit_code=0)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            await run_aider(
                endpoint="http://x",
                model="m",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
            )

        content = run_log.read_text()
        assert "runner: aider" in content
        assert "task_id: MC-001" in content
        assert "status: done" in content

    @pytest.mark.asyncio
    async def test_timeout_returns_failed(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"

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
                "loomstack.agents.aider_runner._stream_to_log",
                side_effect=_hanging_stream,
            ),
        ):
            result = await run_aider(
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
        assert "status: failed" in run_log.read_text()

    @pytest.mark.asyncio
    async def test_spawn_error_returns_failed(self, tmp_path: Path) -> None:
        task = _make_task()
        run_log = tmp_path / ".loomstack" / "runs" / "MC-001.md"

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("aider not found")),
        ):
            result = await run_aider(
                endpoint="http://x",
                model="m",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=run_log,
            )

        assert result.success is False
        assert "aider not found" in result.error_summary

    @pytest.mark.asyncio
    async def test_run_log_parent_created(self, tmp_path: Path) -> None:
        task = _make_task()
        deep_log = tmp_path / "a" / "b" / "c" / "MC-001.md"
        mock_proc = _make_mock_proc(_OUTPUT_SUCCESS, exit_code=0)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            await run_aider(
                endpoint="http://x",
                model="m",
                repo_path=tmp_path,
                task=task,
                claude_md_path=tmp_path / "CLAUDE.md",
                run_log_path=deep_log,
            )

        assert deep_log.exists()
