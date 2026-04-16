"""
Code Worker agent — the default tier for task execution.

Wraps ``claude_code_runner`` to execute a task in a feature branch, then
opens a PR via ``core/github``. Handles success → Proposed, failure → Failed.

Does NOT handle tasks tagged ``security`` or ``breaking_change`` (those
escalate to the Architect tier).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from loomstack.agents.base import Failed, Proposed
from loomstack.agents.claude_code_runner import run_claude_code
from loomstack.core.github import GitError, commit_and_push, create_branch, open_pr

if TYPE_CHECKING:
    from pathlib import Path

    from loomstack.agents.base import AgentResult, TaskContext
    from loomstack.core.plan_parser import Task

log = structlog.get_logger(__name__)

# Tags that should be handled by the architect tier, not the code worker.
_ESCALATION_TAGS = frozenset({"security", "breaking_change"})

# Rough cost estimate per task for a local Qwen model (essentially free).
_DEFAULT_COST_USD = 0.0

# For cloud-backed models, estimate based on average tokens per task.
_CLOUD_COST_PER_TASK_USD = 0.15


class CodeWorker:
    """
    Default agent tier. Executes tasks by spawning claude-code in a feature
    branch, then committing and opening a PR.

    Implements the ``BaseAgent`` protocol.
    """

    role: str = "code_worker"

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
        """True unless the task is tagged for architect-only handling."""
        return not bool(set(task.tags) & _ESCALATION_TAGS)

    async def execute(self, task: Task, ctx: TaskContext) -> AgentResult:
        """
        Run the task end-to-end:
        1. Create feature branch ``feat/<task_id>``
        2. Run ``claude_code_runner`` with the task
        3. On success: commit, push, open PR → ``Proposed``
        4. On failure: return ``Failed`` with retry context
        """
        branch = f"feat/{task.task_id.lower()}"

        log.info(
            "code_worker.start",
            task_id=task.task_id,
            branch=branch,
            model=self.model_id,
        )

        # 1. Create branch
        try:
            await create_branch(self.repo_path, branch)
        except GitError as exc:
            if "already exists" in exc.stderr:
                log.info("code_worker.branch_exists", branch=branch)
            else:
                return Failed(
                    error=f"Failed to create branch {branch}: {exc}",
                    retry_context={"stderr": exc.stderr},
                )

        # 2. Run claude-code
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
                "code_worker.failed",
                task_id=task.task_id,
                exit_code=result.exit_code,
                error=result.error_summary[:200],
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

        # 3. Commit and push
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

        # 4. Open PR
        pr_url = result.pr_url
        if not pr_url:
            try:
                pr_url = await open_pr(
                    self.repo_path,
                    branch,
                    f"{task.task_id}: {task.description[:60]}",
                    body=f"Automated PR for task {task.task_id}.\n\n{task.notes}",
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
            "code_worker.proposed",
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
