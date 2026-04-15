"""Landing page / dashboard route for Weaver."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

from fastapi import APIRouter, Depends, Request

if TYPE_CHECKING:
    from fastapi.responses import Response

from loomstack.weaver.config import WeaverSettings, get_settings
from loomstack.weaver.routes.budget import (
    _entries_for_day,
    _ledger_path,
    _read_ledger_entries,
    _tier_breakdown,
)
from loomstack.weaver.routes.health import fetch_gx10_status

router = APIRouter(tags=["dashboard"])


@dataclass
class TaskCounts:
    pending: int = 0
    in_progress: int = 0
    proposed: int = 0
    done: int = 0
    failed: int = 0
    blocked: int = 0


def _count_tasks(project_dir: str) -> tuple[TaskCounts, int]:
    """Read tasks and return (status counts, pending_approvals)."""
    counts = TaskCounts()
    pending_approvals = 0

    plan_path = Path(project_dir) / "PLAN.md"
    if not plan_path.exists():
        return counts, pending_approvals

    from loomstack.core.plan_parser import parse_plan_string

    plan = parse_plan_string(plan_path.read_text(encoding="utf-8"))
    tasks = plan.tasks

    runs_dir = Path(project_dir) / ".loomstack" / "runs"
    approvals_dir = Path(project_dir) / ".loomstack" / "approvals"

    for task in tasks:
        run_file = runs_dir / f"{task.task_id}.md"
        status = "pending"
        if run_file.exists():
            status = _read_status_from_run_file(run_file)

        match status:
            case "done":
                counts.done += 1
            case "in_progress":
                counts.in_progress += 1
            case "proposed":
                counts.proposed += 1
            case "failed":
                counts.failed += 1
            case "blocked":
                counts.blocked += 1
            case _:
                counts.pending += 1

        if (
            task.human_review
            and not (approvals_dir / task.task_id).exists()
            and status not in ("done", "failed")
        ):
            pending_approvals += 1

    return counts, pending_approvals


def _read_status_from_run_file(path: Path) -> str:
    """Extract status from run file frontmatter."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "pending"

    in_frontmatter = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            break
        if in_frontmatter and stripped.startswith("status:"):
            return stripped.split(":", 1)[1].strip().lower()
    return "pending"


@router.get("/")
async def dashboard(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    """Render the landing page with overview cards."""
    health = await fetch_gx10_status(settings.llm_base_url, settings.llm_api_key)

    entries = _read_ledger_entries(_ledger_path(settings))
    today_entries = _entries_for_day(entries, datetime.now(tz=UTC).date())
    breakdown = _tier_breakdown(today_entries)
    budget_total = round(sum(breakdown.values()), 4)

    counts, pending_approvals = _count_tasks(settings.loomstack_project_dir)

    return cast(
        "Response",
        request.app.state.templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "active": "dashboard",
                "health": health,
                "budget_total": budget_total,
                "counts": counts,
                "pending_approvals": pending_approvals,
            },
        ),
    )
