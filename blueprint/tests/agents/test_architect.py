"""Tests for blueprint/src/loomstack/agents/architect.py."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, patch

import pytest

from loomstack.agents.architect import Architect
from loomstack.agents.base import Blocked, Failed, Proposed, TaskContext
from loomstack.agents.claude_code_runner import ClaudeCodeResult
from loomstack.core.github import GitError
from loomstack.core.plan_parser import AcceptanceBlock, Task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(
    task_id: str = "LS-001",
    description: str = "Redesign plugin architecture",
    tags: list[str] | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        description=description,
        role="architect",
        acceptance=AcceptanceBlock(tests_pass="unit"),
        tags=tags or ["security"],
    )


def make_ctx(tmp_path: Path) -> TaskContext:
    return TaskContext(
        repo_path=tmp_path,
        loomstack_dir=tmp_path / ".loomstack",
        claude_md_path=tmp_path / "CLAUDE.md",
        run_log_path=tmp_path / ".loomstack" / "runs" / "LS-001.md",
    )


def make_architect(repo_path: Path | None = None) -> Architect:
    return Architect(
        endpoint="http://localhost:8080/v1",
        model="claude-opus",
        repo_path=repo_path or Path("/tmp/test-repo"),
        claude_md_path=Path("/tmp/test-repo/CLAUDE.md"),
    )


def make_claude_result(
    success: bool = True,
    pr_url: str | None = None,
    error_summary: str = "",
    tail: list[str] | None = None,
) -> ClaudeCodeResult:
    return ClaudeCodeResult(
        success=success,
        exit_code=0 if success else 1,
        pr_url=pr_url,
        branch=None,
        error_summary=error_summary,
        token_count=2000,
        cost_usd=0.25,
        run_log_path=Path("/tmp/run.md"),
        tail=tail or [],
    )


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------


class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_blocked_without_approval(self, tmp_path: Path) -> None:
        architect = make_architect(tmp_path)
        task = make_task()
        ctx = make_ctx(tmp_path)

        with patch("loomstack.agents.architect.is_approved", return_value=False):
            result = await architect.execute(task, ctx)

        assert isinstance(result, Blocked)
        assert "approval" in result.reason.lower()
        assert result.approval_path is not None

    @pytest.mark.asyncio
    async def test_proceeds_with_approval(self, tmp_path: Path) -> None:
        architect = make_architect(tmp_path)
        task = make_task()
        ctx = make_ctx(tmp_path)

        with (
            patch("loomstack.agents.architect.is_approved", return_value=True),
            patch("loomstack.agents.architect.create_branch", new_callable=AsyncMock),
            patch("loomstack.agents.architect.run_claude_code", new_callable=AsyncMock) as mock_run,
            patch("loomstack.agents.architect.commit_and_push", new_callable=AsyncMock),
            patch("loomstack.agents.architect.open_pr", new_callable=AsyncMock) as mock_pr,
        ):
            mock_run.return_value = make_claude_result(success=True)
            mock_pr.return_value = "https://github.com/org/repo/pull/10"
            result = await architect.execute(task, ctx)

        assert isinstance(result, Proposed)
        assert result.pr_url == "https://github.com/org/repo/pull/10"


# ---------------------------------------------------------------------------
# Execute — success
# ---------------------------------------------------------------------------


class TestExecuteSuccess:
    @pytest.mark.asyncio
    async def test_proposed_with_runner_pr(self, tmp_path: Path) -> None:
        architect = make_architect(tmp_path)
        task = make_task()
        ctx = make_ctx(tmp_path)

        with (
            patch("loomstack.agents.architect.is_approved", return_value=True),
            patch("loomstack.agents.architect.create_branch", new_callable=AsyncMock),
            patch("loomstack.agents.architect.run_claude_code", new_callable=AsyncMock) as mock_run,
            patch("loomstack.agents.architect.commit_and_push", new_callable=AsyncMock),
        ):
            mock_run.return_value = make_claude_result(
                success=True, pr_url="https://github.com/org/repo/pull/5"
            )
            result = await architect.execute(task, ctx)

        assert isinstance(result, Proposed)
        assert result.pr_url == "https://github.com/org/repo/pull/5"
        assert result.token_count == 2000


# ---------------------------------------------------------------------------
# Execute — failure
# ---------------------------------------------------------------------------


class TestExecuteFailure:
    @pytest.mark.asyncio
    async def test_failed_on_runner_error(self, tmp_path: Path) -> None:
        architect = make_architect(tmp_path)
        task = make_task()
        ctx = make_ctx(tmp_path)

        with (
            patch("loomstack.agents.architect.is_approved", return_value=True),
            patch("loomstack.agents.architect.create_branch", new_callable=AsyncMock),
            patch("loomstack.agents.architect.run_claude_code", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = make_claude_result(success=False, error_summary="syntax error")
            result = await architect.execute(task, ctx)

        assert isinstance(result, Failed)
        assert "syntax error" in result.error

    @pytest.mark.asyncio
    async def test_blocked_on_too_large(self, tmp_path: Path) -> None:
        """When runner output suggests decomposition, return Blocked."""
        architect = make_architect(tmp_path)
        task = make_task()
        ctx = make_ctx(tmp_path)

        with (
            patch("loomstack.agents.architect.is_approved", return_value=True),
            patch("loomstack.agents.architect.create_branch", new_callable=AsyncMock),
            patch("loomstack.agents.architect.run_claude_code", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = make_claude_result(
                success=False,
                error_summary="task is too complex",
                tail=["This task is too large. Please decompose into subtasks."],
            )
            result = await architect.execute(task, ctx)

        assert isinstance(result, Blocked)
        assert "decomposition" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_failed_on_branch_error(self, tmp_path: Path) -> None:
        architect = make_architect(tmp_path)
        task = make_task()
        ctx = make_ctx(tmp_path)

        with (
            patch("loomstack.agents.architect.is_approved", return_value=True),
            patch(
                "loomstack.agents.architect.create_branch", new_callable=AsyncMock
            ) as mock_branch,
        ):
            mock_branch.side_effect = GitError("git checkout -b", 128, "fatal error")
            result = await architect.execute(task, ctx)

        assert isinstance(result, Failed)

    @pytest.mark.asyncio
    async def test_continues_if_branch_exists(self, tmp_path: Path) -> None:
        architect = make_architect(tmp_path)
        task = make_task()
        ctx = make_ctx(tmp_path)

        with (
            patch("loomstack.agents.architect.is_approved", return_value=True),
            patch(
                "loomstack.agents.architect.create_branch", new_callable=AsyncMock
            ) as mock_branch,
            patch("loomstack.agents.architect.run_claude_code", new_callable=AsyncMock) as mock_run,
            patch("loomstack.agents.architect.commit_and_push", new_callable=AsyncMock),
            patch("loomstack.agents.architect.open_pr", new_callable=AsyncMock) as mock_pr,
        ):
            mock_branch.side_effect = GitError("git checkout -b", 128, "already exists")
            mock_run.return_value = make_claude_result(success=True)
            mock_pr.return_value = "url"
            result = await architect.execute(task, ctx)

        assert isinstance(result, Proposed)


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


class TestCanHandle:
    @pytest.mark.asyncio
    async def test_handles_any_task(self) -> None:
        architect = make_architect()
        assert await architect.can_handle(make_task()) is True

    @pytest.mark.asyncio
    async def test_handles_non_security_task(self) -> None:
        architect = make_architect()
        task = make_task(tags=[])
        assert await architect.can_handle(task) is True


# ---------------------------------------------------------------------------
# estimate_cost_usd
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_default_cost(self) -> None:
        architect = make_architect()
        assert architect.estimate_cost_usd(make_task()) == 0.25

    def test_custom_cost(self) -> None:
        architect = Architect(
            endpoint="http://localhost:8080/v1",
            model="claude-opus",
            repo_path=Path("/tmp/repo"),
            claude_md_path=Path("/tmp/repo/CLAUDE.md"),
            cost_per_task_usd=0.50,
        )
        assert architect.estimate_cost_usd(make_task()) == 0.50


# ---------------------------------------------------------------------------
# model_id
# ---------------------------------------------------------------------------


class TestModelId:
    def test_model_id_format(self) -> None:
        architect = make_architect()
        assert "claude-opus" in architect.model_id
