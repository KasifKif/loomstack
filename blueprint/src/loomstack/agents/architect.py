"""
Architect agent — highest-tier agent for complex, security, or breaking tasks.

Always gated by approval. Before executing, checks for an approval marker at
``.loomstack/approvals/<task-id>``. If not approved, returns Blocked.

Can also propose task decomposition when a task is too large.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from loomstack.agents.base import Blocked, Failed, Proposed
from loomstack.agents.claude_code_runner import run_claude_code
from loomstack.core.github import GitError, commit_and_push, create_branch, open_pr
from loomstack.core.state import is_approved

if TYPE_CHECKING:
    from pathlib import Path

    from loomstack.agents.base import AgentResult, TaskContext
    from loomstack.core.plan_parser import Task

log = structlog.get_logger(__name__)

# Cost is typically higher for architect tasks (cloud models).
_DEFAULT_COST_USD = 0.25


class Architect:
    """
    Highest-tier agent. Handles architecture decisions, security-tagged tasks,
    and breaking changes. Always requires approval before execution.

    Implements the ``BaseAgent`` protocol.
    """

    role: str = "architect"

    def __init__(
        self,
        endpoint: str,
        model: str,
        repo_path: Path,
        claude_md_path: Path,
        *,
        pr_base: str = "develop",
        cost_per_task_usd: float = _DEFAULT_COST_USD,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.model_id = f"{model} @ {endpoint}"
        self.repo_path = repo_path
        self.claude_md_path = claude_md_path
        self.pr_base = pr_base
        self._cost_per_task_usd = cost_per_task_usd

    # -- BaseAgent protocol --------------------------------------------------

    async def can_handle(self, task: Task) -> bool:
        """Architect can handle any task."""
        return True

    async def execute(self, task: Task, ctx: TaskContext) -> AgentResult:
        """
        Execute an architect-level task:
        1. Check approval gate
        2. Create feature branch
        3. Run claude-code with architect-level prompt
        4. On success: commit, push, open PR → Proposed
        5. On too-large: return Blocked with decomposition suggestion
        6. On failure: return Failed with context
        """
        # 1. Check approval
        if not is_approved(task.task_id, ctx.loomstack_dir):
            log.info(
                "architect.blocked_no_approval",
                task_id=task.task_id,
            )
            return Blocked(
                reason=f"Architect task {task.task_id} requires approval. "
                f"Create marker at {ctx.loomstack_dir}/approvals/{task.task_id}",
                approval_path=str(ctx.loomstack_dir / "approvals" / task.task_id),
            )

        branch = f"feat/{task.task_id.lower()}"

        log.info(
            "architect.start",
            task_id=task.task_id,
            branch=branch,
            model=self.model_id,
        )

        # 2. Create branch
        try:
            await create_branch(self.repo_path, branch)
        except GitError as exc:
            if "already exists" in exc.stderr:
                log.info("architect.branch_exists", branch=branch)
            else:
                return Failed(
                    error=f"Failed to create branch {branch}: {exc}",
                    retry_context={"stderr": exc.stderr},
                )

        # 3. Run claude-code
        result = await run_claude_code(
            endpoint=self.endpoint,
            model=self.model,
            repo_path=self.repo_path,
            task=task,
            claude_md_path=self.claude_md_path,
            run_log_path=ctx.run_log_path,
            timeout_s=task.timeout_s,
        )

        if not result.success:
            log.warning(
                "architect.failed",
                task_id=task.task_id,
                exit_code=result.exit_code,
                error=result.error_summary[:200],
            )
            # Check if the failure suggests decomposition
            tail_text = "\n".join(result.tail[-20:]).lower()
            if "too large" in tail_text or "decompose" in tail_text:
                return Blocked(
                    reason=f"Task {task.task_id} is too large and needs decomposition. "
                    f"Suggested breakdown in run log: {ctx.run_log_path}",
                )

            return Failed(
                error=result.error_summary,
                retry_context={
                    "exit_code": str(result.exit_code),
                    "tail": "\n".join(result.tail[-10:]),
                },
                token_count=result.token_count,
                cost_usd=result.cost_usd,
            )

        # 4. Commit and push
        try:
            await commit_and_push(
                self.repo_path,
                branch,
                f"feat({task.task_id}): {task.description[:60]}",
            )
        except GitError as exc:
            return Failed(
                error=f"Commit/push failed: {exc}",
                retry_context={"stderr": exc.stderr},
                token_count=result.token_count,
                cost_usd=result.cost_usd,
            )

        # 5. Open PR
        pr_url = result.pr_url
        if not pr_url:
            try:
                pr_url = await open_pr(
                    self.repo_path,
                    branch,
                    f"[ARCHITECT] {task.task_id}: {task.description[:50]}",
                    body=(
                        f"Architect-level PR for task {task.task_id}.\n\n"
                        f"{task.notes}\n\n"
                        f"**Requires human review before merge.**"
                    ),
                    base=self.pr_base,
                )
            except GitError as exc:
                return Failed(
                    error=f"PR creation failed: {exc}",
                    retry_context={"stderr": exc.stderr},
                    token_count=result.token_count,
                    cost_usd=result.cost_usd,
                )

        log.info(
            "architect.proposed",
            task_id=task.task_id,
            branch=branch,
            pr_url=pr_url,
        )
        return Proposed(
            branch=branch,
            pr_url=pr_url,
            token_count=result.token_count,
            cost_usd=result.cost_usd,
        )

    def estimate_cost_usd(self, task: Task) -> float:
        """Return the configured per-task cost estimate."""
        return self._cost_per_task_usd
