"""Multi-project API and HTML routes for Weaver."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi.responses import Response

from loomstack.weaver.config import WeaverSettings, get_settings, parse_project_dirs
from loomstack.weaver.routes.budget import (
    TodayBudgetResponse,
    _entries_for_day,
    _read_ledger_entries,
    _tier_breakdown,
)
from loomstack.weaver.routes.tasks import PlanResponse, TaskSummary

log = structlog.get_logger(__name__)

router = APIRouter(tags=["projects"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_project(project: str, settings: WeaverSettings) -> Path:
    """Return the Path for *project* or raise HTTP 404."""
    dirs = parse_project_dirs(settings)
    if project not in dirs:
        raise HTTPException(status_code=404, detail=f"Project '{project}' not configured")
    return Path(dirs[project])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ProjectInfo(BaseModel):
    name: str
    path: str


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@router.get("/api/projects", response_model=list[ProjectInfo])
async def list_projects(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> list[ProjectInfo]:
    """Return all configured project names and paths."""
    dirs = parse_project_dirs(settings)
    return [ProjectInfo(name=n, path=p) for n, p in dirs.items()]


@router.get("/api/{project}/tasks", response_model=PlanResponse)
async def get_project_tasks(
    project: str,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> PlanResponse:
    """Return task list for the named project."""
    from loomstack.core.plan_parser import PlanParseError, parse_plan_file
    from loomstack.core.state import derive_status

    project_dir = _resolve_project(project, settings)
    plan_path = project_dir / "PLAN.md"

    if not plan_path.exists():
        raise HTTPException(status_code=404, detail="PLAN.md not found")

    try:
        plan = await parse_plan_file(plan_path)
    except PlanParseError as exc:
        log.error("plan_parse_failed", project=project, error=str(exc))
        raise HTTPException(status_code=422, detail=f"Invalid PLAN.md: {exc}") from exc
    except OSError as exc:
        log.error("plan_read_failed", project=project, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    statuses = await asyncio.gather(*[derive_status(t.task_id, project_dir) for t in plan.tasks])
    return PlanResponse(
        title=plan.title,
        tasks=[
            TaskSummary(**t.model_dump(), status=s)
            for t, s in zip(plan.tasks, statuses, strict=True)
        ],
    )


@router.get("/api/{project}/budget/today", response_model=TodayBudgetResponse)
async def get_project_budget_today(
    project: str,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> TodayBudgetResponse:
    """Return today's spend for the named project."""
    project_dir = _resolve_project(project, settings)
    ledger = str(project_dir / ".loomstack" / "ledger.jsonl")
    entries = _read_ledger_entries(ledger)
    today = datetime.now(tz=UTC).date()
    breakdown = _tier_breakdown(_entries_for_day(entries, today))
    total = sum(breakdown.values())
    return TodayBudgetResponse(
        date=today.isoformat(),
        total_usd=round(total, 4),
        per_tier={k: round(v, 4) for k, v in sorted(breakdown.items())},
    )


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------


@router.get("/projects/{project}", include_in_schema=False)
async def project_tasks_page(
    project: str,
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Any:
    """Render the task list page scoped to a named project."""
    plan_res = await get_project_tasks(project, settings)
    return cast(
        "Response",
        request.app.state.templates.TemplateResponse(
            request,
            "tasks.html",
            {
                "active": "tasks",
                "project": project,
                "title": plan_res.title,
                "tasks": [t.model_dump() for t in plan_res.tasks],
            },
        ),
    )
