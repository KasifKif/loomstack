"""Tests for blueprint/src/loomstack/agents/base.py."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from loomstack.agents.base import (
    AgentResult,
    BaseAgent,
    Blocked,
    Failed,
    Proposed,
    TaskContext,
    is_terminal,
    result_cost_usd,
    result_token_count,
)
from loomstack.core.plan_parser import Task


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_task(**kwargs: object) -> Task:
    defaults: dict[str, object] = {
        "task_id": "MC-001",
        "description": "Test task",
        "role": "code_worker",
        "acceptance": {"ci": "passes"},
    }
    defaults.update(kwargs)
    return Task.model_validate(defaults)


def _make_ctx(tmp_path: Path) -> TaskContext:
    return TaskContext(
        repo_path=tmp_path,
        loomstack_dir=tmp_path / ".loomstack",
        claude_md_path=tmp_path / "CLAUDE.md",
        run_log_path=tmp_path / ".loomstack" / "runs" / "MC-001.md",
    )


# ---------------------------------------------------------------------------
# TaskContext
# ---------------------------------------------------------------------------


class TestTaskContext:
    def test_defaults(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        assert ctx.retry_count == 0
        assert ctx.prior_error is None
        assert ctx.prior_diff is None

    def test_frozen(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        with pytest.raises(Exception):
            ctx.retry_count = 1  # type: ignore[misc]

    def test_with_retry_context(self, tmp_path: Path) -> None:
        ctx = TaskContext(
            repo_path=tmp_path,
            loomstack_dir=tmp_path / ".loomstack",
            claude_md_path=tmp_path / "CLAUDE.md",
            run_log_path=tmp_path / ".loomstack" / "runs" / "MC-001.md",
            retry_count=2,
            prior_error="test failed",
            prior_diff="--- a/foo.py\n+++ b/foo.py\n",
        )
        assert ctx.retry_count == 2
        assert ctx.prior_error == "test failed"


# ---------------------------------------------------------------------------
# Result variants
# ---------------------------------------------------------------------------


class TestProposed:
    def test_basic(self) -> None:
        r = Proposed(branch="feat/mc-001", pr_url="https://github.com/o/r/pull/1")
        assert r.branch == "feat/mc-001"
        assert r.pr_url == "https://github.com/o/r/pull/1"
        assert r.token_count == 0
        assert r.cost_usd == 0.0

    def test_with_cost(self) -> None:
        r = Proposed(branch="b", pr_url="u", token_count=1500, cost_usd=0.003)
        assert r.token_count == 1500
        assert r.cost_usd == pytest.approx(0.003)

    def test_frozen(self) -> None:
        r = Proposed(branch="b", pr_url="u")
        with pytest.raises(Exception):
            r.branch = "other"  # type: ignore[misc]


class TestBlocked:
    def test_basic(self) -> None:
        r = Blocked(reason="waiting for architect approval")
        assert r.reason == "waiting for architect approval"
        assert r.approval_path is None

    def test_with_path(self) -> None:
        r = Blocked(reason="gate", approval_path=".loomstack/approvals/MC-001")
        assert r.approval_path == ".loomstack/approvals/MC-001"

    def test_frozen(self) -> None:
        r = Blocked(reason="x")
        with pytest.raises(Exception):
            r.reason = "y"  # type: ignore[misc]


class TestFailed:
    def test_basic(self) -> None:
        r = Failed(error="subprocess timed out")
        assert r.error == "subprocess timed out"
        assert r.retry_context == {}
        assert r.token_count == 0
        assert r.cost_usd == 0.0

    def test_with_context(self) -> None:
        r = Failed(
            error="tests failed",
            retry_context={"test_output": "AssertionError: ..."},
            token_count=800,
            cost_usd=0.001,
        )
        assert r.retry_context["test_output"] == "AssertionError: ..."

    def test_frozen(self) -> None:
        r = Failed(error="x")
        with pytest.raises(Exception):
            r.error = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_is_terminal_proposed(self) -> None:
        assert is_terminal(Proposed(branch="b", pr_url="u")) is True

    def test_is_terminal_blocked(self) -> None:
        assert is_terminal(Blocked(reason="x")) is True

    def test_is_terminal_failed(self) -> None:
        assert is_terminal(Failed(error="x")) is False

    def test_result_cost_proposed(self) -> None:
        r = Proposed(branch="b", pr_url="u", cost_usd=0.05)
        assert result_cost_usd(r) == pytest.approx(0.05)

    def test_result_cost_blocked(self) -> None:
        assert result_cost_usd(Blocked(reason="x")) == 0.0

    def test_result_cost_failed(self) -> None:
        r = Failed(error="x", cost_usd=0.002)
        assert result_cost_usd(r) == pytest.approx(0.002)

    def test_result_tokens_proposed(self) -> None:
        r = Proposed(branch="b", pr_url="u", token_count=2000)
        assert result_token_count(r) == 2000

    def test_result_tokens_blocked(self) -> None:
        assert result_token_count(Blocked(reason="x")) == 0

    def test_result_tokens_failed(self) -> None:
        r = Failed(error="x", token_count=500)
        assert result_token_count(r) == 500


# ---------------------------------------------------------------------------
# BaseAgent protocol conformance
# ---------------------------------------------------------------------------


class TestBaseAgentProtocol:
    def test_conforming_class_passes_isinstance(self) -> None:
        """A class implementing the protocol is recognised at runtime."""

        class StubAgent:
            role = "code_worker"
            model_id = "qwen3-coder-next @ gx10"

            async def can_handle(self, task: Task) -> bool:
                return task.role.value == self.role

            async def execute(self, task: Task, ctx: TaskContext) -> AgentResult:
                return Proposed(branch="feat/mc-001", pr_url="https://example.com/1")

            def estimate_cost_usd(self, task: Task) -> float:
                return 0.0

        assert isinstance(StubAgent(), BaseAgent)

    def test_missing_role_fails_isinstance(self) -> None:
        class BadAgent:
            model_id = "x"

            async def can_handle(self, task: Task) -> bool:
                return True

            async def execute(self, task: Task, ctx: TaskContext) -> AgentResult:
                return Blocked(reason="x")

            def estimate_cost_usd(self, task: Task) -> float:
                return 0.0

        assert not isinstance(BadAgent(), BaseAgent)

    @pytest.mark.asyncio
    async def test_stub_agent_execute(self, tmp_path: Path) -> None:
        task = _make_task()
        ctx = _make_ctx(tmp_path)

        execute_mock = AsyncMock(
            return_value=Proposed(branch="feat/mc-001", pr_url="https://gh.com/1")
        )

        class StubAgent:
            role = "code_worker"
            model_id = "stub"

            async def can_handle(self, t: Task) -> bool:
                return True

            async def execute(self, t: Task, c: TaskContext) -> AgentResult:
                return await execute_mock(t, c)

            def estimate_cost_usd(self, t: Task) -> float:
                return 0.0

        agent = StubAgent()
        result = await agent.execute(task, ctx)
        assert isinstance(result, Proposed)
        execute_mock.assert_awaited_once_with(task, ctx)
