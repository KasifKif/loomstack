"""Tests for blueprint/src/loomstack/agents/reviewer.py."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, patch

import pytest

from loomstack.agents.base import Failed, Proposed, TaskContext
from loomstack.agents.claude_code_runner import ClaudeCodeResult
from loomstack.agents.reviewer import Reviewer, _make_review_task
from loomstack.core.plan_parser import AcceptanceBlock, Task
from loomstack.core.state import RunMeta

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(
    task_id: str = "LS-001",
    description: str = "Add widget module",
) -> Task:
    return Task(
        task_id=task_id,
        description=description,
        role="reviewer",
        acceptance=AcceptanceBlock(tests_pass="unit"),
    )


def make_ctx(
    tmp_path: Path | None = None,
    prior_diff: str | None = None,
    prior_error: str | None = None,
) -> TaskContext:
    base = tmp_path or Path("/tmp/test-repo")
    return TaskContext(
        repo_path=base,
        loomstack_dir=base / ".loomstack",
        claude_md_path=base / "CLAUDE.md",
        run_log_path=base / ".loomstack" / "runs" / "LS-001.md",
        prior_diff=prior_diff,
        prior_error=prior_error,
    )


def make_reviewer(repo_path: Path | None = None) -> Reviewer:
    return Reviewer(
        endpoint="http://localhost:8080/v1",
        model="qwen3-coder",
        repo_path=repo_path or Path("/tmp/test-repo"),
        claude_md_path=Path("/tmp/test-repo/CLAUDE.md"),
    )


def make_claude_result(
    success: bool = True,
    tail: list[str] | None = None,
    pr_url: str | None = None,
) -> ClaudeCodeResult:
    return ClaudeCodeResult(
        success=success,
        exit_code=0 if success else 1,
        pr_url=pr_url,
        branch=None,
        error_summary="" if success else "review failed",
        token_count=500,
        cost_usd=0.01,
        run_log_path=Path("/tmp/run.md"),
        tail=tail or [],
    )


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


class TestCanHandle:
    @pytest.mark.asyncio
    async def test_can_handle_any_task(self) -> None:
        reviewer = make_reviewer()
        task = make_task()
        assert await reviewer.can_handle(task) is True

    @pytest.mark.asyncio
    async def test_can_handle_security_task(self) -> None:
        reviewer = make_reviewer()
        task = Task(
            task_id="LS-002",
            description="Fix auth",
            role="reviewer",
            acceptance=AcceptanceBlock(tests_pass="unit"),
            tags=["security"],
        )
        assert await reviewer.can_handle(task) is True


# ---------------------------------------------------------------------------
# execute — review passed
# ---------------------------------------------------------------------------


class TestReviewPassed:
    @pytest.mark.asyncio
    async def test_returns_proposed_when_approved(self) -> None:
        reviewer = make_reviewer()
        task = make_task()
        ctx = make_ctx(prior_diff="diff --git a/foo.py b/foo.py\n+new line")

        with patch("loomstack.agents.reviewer.run_claude_code", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = make_claude_result(
                success=True,
                tail=["All looks good.", "REVIEW PASSED"],
            )
            result = await reviewer.execute(task, ctx)

        assert isinstance(result, Proposed)
        assert result.token_count == 500


# ---------------------------------------------------------------------------
# execute — review found issues
# ---------------------------------------------------------------------------


class TestReviewIssues:
    @pytest.mark.asyncio
    async def test_returns_failed_when_issues_found(self) -> None:
        reviewer = make_reviewer()
        task = make_task()
        ctx = make_ctx(prior_diff="diff content")

        with patch("loomstack.agents.reviewer.run_claude_code", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = make_claude_result(
                success=True,
                tail=["Issue: missing error handling in foo.py:42"],
            )
            result = await reviewer.execute(task, ctx)

        assert isinstance(result, Failed)
        assert "missing error handling" in result.error


# ---------------------------------------------------------------------------
# execute — runner failure
# ---------------------------------------------------------------------------


class TestRunnerFailure:
    @pytest.mark.asyncio
    async def test_returns_failed_on_runner_error(self) -> None:
        reviewer = make_reviewer()
        task = make_task()
        ctx = make_ctx(prior_diff="diff content")

        with patch("loomstack.agents.reviewer.run_claude_code", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = make_claude_result(success=False)
            result = await reviewer.execute(task, ctx)

        assert isinstance(result, Failed)
        assert "review failed" in result.error


# ---------------------------------------------------------------------------
# execute — no diff available
# ---------------------------------------------------------------------------


class TestNoDiff:
    @pytest.mark.asyncio
    async def test_returns_failed_when_no_diff(self) -> None:
        reviewer = make_reviewer()
        task = make_task()
        ctx = make_ctx(prior_diff=None)

        with patch(
            "loomstack.core.state.read_run_meta",
            return_value=RunMeta(pr_url=None),
        ):
            result = await reviewer.execute(task, ctx)

        assert isinstance(result, Failed)
        assert "no diff" in result.error.lower()


# ---------------------------------------------------------------------------
# _make_review_task
# ---------------------------------------------------------------------------


class TestMakeReviewTask:
    def test_copies_task_with_review_prompt(self) -> None:
        task = make_task(description="Add widget")
        review_task = _make_review_task(task, "diff content", "prior error")
        assert "Add widget" in review_task.notes
        assert "diff content" in review_task.notes
        assert "prior error" in review_task.notes
        # Original task unchanged
        assert task.notes == ""

    def test_caps_diff_size(self) -> None:
        task = make_task()
        long_diff = "x" * 10000
        review_task = _make_review_task(task, long_diff)
        assert len(review_task.notes) < 10000


# ---------------------------------------------------------------------------
# estimate_cost_usd
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_default_cost_zero(self) -> None:
        reviewer = make_reviewer()
        assert reviewer.estimate_cost_usd(make_task()) == 0.0

    def test_custom_cost(self) -> None:
        reviewer = Reviewer(
            endpoint="http://localhost:8080/v1",
            model="claude-sonnet",
            repo_path=Path("/tmp/repo"),
            claude_md_path=Path("/tmp/repo/CLAUDE.md"),
            cost_per_review_usd=0.10,
        )
        assert reviewer.estimate_cost_usd(make_task()) == 0.10
