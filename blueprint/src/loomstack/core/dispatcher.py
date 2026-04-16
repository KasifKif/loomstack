"""
Dispatcher — the orchestration heart of Loomstack.

Reads PLAN.md, derives task status, classifies pending tasks, checks budget,
and dispatches them to the appropriate agent tier. Writes results to run files
and charges the budget ledger.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiofiles
import structlog

from loomstack.agents.base import Blocked, Failed, Proposed, result_cost_usd, result_token_count
from loomstack.core.plan_parser import parse_plan_file
from loomstack.core.state import TaskStatus, derive_status, is_approved, read_run_meta

if TYPE_CHECKING:
    from pathlib import Path

    from loomstack.agents.base import AgentResult, BaseAgent
    from loomstack.agents.classifier import Classifier
    from loomstack.core.budget import Budget
    from loomstack.core.plan_parser import Task

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Escalation ladder
# ---------------------------------------------------------------------------

# Tier promotion order. When retries exhaust, move to the next tier.
_TIER_LADDER: list[str] = ["code_worker", "reviewer", "architect"]

# Retry threshold before escalating to the next tier.
_ESCALATION_RETRY_THRESHOLD = 3

# Tags that always escalate directly to architect, regardless of retry count.
_ALWAYS_ARCHITECT_TAGS = frozenset({"security", "breaking_change"})


def _resolve_tier(
    classified_tier: str,
    retry_count: int,
    task_tags: list[str],
) -> str:
    """
    Determine the effective tier for a task based on escalation rules.

    - Tasks tagged security/breaking_change → always architect.
    - retry_count < threshold → use classified tier.
    - retry_count >= threshold → promote to next tier in the ladder.
    """
    if _ALWAYS_ARCHITECT_TAGS & set(task_tags):
        return "architect"

    if retry_count < _ESCALATION_RETRY_THRESHOLD:
        return classified_tier

    # Find position in ladder and promote
    try:
        idx = _TIER_LADDER.index(classified_tier)
    except ValueError:
        return classified_tier

    next_idx = min(idx + 1, len(_TIER_LADDER) - 1)
    return _TIER_LADDER[next_idx]


# ---------------------------------------------------------------------------
# Dispatch result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchResult:
    """Record of one task dispatch attempt."""

    task_id: str
    tier: str
    result: AgentResult
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# ---------------------------------------------------------------------------
# Run-file I/O
# ---------------------------------------------------------------------------


async def _write_run_result(
    run_log_path: Path,
    task_id: str,
    tier: str,
    result: AgentResult,
    *,
    retry_count: int = 0,
) -> None:
    """Append a result footer to the run file."""
    run_log_path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(result, Proposed):
        status = "done"
        extra = f"pr_url: {result.pr_url}\nbranch: {result.branch}\n"
    elif isinstance(result, Blocked):
        status = "blocked"
        extra = f"reason: {result.reason}\n"
    else:
        status = "failed"
        extra = f"error: {result.error[:200]}\nretry_count: {retry_count + 1}\n"

    footer = (
        f"\n---\nstatus: {status}\ntier: {tier}\n"
        f"token_count: {result_token_count(result)}\n"
        f"cost_usd: {result_cost_usd(result)}\n"
        f"{extra}---\n"
    )

    async with aiofiles.open(run_log_path, "a", encoding="utf-8") as fh:
        await fh.write(footer)


async def _write_ledger_entry(
    ledger_path: Path,
    task_id: str,
    tier: str,
    result: AgentResult,
) -> None:
    """Append a JSON line to the ledger."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    status = (
        "proposed"
        if isinstance(result, Proposed)
        else ("blocked" if isinstance(result, Blocked) else "failed")
    )
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "tier": tier,
        "status": status,
        "cost_usd": result_cost_usd(result),
        "token_count": result_token_count(result),
    }

    async with aiofiles.open(ledger_path, "a", encoding="utf-8") as fh:
        await fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class Dispatcher:
    """
    Single dispatch cycle orchestrator.

    Reads PLAN.md → derives status → classifies → budget-checks → dispatches.
    """

    def __init__(
        self,
        repo_path: Path,
        agents: dict[str, BaseAgent],
        budget: Budget,
        classifier: Classifier,
        *,
        plan_path: Path | None = None,
        loomstack_dir: Path | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.agents = agents
        self.budget = budget
        self.classifier = classifier
        self.plan_path = plan_path or repo_path / "PLAN.md"
        self.loomstack_dir = loomstack_dir or repo_path / ".loomstack"
        self._ledger_path = self.loomstack_dir / "ledger.jsonl"

    def _run_log_path(self, task_id: str) -> Path:
        return self.loomstack_dir / "runs" / f"{task_id}.md"

    async def run_once(self) -> list[DispatchResult]:
        """
        Execute a single dispatch cycle.

        1. Parse PLAN.md
        2. Derive status for every task
        3. Find PENDING tasks with all deps DONE
        4. Classify → budget check → execute (one at a time)
        5. Write run file + ledger entry
        """
        plan = await parse_plan_file(self.plan_path)

        # Derive statuses
        statuses: dict[str, TaskStatus] = {}
        for task in plan.tasks:
            statuses[task.task_id] = await derive_status(
                task.task_id, self.repo_path, self.loomstack_dir
            )

        done_ids = {tid for tid, s in statuses.items() if s == TaskStatus.DONE}
        ready = plan.ready_tasks(done_ids)

        # Filter to PENDING or FAILED (retryable) tasks
        dispatchable = [
            t for t in ready if statuses.get(t.task_id) in (TaskStatus.PENDING, TaskStatus.FAILED)
        ]

        if not dispatchable:
            log.info("dispatcher.no_pending_tasks", total=len(plan.tasks), done=len(done_ids))
            return []

        pending = dispatchable

        results: list[DispatchResult] = []

        for task in pending:
            dr = await self._dispatch_one(task)
            if dr is not None:
                results.append(dr)

        return results

    async def _dispatch_one(self, task: Task) -> DispatchResult | None:
        """Classify, budget-check, escalate if needed, and execute a single task."""
        classification = await self.classifier.classify(task)

        # Read run metadata for retry/escalation decisions
        run_meta = read_run_meta(self._run_log_path(task.task_id))
        tier = _resolve_tier(classification.tier, run_meta.retry_count, task.tags)

        if tier != classification.tier:
            log.info(
                "dispatcher.escalated",
                task_id=task.task_id,
                from_tier=classification.tier,
                to_tier=tier,
                retry_count=run_meta.retry_count,
            )

        # Architect tasks require approval
        if tier == "architect" and not is_approved(task.task_id, self.loomstack_dir):
            log.info(
                "dispatcher.awaiting_approval",
                task_id=task.task_id,
                tier=tier,
            )
            blocked = Blocked(
                reason=f"Architect task {task.task_id} awaiting approval",
                approval_path=str(self.loomstack_dir / "approvals" / task.task_id),
            )
            await _write_run_result(self._run_log_path(task.task_id), task.task_id, tier, blocked)
            return DispatchResult(task_id=task.task_id, tier=tier, result=blocked)

        # Find agent
        agent = self.agents.get(tier)
        if agent is None:
            log.warning("dispatcher.no_agent", task_id=task.task_id, tier=tier)
            return None

        # Budget check
        estimate = agent.estimate_cost_usd(task)
        budget_result = await self.budget.check(tier, estimate, task.task_id)
        if budget_result is not None:
            log.warning(
                "dispatcher.budget_exceeded",
                task_id=task.task_id,
                tier=tier,
                estimate=estimate,
            )
            failed = Failed(
                error=f"Budget exceeded for tier {tier}: {str(budget_result)}",
                retry_context={"budget_reason": str(budget_result)},
            )
            await _write_run_result(self._run_log_path(task.task_id), task.task_id, tier, failed)
            return DispatchResult(task_id=task.task_id, tier=tier, result=failed)

        # Can the agent handle it?
        if not await agent.can_handle(task):
            log.warning(
                "dispatcher.agent_cannot_handle",
                task_id=task.task_id,
                tier=tier,
            )
            return None

        # Build context with escalation-aware retry expansion
        from loomstack.agents.base import TaskContext

        ctx = TaskContext(
            repo_path=self.repo_path,
            loomstack_dir=self.loomstack_dir,
            claude_md_path=self.repo_path / "CLAUDE.md",
            run_log_path=self._run_log_path(task.task_id),
            retry_count=run_meta.retry_count,
            prior_error=run_meta.last_error if run_meta.retry_count >= 1 else None,
            prior_diff=run_meta.last_diff if run_meta.retry_count >= 2 else None,
        )

        log.info(
            "dispatcher.executing",
            task_id=task.task_id,
            tier=tier,
            retry_count=ctx.retry_count,
        )

        # Execute
        result = await agent.execute(task, ctx)

        # Charge budget
        cost = result_cost_usd(result)
        if cost > 0:
            await self.budget.charge(tier, cost, task.task_id)

        # Write result (include retry_count so next cycle can read it back)
        await _write_run_result(
            self._run_log_path(task.task_id),
            task.task_id,
            tier,
            result,
            retry_count=run_meta.retry_count,
        )
        await _write_ledger_entry(self._ledger_path, task.task_id, tier, result)

        log.info(
            "dispatcher.result",
            task_id=task.task_id,
            tier=tier,
            result_type=type(result).__name__,
            cost_usd=cost,
        )

        return DispatchResult(task_id=task.task_id, tier=tier, result=result)

    async def run_loop(self, interval_s: int = 30) -> None:
        """
        Repeatedly call run_once with sleep between cycles.

        Catches and logs exceptions — never crashes the loop.
        Intended to be run as an asyncio task; cancel to stop.
        """
        log.info("dispatcher.loop_start", interval_s=interval_s)
        while True:
            try:
                results = await self.run_once()
                if results:
                    log.info(
                        "dispatcher.cycle_done",
                        dispatched=len(results),
                        tasks=[r.task_id for r in results],
                    )
            except Exception:
                log.exception("dispatcher.cycle_error")
            await asyncio.sleep(interval_s)
