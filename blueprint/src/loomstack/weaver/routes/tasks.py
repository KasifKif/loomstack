"""Task-related API routes for Weaver."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import markdown2  # type: ignore[import-untyped]
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi.responses import Response
    from fastapi.templating import Jinja2Templates

from loomstack.core.plan_parser import PlanParseError, Task, parse_plan_file
from loomstack.core.state import (
    RunMeta,
    TaskStatus,
    derive_run_meta,
    derive_status,
)
from loomstack.weaver.config import WeaverSettings, get_settings

logger = structlog.get_logger()

router = APIRouter(tags=["tasks"])

_TASK_ID_RE = re.compile(r"^[A-Z]{2,4}-\d+$")


def _validate_task_id(task_id: str) -> None:
    """Reject task IDs that don't match the expected pattern."""
    if not _TASK_ID_RE.match(task_id):
        raise HTTPException(status_code=400, detail="Invalid task ID format")


class TaskSummary(Task):
    """Task model extended with current runtime status."""

    status: TaskStatus


class TaskDetail(TaskSummary):
    """Full task detail including plan data and run metadata."""

    run_meta: RunMeta


class PlanResponse(BaseModel):
    """Response model for the task list."""

    title: str
    tasks: list[TaskSummary]


@router.get("/api/tasks", response_model=PlanResponse)
async def list_tasks(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> PlanResponse:
    """
    Return the full task list parsed from PLAN.md with current status.
    """
    project_dir = Path(settings.loomstack_project_dir)
    plan_path = project_dir / "PLAN.md"

    if not plan_path.exists():
        logger.error("plan_file_not_found", path=str(plan_path))
        raise HTTPException(status_code=404, detail="PLAN.md not found in project directory")

    try:
        plan = await parse_plan_file(plan_path)
    except PlanParseError as exc:
        logger.error("plan_parse_failed", path=str(plan_path), error=str(exc))
        raise HTTPException(status_code=422, detail=f"Invalid PLAN.md: {exc}") from exc
    except OSError as exc:
        logger.error("plan_read_failed", path=str(plan_path), error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to read PLAN.md: {exc}") from exc

    # Derive status for all tasks concurrently
    task_ids = [t.task_id for t in plan.tasks]
    statuses = await asyncio.gather(*[derive_status(tid, project_dir) for tid in task_ids])

    task_summaries = []
    for task, status in zip(plan.tasks, statuses, strict=True):
        summary = TaskSummary(**task.model_dump(), status=status)
        task_summaries.append(summary)

    return PlanResponse(title=plan.title, tasks=task_summaries)


@router.get("/api/tasks/{task_id}", response_model=TaskDetail)
async def get_task_detail(
    task_id: str,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> TaskDetail:
    """
    Return full details for a single task.
    """
    _validate_task_id(task_id)
    project_dir = Path(settings.loomstack_project_dir)
    plan_path = project_dir / "PLAN.md"

    if not plan_path.exists():
        raise HTTPException(status_code=404, detail="PLAN.md not found")

    try:
        plan = await parse_plan_file(plan_path)
        task = plan.get_task(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"Task {task_id} not found in PLAN.md"
        ) from None
    except PlanParseError as exc:
        logger.error("plan_parse_failed", path=str(plan_path), error=str(exc))
        raise HTTPException(status_code=422, detail=f"Invalid PLAN.md: {exc}") from exc
    except OSError as exc:
        logger.error("plan_read_failed", path=str(plan_path), error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to read PLAN.md: {exc}") from exc

    # Get status and run metadata concurrently
    status, run_meta = await asyncio.gather(
        derive_status(task_id, project_dir),
        derive_run_meta(task_id, project_dir),
    )

    return TaskDetail(**task.model_dump(), status=status, run_meta=run_meta)


def _approved_task_ids(project_dir: Path) -> set[str]:
    """Return set of task IDs that have approval markers."""
    approvals_dir = project_dir / ".loomstack" / "approvals"
    if not approvals_dir.is_dir():
        return set()
    return {p.name for p in approvals_dir.iterdir() if p.is_file()}


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    """
    Render the task list and dependency graph page.
    """
    plan_res = await list_tasks(settings)
    project_dir = Path(settings.loomstack_project_dir)
    approved = _approved_task_ids(project_dir)
    templates = cast("Jinja2Templates", request.app.state.templates)

    ctx = {
        "tasks": [t.model_dump() for t in plan_res.tasks],
        "approved_ids": approved,
    }

    # If it's an HTMX request for the table, return only the tbody content
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "task-table-body":
        return cast(
            "Response",
            templates.TemplateResponse(request, "task_table_partial.html", ctx),
        )

    return cast(
        "Response",
        templates.TemplateResponse(
            request,
            "tasks.html",
            {
                "title": plan_res.title,
                **ctx,
            },
        ),
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def get_task_detail_html(
    task_id: str,
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    """
    Return HTML partial for task detail side panel.
    """
    _validate_task_id(task_id)
    detail = await get_task_detail(task_id, settings)
    templates = cast("Jinja2Templates", request.app.state.templates)
    return cast(
        "Response",
        templates.TemplateResponse(
            request,
            "task_detail_partial.html",
            {"task": detail.model_dump()},
        ),
    )


@router.get("/tasks/{task_id}/log", response_class=HTMLResponse)
async def view_task_log(
    task_id: str,
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    """
    Render the run log for a task as HTML.
    """
    _validate_task_id(task_id)
    project_dir = Path(settings.loomstack_project_dir)
    run_file = project_dir / ".loomstack" / "runs" / f"{task_id}.md"

    if not run_file.exists():
        raise HTTPException(status_code=404, detail=f"Run log for task {task_id} not found")

    try:
        content = run_file.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("log_read_failed", task_id=task_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to read log file: {exc}") from exc

    # Parse metadata for the card
    from loomstack.core.state import _parse_run_meta

    run_meta = _parse_run_meta(content)

    # Strip frontmatter blocks for the body
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, flags=re.DOTALL | re.MULTILINE)
    body = body.strip()

    # Render markdown to HTML
    html_body = markdown2.markdown(
        body, extras=["fenced-code-blocks", "tables"], safe_mode="escape"
    )

    templates = cast("Jinja2Templates", request.app.state.templates)
    return cast(
        "Response",
        templates.TemplateResponse(
            request,
            "run_log.html",
            {
                "task_id": task_id,
                "run_meta": run_meta,
                "html_body": html_body,
            },
        ),
    )
