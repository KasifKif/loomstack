"""Task-related API routes for Weaver."""

import asyncio
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from loomstack.core.plan_parser import Task, parse_plan_file
from loomstack.core.state import (
    RunMeta,
    TaskStatus,
    derive_run_meta,
    derive_status,
)
from loomstack.weaver.config import WeaverSettings, get_settings

logger = structlog.get_logger()

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


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


@router.get("", response_model=PlanResponse)
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
    except Exception as exc:
        logger.exception("plan_parse_failed", path=str(plan_path))
        raise HTTPException(status_code=500, detail=f"Failed to parse PLAN.md: {exc}") from exc

    # Derive status for all tasks concurrently
    task_ids = [t.task_id for t in plan.tasks]
    statuses = await asyncio.gather(*[derive_status(tid, project_dir) for tid in task_ids])

    task_summaries = []
    for task, status in zip(plan.tasks, statuses, strict=True):
        summary = TaskSummary(**task.model_dump(), status=status)
        task_summaries.append(summary)

    return PlanResponse(title=plan.title, tasks=task_summaries)


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task_detail(
    task_id: str,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> TaskDetail:
    """
    Return full details for a single task.
    """
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
    except Exception as exc:
        logger.exception("plan_parse_failed", path=str(plan_path))
        raise HTTPException(status_code=500, detail=f"Failed to parse PLAN.md: {exc}") from exc

    # Get status and run metadata concurrently
    status, run_meta = await asyncio.gather(
        derive_status(task_id, project_dir),
        derive_run_meta(task_id, project_dir),
    )

    return TaskDetail(**task.model_dump(), status=status, run_meta=run_meta)
