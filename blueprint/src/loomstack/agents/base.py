"""
BaseAgent protocol and the shared dataclasses all agent tiers use.

Every agent in agents/ must implement BaseAgent. Result variants:
  - Proposed(branch, pr_url)  — PR opened, awaiting review/CI
  - Blocked(reason)           — needs gate, approval, or missing context
  - Failed(error, retry_ctx)  — retry with expanded context

TaskContext carries the runtime inputs an agent needs beyond the Task itself.
AgentResult is the sealed union of the three result variants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from loomstack.core.plan_parser import Task


# ---------------------------------------------------------------------------
# Task context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskContext:
    """
    Runtime inputs passed to every agent alongside the Task.

    Attributes
    ----------
    repo_path:
        Absolute path to the managed project repo.
    loomstack_dir:
        Path to the .loomstack/ directory (runs, ledger, approvals).
    claude_md_path:
        Path to the repo-local CLAUDE.md. Passed to claude-code as context.
    run_log_path:
        Path to .loomstack/runs/<task-id>.md where agent streams output.
    retry_count:
        How many times this task has already been attempted at this tier.
    prior_error:
        Error message from the previous attempt, if any. Used to expand
        context on retries.
    prior_diff:
        Diff from the previous attempt (for retry context expansion).
    """

    repo_path: Path
    loomstack_dir: Path
    claude_md_path: Path
    run_log_path: Path
    retry_count: int = 0
    prior_error: str | None = None
    prior_diff: str | None = None
    extra_context_files: list[Path] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Result variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposed:
    """Agent opened a PR. Awaiting review/CI."""

    branch: str
    pr_url: str
    token_count: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Blocked:
    """Agent cannot proceed without human input or an approval gate."""

    reason: str
    # Optional: which approval file to watch for
    approval_path: str | None = None


@dataclass(frozen=True)
class Failed:
    """Agent failed. Caller decides whether to retry or escalate."""

    error: str
    # Structured retry context to expand on next attempt
    retry_context: dict[str, str] = field(default_factory=dict)
    token_count: int = 0
    cost_usd: float = 0.0


# Sealed union — exhaustive match on isinstance()
AgentResult = Proposed | Blocked | Failed


# ---------------------------------------------------------------------------
# BaseAgent protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BaseAgent(Protocol):
    """
    Protocol all Loomstack agent tiers must satisfy.

    Rules (enforced by tests, not the type system):
    - execute() must be idempotent: re-running from the same inputs produces
      no duplicate PRs or commits.
    - estimate_cost_usd() must be called by the dispatcher BEFORE execute().
    - Agents never commit to the default branch directly.
    - Agents never write to .loomstack/ledger.jsonl; they return AgentResult
      and the dispatcher writes the ledger.
    """

    #: Stable identifier used for routing and ledger entries (e.g. "code_worker")
    role: str

    #: Human-readable model description for logs (e.g. "qwen3-coder-next @ gx10")
    model_id: str

    async def can_handle(self, task: Task) -> bool:
        """
        Return True if this agent is capable of handling the given task.
        Used by the dispatcher to validate routing before calling execute().
        """
        ...

    async def execute(self, task: Task, ctx: TaskContext) -> AgentResult:
        """
        Attempt the task. Returns Proposed, Blocked, or Failed.
        Must be idempotent — safe to call again on the same task/ctx.
        """
        ...

    def estimate_cost_usd(self, task: Task) -> float:
        """
        Estimate the cost in USD before running. Budget system calls this
        before execute() and rejects if it exceeds the daily cap.
        Return 0.0 for local/free tiers.
        """
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_terminal(result: AgentResult) -> bool:
    """Return True if the result requires no further agent action (Proposed or Blocked)."""
    return isinstance(result, (Proposed, Blocked))


def result_cost_usd(result: AgentResult) -> float:
    """Extract cost from a result. Blocked has no cost field → 0.0."""
    if isinstance(result, (Proposed, Failed)):
        return result.cost_usd
    return 0.0


def result_token_count(result: AgentResult) -> int:
    """Extract token count from a result. Blocked has no token field → 0."""
    if isinstance(result, (Proposed, Failed)):
        return result.token_count
    return 0
