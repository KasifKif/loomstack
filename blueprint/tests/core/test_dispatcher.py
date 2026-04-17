"""Tests for blueprint/src/loomstack/core/dispatcher.py."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 — used at runtime in pytest fixtures
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from loomstack.agents.base import Blocked, Failed, Proposed
from loomstack.agents.classifier import ClassificationResult
from loomstack.core.dispatcher import (
    Dispatcher,
    DispatchResult,
    _resolve_tier,
    _write_ledger_entry,
    _write_run_result,
)
from loomstack.core.plan_parser import AcceptanceBlock, Plan, Task
from loomstack.core.state import TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(
    task_id: str = "LS-001",
    description: str = "Add widget",
    depends_on: list[str] | None = None,
    tags: list[str] | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        description=description,
        role="code_worker",
        acceptance=AcceptanceBlock(tests_pass="unit"),
        depends_on=depends_on or [],
        tags=tags or [],
    )


def make_plan(tasks: list[Task] | None = None) -> Plan:
    return Plan(title="Test Plan", tasks=tasks or [make_task()])


def make_agent(
    role: str = "code_worker",
    can_handle: bool = True,
    result: Proposed | Failed | Blocked | None = None,
    cost: float = 0.0,
) -> MagicMock:
    agent = MagicMock()
    agent.role = role
    agent.model_id = "test-model @ test"
    agent.can_handle = AsyncMock(return_value=can_handle)
    agent.execute = AsyncMock(
        return_value=result
        or Proposed(branch="feat/ls-001", pr_url="https://github.com/org/repo/pull/1")
    )
    agent.estimate_cost_usd = MagicMock(return_value=cost)
    return agent


def make_classifier(tier: str = "code_worker", tags: frozenset[str] | None = None) -> MagicMock:
    c = MagicMock()
    c.role = "classifier"
    c.classify = AsyncMock(
        return_value=ClassificationResult(tier=tier, tags=tags or frozenset(), confidence=1.0)
    )
    return c


def make_budget(exceeded: bool = False) -> MagicMock:
    b = MagicMock()
    if exceeded:
        exc = MagicMock()
        exc.__str__ = lambda self: "budget exceeded"
        b.check = AsyncMock(return_value=exc)
    else:
        b.check = AsyncMock(return_value=None)
    b.charge = AsyncMock()
    return b


def make_dispatcher(
    tmp_path: Path,
    agents: dict[str, MagicMock] | None = None,
    budget: MagicMock | None = None,
    classifier: MagicMock | None = None,
) -> Dispatcher:
    return Dispatcher(
        repo_path=tmp_path,
        agents=agents or {"code_worker": make_agent()},
        budget=budget or make_budget(),
        classifier=classifier or make_classifier(),
        plan_path=tmp_path / "PLAN.md",
        loomstack_dir=tmp_path / ".loomstack",
    )


# ---------------------------------------------------------------------------
# run_once — dispatch pending tasks
# ---------------------------------------------------------------------------


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_dispatches_pending_task(self, tmp_path: Path) -> None:
        agent = make_agent()
        dispatcher = make_dispatcher(tmp_path, agents={"code_worker": agent})
        plan = make_plan()

        with (
            patch(
                "loomstack.core.dispatcher.parse_plan_file", new_callable=AsyncMock
            ) as mock_parse,
            patch("loomstack.core.dispatcher.derive_status", new_callable=AsyncMock) as mock_status,
        ):
            mock_parse.return_value = plan
            mock_status.return_value = TaskStatus.PENDING
            results = await dispatcher.run_once()

        assert len(results) == 1
        assert isinstance(results[0].result, Proposed)
        agent.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_done_tasks(self, tmp_path: Path) -> None:
        agent = make_agent()
        dispatcher = make_dispatcher(tmp_path, agents={"code_worker": agent})
        plan = make_plan()

        with (
            patch(
                "loomstack.core.dispatcher.parse_plan_file", new_callable=AsyncMock
            ) as mock_parse,
            patch("loomstack.core.dispatcher.derive_status", new_callable=AsyncMock) as mock_status,
        ):
            mock_parse.return_value = plan
            mock_status.return_value = TaskStatus.DONE
            results = await dispatcher.run_once()

        assert len(results) == 0
        agent.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_task_with_unmet_deps(self, tmp_path: Path) -> None:
        task_a = make_task(task_id="LS-001", description="First")
        task_b = make_task(task_id="LS-002", description="Second", depends_on=["LS-001"])
        plan = make_plan(tasks=[task_a, task_b])

        agent = make_agent()
        dispatcher = make_dispatcher(tmp_path, agents={"code_worker": agent})

        with (
            patch(
                "loomstack.core.dispatcher.parse_plan_file", new_callable=AsyncMock
            ) as mock_parse,
            patch("loomstack.core.dispatcher.derive_status", new_callable=AsyncMock) as mock_status,
        ):
            mock_parse.return_value = plan
            # Both PENDING — LS-002 deps not met since LS-001 not DONE
            mock_status.return_value = TaskStatus.PENDING
            results = await dispatcher.run_once()

        # Only LS-001 should be dispatched (LS-002 depends on LS-001 which isn't DONE)
        assert len(results) == 1
        assert results[0].task_id == "LS-001"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TestBudgetCheck:
    @pytest.mark.asyncio
    async def test_budget_exceeded_skips_task(self, tmp_path: Path) -> None:
        budget = make_budget(exceeded=True)
        dispatcher = make_dispatcher(tmp_path, budget=budget)
        plan = make_plan()

        with (
            patch(
                "loomstack.core.dispatcher.parse_plan_file", new_callable=AsyncMock
            ) as mock_parse,
            patch("loomstack.core.dispatcher.derive_status", new_callable=AsyncMock) as mock_status,
        ):
            mock_parse.return_value = plan
            mock_status.return_value = TaskStatus.PENDING
            results = await dispatcher.run_once()

        assert len(results) == 1
        assert isinstance(results[0].result, Failed)
        assert "budget" in results[0].result.error.lower()


# ---------------------------------------------------------------------------
# Architect approval gate
# ---------------------------------------------------------------------------


class TestArchitectApproval:
    @pytest.mark.asyncio
    async def test_architect_blocked_without_approval(self, tmp_path: Path) -> None:
        classifier = make_classifier(tier="architect")
        agent = make_agent(role="architect")
        dispatcher = make_dispatcher(
            tmp_path,
            agents={"architect": agent},
            classifier=classifier,
        )
        plan = make_plan()

        with (
            patch(
                "loomstack.core.dispatcher.parse_plan_file", new_callable=AsyncMock
            ) as mock_parse,
            patch("loomstack.core.dispatcher.derive_status", new_callable=AsyncMock) as mock_status,
            patch("loomstack.core.dispatcher.is_approved", return_value=False),
        ):
            mock_parse.return_value = plan
            mock_status.return_value = TaskStatus.PENDING
            results = await dispatcher.run_once()

        assert len(results) == 1
        assert isinstance(results[0].result, Blocked)
        agent.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_architect_proceeds_with_approval(self, tmp_path: Path) -> None:
        classifier = make_classifier(tier="architect")
        agent = make_agent(role="architect")
        dispatcher = make_dispatcher(
            tmp_path,
            agents={"architect": agent},
            classifier=classifier,
        )
        plan = make_plan()

        with (
            patch(
                "loomstack.core.dispatcher.parse_plan_file", new_callable=AsyncMock
            ) as mock_parse,
            patch("loomstack.core.dispatcher.derive_status", new_callable=AsyncMock) as mock_status,
            patch("loomstack.core.dispatcher.is_approved", return_value=True),
        ):
            mock_parse.return_value = plan
            mock_status.return_value = TaskStatus.PENDING
            results = await dispatcher.run_once()

        assert len(results) == 1
        assert isinstance(results[0].result, Proposed)
        agent.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Agent failure
# ---------------------------------------------------------------------------


class TestAgentFailure:
    @pytest.mark.asyncio
    async def test_failed_result_recorded(self, tmp_path: Path) -> None:
        failed = Failed(error="syntax error", token_count=500, cost_usd=0.01)
        agent = make_agent(result=failed)
        dispatcher = make_dispatcher(tmp_path, agents={"code_worker": agent})
        plan = make_plan()

        with (
            patch(
                "loomstack.core.dispatcher.parse_plan_file", new_callable=AsyncMock
            ) as mock_parse,
            patch("loomstack.core.dispatcher.derive_status", new_callable=AsyncMock) as mock_status,
        ):
            mock_parse.return_value = plan
            mock_status.return_value = TaskStatus.PENDING
            results = await dispatcher.run_once()

        assert len(results) == 1
        assert isinstance(results[0].result, Failed)
        assert results[0].result.error == "syntax error"


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


class TestWriteRunResult:
    @pytest.mark.asyncio
    async def test_writes_proposed_footer(self, tmp_path: Path) -> None:
        run_path = tmp_path / "runs" / "LS-001.md"
        result = Proposed(branch="feat/ls-001", pr_url="https://github.com/org/repo/pull/1")
        await _write_run_result(run_path, "LS-001", "code_worker", result)

        content = run_path.read_text()
        assert "status: done" in content
        assert "pr_url:" in content

    @pytest.mark.asyncio
    async def test_writes_failed_footer(self, tmp_path: Path) -> None:
        run_path = tmp_path / "runs" / "LS-001.md"
        result = Failed(error="compile error")
        await _write_run_result(run_path, "LS-001", "code_worker", result)

        content = run_path.read_text()
        assert "status: failed" in content
        assert "compile error" in content

    @pytest.mark.asyncio
    async def test_writes_blocked_footer(self, tmp_path: Path) -> None:
        run_path = tmp_path / "runs" / "LS-001.md"
        result = Blocked(reason="needs approval")
        await _write_run_result(run_path, "LS-001", "architect", result)

        content = run_path.read_text()
        assert "status: blocked" in content
        assert "needs approval" in content


class TestWriteLedgerEntry:
    @pytest.mark.asyncio
    async def test_writes_jsonl(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = Proposed(branch="feat/ls-001", pr_url="url", cost_usd=0.05, token_count=1000)
        await _write_ledger_entry(ledger_path, "LS-001", "code_worker", result)

        line = ledger_path.read_text().strip()
        entry = json.loads(line)
        assert entry["task_id"] == "LS-001"
        assert entry["status"] == "proposed"
        assert entry["cost_usd"] == 0.05


# ---------------------------------------------------------------------------
# DispatchResult
# ---------------------------------------------------------------------------


class TestDispatchResult:
    def test_fields(self) -> None:
        r = DispatchResult(
            task_id="LS-001",
            tier="code_worker",
            result=Proposed(branch="b", pr_url="u"),
        )
        assert r.task_id == "LS-001"
        assert r.tier == "code_worker"
        assert r.timestamp  # non-empty


# ---------------------------------------------------------------------------
# Escalation — _resolve_tier
# ---------------------------------------------------------------------------


class TestResolveTier:
    def test_no_retries_uses_classified(self) -> None:
        assert _resolve_tier("code_worker", 0, []) == "code_worker"

    def test_below_threshold_stays_at_tier(self) -> None:
        assert _resolve_tier("code_worker", 2, []) == "code_worker"

    def test_at_threshold_escalates(self) -> None:
        assert _resolve_tier("code_worker", 3, []) == "reviewer"

    def test_reviewer_escalates_to_architect(self) -> None:
        assert _resolve_tier("reviewer", 3, []) == "architect"

    def test_architect_stays_at_architect(self) -> None:
        assert _resolve_tier("architect", 5, []) == "architect"

    def test_security_tag_always_architect(self) -> None:
        assert _resolve_tier("code_worker", 0, ["security"]) == "architect"

    def test_breaking_change_tag_always_architect(self) -> None:
        assert _resolve_tier("code_worker", 0, ["breaking_change"]) == "architect"

    def test_unknown_tier_no_crash(self) -> None:
        assert _resolve_tier("custom_tier", 5, []) == "custom_tier"


# ---------------------------------------------------------------------------
# Escalation — dispatch integration
# ---------------------------------------------------------------------------


class TestEscalationDispatch:
    @pytest.mark.asyncio
    async def test_failed_task_retried(self, tmp_path: Path) -> None:
        """FAILED tasks are picked up for retry."""
        agent = make_agent()
        dispatcher = make_dispatcher(tmp_path, agents={"code_worker": agent})
        plan = make_plan()

        with (
            patch(
                "loomstack.core.dispatcher.parse_plan_file", new_callable=AsyncMock
            ) as mock_parse,
            patch("loomstack.core.dispatcher.derive_status", new_callable=AsyncMock) as mock_status,
        ):
            mock_parse.return_value = plan
            mock_status.return_value = TaskStatus.FAILED
            results = await dispatcher.run_once()

        assert len(results) == 1
        agent.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_escalation_changes_tier(self, tmp_path: Path) -> None:
        """After 3+ retries, task escalates from code_worker to reviewer."""
        reviewer_agent = make_agent(role="reviewer")
        code_agent = make_agent(role="code_worker")
        classifier = make_classifier(tier="code_worker")
        dispatcher = make_dispatcher(
            tmp_path,
            agents={"code_worker": code_agent, "reviewer": reviewer_agent},
            classifier=classifier,
        )
        plan = make_plan()

        # Simulate a run file with retry_count=3
        from loomstack.core.state import RunMeta

        with (
            patch(
                "loomstack.core.dispatcher.parse_plan_file", new_callable=AsyncMock
            ) as mock_parse,
            patch("loomstack.core.dispatcher.derive_status", new_callable=AsyncMock) as mock_status,
            patch(
                "loomstack.core.dispatcher.read_run_meta",
                return_value=RunMeta(retry_count=3, status=TaskStatus.FAILED),
            ),
        ):
            mock_parse.return_value = plan
            mock_status.return_value = TaskStatus.FAILED
            results = await dispatcher.run_once()

        assert len(results) == 1
        assert results[0].tier == "reviewer"
        reviewer_agent.execute.assert_called_once()
        code_agent.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_count_written_on_failure(self, tmp_path: Path) -> None:
        """Failed result footer includes incremented retry_count."""
        run_path = tmp_path / "runs" / "LS-001.md"
        result = Failed(error="compile error")
        await _write_run_result(run_path, "LS-001", "code_worker", result, retry_count=1)

        content = run_path.read_text()
        assert "retry_count: 2" in content
