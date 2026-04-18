"""Tests for blueprint/src/loomstack/agents/code_worker.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from loomstack.agents.aider_runner import AiderResult
from loomstack.agents.base import Failed, Proposed, TaskContext
from loomstack.agents.claude_code_runner import ClaudeCodeResult
from loomstack.agents.code_worker import CodeWorker
from loomstack.core.github import GitError
from loomstack.core.plan_parser import AcceptanceBlock, Task

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_task(
    task_id: str = "LS-001",
    description: str = "Add frobnicator module",
    tags: list[str] | None = None,
    notes: str = "Implement the frobnicator",
) -> Task:
    return Task(
        task_id=task_id,
        description=description,
        role="code_worker",
        acceptance=AcceptanceBlock(tests_pass="unit"),
        tags=tags or [],
        notes=notes,
    )


def make_ctx(tmp_path: Path | None = None) -> TaskContext:
    base = tmp_path or Path("/tmp/test-repo")
    return TaskContext(
        repo_path=base,
        loomstack_dir=base / ".loomstack",
        claude_md_path=base / "CLAUDE.md",
        run_log_path=base / ".loomstack" / "runs" / "LS-001.md",
    )


def make_worker(
    repo_path: Path | None = None,
    runner: str = "claude_code",
) -> CodeWorker:
    return CodeWorker(
        endpoint="http://localhost:8080/v1",
        model="qwen3-coder",
        repo_path=repo_path or Path("/tmp/test-repo"),
        claude_md_path=Path("/tmp/test-repo/CLAUDE.md"),
        runner=runner,  # type: ignore[arg-type]
    )


def make_claude_result(
    success: bool = True,
    pr_url: str | None = None,
    branch: str | None = None,
    error_summary: str = "",
    exit_code: int = 0,
) -> ClaudeCodeResult:
    return ClaudeCodeResult(
        success=success,
        exit_code=exit_code,
        pr_url=pr_url,
        branch=branch,
        error_summary=error_summary,
        token_count=1000,
        cost_usd=0.02,
        run_log_path=Path("/tmp/run.md"),
        tail=["line1", "line2"],
    )


def make_aider_result(
    success: bool = True,
    files_modified: list[str] | None = None,
    error_summary: str = "",
    exit_code: int = 0,
) -> AiderResult:
    return AiderResult(
        success=success,
        exit_code=exit_code,
        files_modified=files_modified or ["src/x.py"],
        error_summary=error_summary,
        token_count=500,
        cost_usd=0.0,
        run_log_path=Path("/tmp/run.md"),
        tail=["line1", "line2"],
    )


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


class TestCanHandle:
    @pytest.mark.asyncio
    async def test_accepts_normal_task(self) -> None:
        worker = make_worker()
        task = make_task()
        assert await worker.can_handle(task) is True

    @pytest.mark.asyncio
    async def test_rejects_security_tag(self) -> None:
        worker = make_worker()
        task = make_task(tags=["security"])
        assert await worker.can_handle(task) is False

    @pytest.mark.asyncio
    async def test_rejects_breaking_change_tag(self) -> None:
        worker = make_worker()
        task = make_task(tags=["breaking_change"])
        assert await worker.can_handle(task) is False

    @pytest.mark.asyncio
    async def test_accepts_other_tags(self) -> None:
        worker = make_worker()
        task = make_task(tags=["refactor", "tests"])
        assert await worker.can_handle(task) is True


# ---------------------------------------------------------------------------
# execute — success path
# ---------------------------------------------------------------------------


class TestExecuteSuccess:
    @pytest.mark.asyncio
    async def test_proposed_with_pr_from_runner(self) -> None:
        """When claude_code_runner finds a PR URL, use it directly."""
        worker = make_worker()
        task = make_task()
        ctx = make_ctx()

        with (
            patch(
                "loomstack.agents.code_worker.create_branch", new_callable=AsyncMock
            ) as mock_branch,
            patch(
                "loomstack.agents.code_worker.run_claude_code", new_callable=AsyncMock
            ) as mock_run,
            patch(
                "loomstack.agents.code_worker.commit_and_push", new_callable=AsyncMock
            ) as mock_push,
        ):
            mock_run.return_value = make_claude_result(
                success=True, pr_url="https://github.com/org/repo/pull/1"
            )
            result = await worker.execute(task, ctx)

        assert isinstance(result, Proposed)
        assert result.pr_url == "https://github.com/org/repo/pull/1"
        assert result.branch == "feat/ls-001"
        assert result.token_count == 1000
        mock_branch.assert_called_once()
        mock_push.assert_called_once()

    @pytest.mark.asyncio
    async def test_proposed_opens_pr_when_runner_has_none(self) -> None:
        """When runner doesn't find a PR URL, open one ourselves."""
        worker = make_worker()
        task = make_task()
        ctx = make_ctx()

        with (
            patch("loomstack.agents.code_worker.create_branch", new_callable=AsyncMock),
            patch(
                "loomstack.agents.code_worker.run_claude_code", new_callable=AsyncMock
            ) as mock_run,
            patch("loomstack.agents.code_worker.commit_and_push", new_callable=AsyncMock),
            patch("loomstack.agents.code_worker.open_pr", new_callable=AsyncMock) as mock_pr,
        ):
            mock_run.return_value = make_claude_result(success=True, pr_url=None)
            mock_pr.return_value = "https://github.com/org/repo/pull/99"
            result = await worker.execute(task, ctx)

        assert isinstance(result, Proposed)
        assert result.pr_url == "https://github.com/org/repo/pull/99"
        mock_pr.assert_called_once()


# ---------------------------------------------------------------------------
# execute — failure paths
# ---------------------------------------------------------------------------


class TestExecuteFailure:
    @pytest.mark.asyncio
    async def test_failed_on_runner_failure(self) -> None:
        worker = make_worker()
        task = make_task()
        ctx = make_ctx()

        with (
            patch("loomstack.agents.code_worker.create_branch", new_callable=AsyncMock),
            patch(
                "loomstack.agents.code_worker.run_claude_code", new_callable=AsyncMock
            ) as mock_run,
        ):
            mock_run.return_value = make_claude_result(
                success=False, error_summary="syntax error", exit_code=1
            )
            result = await worker.execute(task, ctx)

        assert isinstance(result, Failed)
        assert "syntax error" in result.error
        assert result.token_count == 1000

    @pytest.mark.asyncio
    async def test_failed_on_branch_creation_error(self) -> None:
        worker = make_worker()
        task = make_task()
        ctx = make_ctx()

        with patch(
            "loomstack.agents.code_worker.create_branch", new_callable=AsyncMock
        ) as mock_branch:
            mock_branch.side_effect = GitError("git checkout -b", 128, "fatal error")
            result = await worker.execute(task, ctx)

        assert isinstance(result, Failed)
        assert "branch" in result.error.lower()

    @pytest.mark.asyncio
    async def test_continues_if_branch_already_exists(self) -> None:
        """If the branch exists, we keep going (idempotent)."""
        worker = make_worker()
        task = make_task()
        ctx = make_ctx()

        with (
            patch(
                "loomstack.agents.code_worker.create_branch", new_callable=AsyncMock
            ) as mock_branch,
            patch(
                "loomstack.agents.code_worker.run_claude_code", new_callable=AsyncMock
            ) as mock_run,
            patch("loomstack.agents.code_worker.commit_and_push", new_callable=AsyncMock),
            patch("loomstack.agents.code_worker.open_pr", new_callable=AsyncMock) as mock_pr,
        ):
            mock_branch.side_effect = GitError("git checkout -b", 128, "already exists")
            mock_run.return_value = make_claude_result(success=True)
            mock_pr.return_value = "https://github.com/org/repo/pull/5"
            result = await worker.execute(task, ctx)

        assert isinstance(result, Proposed)

    @pytest.mark.asyncio
    async def test_failed_on_push_error(self) -> None:
        worker = make_worker()
        task = make_task()
        ctx = make_ctx()

        with (
            patch("loomstack.agents.code_worker.create_branch", new_callable=AsyncMock),
            patch(
                "loomstack.agents.code_worker.run_claude_code", new_callable=AsyncMock
            ) as mock_run,
            patch(
                "loomstack.agents.code_worker.commit_and_push", new_callable=AsyncMock
            ) as mock_push,
        ):
            mock_run.return_value = make_claude_result(success=True)
            mock_push.side_effect = GitError("git push", 1, "rejected")
            result = await worker.execute(task, ctx)

        assert isinstance(result, Failed)
        assert "push" in result.error.lower() or "commit" in result.error.lower()

    @pytest.mark.asyncio
    async def test_failed_on_pr_creation_error(self) -> None:
        worker = make_worker()
        task = make_task()
        ctx = make_ctx()

        with (
            patch("loomstack.agents.code_worker.create_branch", new_callable=AsyncMock),
            patch(
                "loomstack.agents.code_worker.run_claude_code", new_callable=AsyncMock
            ) as mock_run,
            patch("loomstack.agents.code_worker.commit_and_push", new_callable=AsyncMock),
            patch("loomstack.agents.code_worker.open_pr", new_callable=AsyncMock) as mock_pr,
        ):
            mock_run.return_value = make_claude_result(success=True, pr_url=None)
            mock_pr.side_effect = GitError("gh pr create", 1, "no remote")
            result = await worker.execute(task, ctx)

        assert isinstance(result, Failed)
        assert "pr" in result.error.lower()


# ---------------------------------------------------------------------------
# estimate_cost_usd
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_default_cost_is_zero(self) -> None:
        worker = make_worker()
        task = make_task()
        assert worker.estimate_cost_usd(task) == 0.0

    def test_custom_cost(self) -> None:
        worker = CodeWorker(
            endpoint="http://localhost:8080/v1",
            model="claude-sonnet",
            repo_path=Path("/tmp/repo"),
            claude_md_path=Path("/tmp/repo/CLAUDE.md"),
            cost_per_task_usd=0.25,
        )
        task = make_task()
        assert worker.estimate_cost_usd(task) == 0.25


# ---------------------------------------------------------------------------
# model_id
# ---------------------------------------------------------------------------


class TestModelId:
    def test_model_id_format(self) -> None:
        worker = make_worker()
        assert "qwen3-coder" in worker.model_id
        assert "localhost" in worker.model_id

    def test_model_id_includes_runner(self) -> None:
        assert "claude_code" in make_worker().model_id
        assert "aider" in make_worker(runner="aider").model_id


# ---------------------------------------------------------------------------
# execute — aider runner path
# ---------------------------------------------------------------------------


class TestExecuteWithAider:
    @pytest.mark.asyncio
    async def test_calls_aider_not_claude(self) -> None:
        worker = make_worker(runner="aider")
        task = make_task()
        ctx = make_ctx()

        with (
            patch("loomstack.agents.code_worker.create_branch", new_callable=AsyncMock),
            patch("loomstack.agents.code_worker.run_aider", new_callable=AsyncMock) as mock_aider,
            patch(
                "loomstack.agents.code_worker.run_claude_code", new_callable=AsyncMock
            ) as mock_claude,
            patch("loomstack.agents.code_worker.commit_and_push", new_callable=AsyncMock),
            patch("loomstack.agents.code_worker.open_pr", new_callable=AsyncMock) as mock_pr,
        ):
            mock_aider.return_value = make_aider_result(success=True)
            mock_pr.return_value = "https://github.com/org/repo/pull/7"
            result = await worker.execute(task, ctx)

        assert isinstance(result, Proposed)
        mock_aider.assert_called_once()
        mock_claude.assert_not_called()
        # aider never reports a PR URL — open_pr must run
        mock_pr.assert_called_once()

    @pytest.mark.asyncio
    async def test_proposed_uses_token_and_cost_from_aider(self) -> None:
        worker = make_worker(runner="aider")
        task = make_task()
        ctx = make_ctx()

        with (
            patch("loomstack.agents.code_worker.create_branch", new_callable=AsyncMock),
            patch("loomstack.agents.code_worker.run_aider", new_callable=AsyncMock) as mock_aider,
            patch("loomstack.agents.code_worker.commit_and_push", new_callable=AsyncMock),
            patch("loomstack.agents.code_worker.open_pr", new_callable=AsyncMock) as mock_pr,
        ):
            mock_aider.return_value = make_aider_result(success=True)
            mock_pr.return_value = "https://github.com/org/repo/pull/7"
            result = await worker.execute(task, ctx)

        assert isinstance(result, Proposed)
        assert result.token_count == 500
        assert result.cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_aider_failure_returns_failed(self) -> None:
        worker = make_worker(runner="aider")
        task = make_task()
        ctx = make_ctx()

        with (
            patch("loomstack.agents.code_worker.create_branch", new_callable=AsyncMock),
            patch("loomstack.agents.code_worker.run_aider", new_callable=AsyncMock) as mock_aider,
        ):
            mock_aider.return_value = make_aider_result(
                success=False,
                files_modified=[],
                error_summary="No changes made to any files.",
                exit_code=0,
            )
            result = await worker.execute(task, ctx)

        assert isinstance(result, Failed)
        assert "no changes" in result.error.lower()

    @pytest.mark.asyncio
    async def test_aider_push_failure_returns_failed_with_aider_cost(self) -> None:
        """Cost from the runner is preserved when a downstream step fails."""
        worker = make_worker(runner="aider")
        task = make_task()
        ctx = make_ctx()

        with (
            patch("loomstack.agents.code_worker.create_branch", new_callable=AsyncMock),
            patch("loomstack.agents.code_worker.run_aider", new_callable=AsyncMock) as mock_aider,
            patch(
                "loomstack.agents.code_worker.commit_and_push", new_callable=AsyncMock
            ) as mock_push,
        ):
            mock_aider.return_value = make_aider_result(success=True)
            mock_push.side_effect = GitError("git push", 1, "rejected")
            result = await worker.execute(task, ctx)

        assert isinstance(result, Failed)
        assert result.token_count == 500


class TestRunnerDefault:
    def test_default_is_claude_code(self) -> None:
        worker = make_worker()
        assert worker.runner == "claude_code"

    def test_explicit_aider(self) -> None:
        worker = make_worker(runner="aider")
        assert worker.runner == "aider"
