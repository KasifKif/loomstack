"""
Reviewer agent — reviews PRs by reading diffs and running claude-code.

The reviewer reads a PR diff and asks claude-code to check for correctness,
test coverage, style violations, and security concerns. Returns Proposed
(approved) or Failed (with review comments as error context).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from loomstack.agents.base import Failed, Proposed
from loomstack.agents.claude_code_runner import run_claude_code
from loomstack.core.github import GitError, get_pr_diff

if TYPE_CHECKING:
    from pathlib import Path

    from loomstack.agents.base import AgentResult, TaskContext
    from loomstack.core.plan_parser import Task

log = structlog.get_logger(__name__)

# Default cost estimate for a review task (lower than code generation).
_DEFAULT_COST_USD = 0.0
_CLOUD_COST_PER_REVIEW_USD = 0.08

# Prompt template for the reviewer. Injected with the diff.
_REVIEW_PROMPT_TEMPLATE = """Review this pull request diff for the task: {description}

Focus on:
1. Correctness: Does the code do what the task asks?
2. Test coverage: Are there tests for the new/changed code?
3. Style: Does it follow the project conventions?
4. Security: Any obvious vulnerabilities?

PR Diff:
```
{diff}
```

Prior error context (if any):
{prior_error}

If the code looks good, say "REVIEW PASSED". If there are issues,
describe each one clearly with file and line references.
"""


class Reviewer:
    """
    Review agent tier. Reads a PR diff and runs claude-code with a
    review-focused prompt.

    Implements the ``BaseAgent`` protocol.
    """

    role: str = "reviewer"

    def __init__(
        self,
        endpoint: str,
        model: str,
        repo_path: Path,
        claude_md_path: Path,
        *,
        cost_per_review_usd: float = _DEFAULT_COST_USD,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.model_id = f"{model} @ {endpoint}"
        self.repo_path = repo_path
        self.claude_md_path = claude_md_path
        self._cost_per_review_usd = cost_per_review_usd

    # -- BaseAgent protocol --------------------------------------------------

    async def can_handle(self, task: Task) -> bool:
        """Reviewer can handle any task (review is always applicable)."""
        return True

    async def execute(self, task: Task, ctx: TaskContext) -> AgentResult:
        """
        Review a task's PR:
        1. Get the PR diff (from prior run metadata or task context)
        2. Run claude-code with a review prompt
        3. Parse for approval or issues
        """
        log.info(
            "reviewer.start",
            task_id=task.task_id,
            model=self.model_id,
        )

        # Get diff from prior context or try to fetch from a PR
        diff = ctx.prior_diff or ""
        if not diff:
            diff = await self._try_fetch_diff(task, ctx)

        if not diff:
            return Failed(
                error=f"No diff available for review of {task.task_id}",
                retry_context={"reason": "no_diff"},
            )

        # Build review prompt
        review_task = _make_review_task(task, diff, ctx.prior_error)

        # Run claude-code with review prompt
        result = await run_claude_code(
            endpoint=self.endpoint,
            model=self.model,
            repo_path=self.repo_path,
            task=review_task,
            claude_md_path=self.claude_md_path,
            run_log_path=ctx.run_log_path,
            timeout_s=task.timeout_s,
        )

        if not result.success:
            log.warning(
                "reviewer.failed",
                task_id=task.task_id,
                error=result.error_summary[:200],
            )
            return Failed(
                error=result.error_summary,
                retry_context={"tail": "\n".join(result.tail[-10:])},
                token_count=result.token_count,
                cost_usd=result.cost_usd,
            )

        # Check if the review passed
        tail_text = "\n".join(result.tail[-20:]).lower()
        if "review passed" in tail_text:
            log.info("reviewer.approved", task_id=task.task_id)
            return Proposed(
                branch=f"feat/{task.task_id.lower()}",
                pr_url=result.pr_url or "",
                token_count=result.token_count,
                cost_usd=result.cost_usd,
            )

        # Review found issues
        review_comments = "\n".join(result.tail[-20:])
        log.info(
            "reviewer.issues_found",
            task_id=task.task_id,
            comment_preview=review_comments[:100],
        )
        return Failed(
            error=f"Review found issues: {review_comments[:500]}",
            retry_context={"review_comments": review_comments},
            token_count=result.token_count,
            cost_usd=result.cost_usd,
        )

    def estimate_cost_usd(self, task: Task) -> float:
        """Return the configured per-review cost estimate."""
        return self._cost_per_review_usd

    # -- Internal helpers ----------------------------------------------------

    async def _try_fetch_diff(self, task: Task, ctx: TaskContext) -> str:
        """Try to fetch the PR diff from GitHub. Returns empty string on failure."""
        # Look for a PR URL in the run metadata
        from loomstack.core.state import read_run_meta

        run_meta = read_run_meta(ctx.run_log_path)
        if run_meta.pr_url:
            try:
                return await get_pr_diff(self.repo_path, run_meta.pr_url)
            except GitError:
                log.warning(
                    "reviewer.diff_fetch_failed",
                    task_id=task.task_id,
                    pr_url=run_meta.pr_url,
                )
        return ""


def _make_review_task(task: Task, diff: str, prior_error: str | None = None) -> Task:
    """Create a modified task with the review prompt as notes."""
    prompt = _REVIEW_PROMPT_TEMPLATE.format(
        description=task.description,
        diff=diff[:5000],  # Cap diff size for prompt
        prior_error=prior_error or "None",
    )
    return task.model_copy(update={"notes": prompt})
