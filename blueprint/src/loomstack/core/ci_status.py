"""
CI status polling — checks GitHub PR check results.

Uses ``core/github.get_pr_status()`` to fetch PR check rollup and
returns a structured CIResult. Designed to be called by the dispatcher
after a task reaches PROPOSED state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from loomstack.core.github import GitError, get_pr_status

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# CI state
# ---------------------------------------------------------------------------


class CIState(StrEnum):
    """Possible CI check states."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"


# ---------------------------------------------------------------------------
# CI result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CIResult:
    """Structured result from polling PR CI checks."""

    state: CIState
    pr_url: str
    checks: list[CheckResult] = field(default_factory=list)
    summary: str = ""


@dataclass(frozen=True)
class CheckResult:
    """A single CI check's result."""

    name: str
    state: str
    conclusion: str = ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _map_check_state(check: dict[str, Any]) -> CheckResult:
    """Map a single statusCheckRollup entry to a CheckResult."""
    name = check.get("name") or check.get("context") or "unknown"
    state = check.get("status", "").upper()
    conclusion = check.get("conclusion") or ""
    return CheckResult(name=name, state=state, conclusion=conclusion)


def _aggregate_state(checks: list[CheckResult]) -> CIState:
    """Determine the overall CI state from individual check results."""
    if not checks:
        return CIState.PENDING

    conclusions = [c.conclusion.lower() for c in checks if c.conclusion]
    states = [c.state.upper() for c in checks]

    if any(c in ("failure", "action_required", "timed_out") for c in conclusions):
        return CIState.FAILURE

    if any(c in ("error", "cancelled", "startup_failure") for c in conclusions):
        return CIState.ERROR

    if any(s in ("QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED") for s in states):
        return CIState.PENDING

    if all(c == "success" for c in conclusions if c):
        return CIState.SUCCESS

    return CIState.PENDING


def _build_summary(state: CIState, checks: list[CheckResult]) -> str:
    """Build a human-readable summary of CI results."""
    total = len(checks)
    if total == 0:
        return "No CI checks found"

    passed = sum(1 for c in checks if c.conclusion.lower() == "success")
    failed = sum(1 for c in checks if c.conclusion.lower() in ("failure", "error", "timed_out"))
    pending = total - passed - failed

    parts = []
    if passed:
        parts.append(f"{passed} passed")
    if failed:
        parts.append(f"{failed} failed")
    if pending:
        parts.append(f"{pending} pending")

    detail = ", ".join(parts)

    if state == CIState.FAILURE:
        failed_names = [
            c.name for c in checks if c.conclusion.lower() in ("failure", "error", "timed_out")
        ]
        detail += f". Failed: {', '.join(failed_names)}"

    return f"{total} checks: {detail}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def poll_pr_checks(repo_path: Path, pr_url: str) -> CIResult:
    """
    Poll GitHub for PR check status.

    Returns a CIResult with the aggregate state and individual check results.
    On failure to fetch, returns CIState.ERROR with the error message.
    """
    try:
        data = await get_pr_status(repo_path, pr_url)
    except GitError as exc:
        log.warning(
            "ci_status.fetch_failed",
            pr_url=pr_url,
            error=str(exc),
        )
        return CIResult(
            state=CIState.ERROR,
            pr_url=pr_url,
            summary=f"Failed to fetch CI status: {exc}",
        )

    raw_checks: list[dict[str, Any]] = data.get("statusCheckRollup", []) or []
    checks = [_map_check_state(c) for c in raw_checks]
    state = _aggregate_state(checks)
    summary = _build_summary(state, checks)

    log.info(
        "ci_status.polled",
        pr_url=pr_url,
        state=state.value,
        total_checks=len(checks),
    )

    return CIResult(
        state=state,
        pr_url=pr_url,
        checks=checks,
        summary=summary,
    )
