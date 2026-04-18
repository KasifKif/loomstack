"""Approval gate API routes for Weaver."""

from __future__ import annotations

import re
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from loomstack.core.plan_parser import PlanParseError, parse_plan_file
from loomstack.core.state import approval_marker_path, is_approved
from loomstack.weaver.config import WeaverSettings, get_active_project_dir, get_settings

logger = structlog.get_logger()

router = APIRouter(prefix="/api", tags=["approvals"])

_TASK_ID_RE = re.compile(r"^[A-Z]{2,4}-\d+$")


def _validate_task_id(task_id: str) -> None:
    if not _TASK_ID_RE.match(task_id):
        raise HTTPException(status_code=400, detail="Invalid task ID format")


class PendingApproval(BaseModel):
    """A task awaiting human review."""

    task_id: str
    description: str


class PendingApprovalsResponse(BaseModel):
    """Response model for the pending approvals list."""

    tasks: list[PendingApproval]


@router.post("/approve/{task_id}", status_code=status.HTTP_201_CREATED, response_model=None)
async def approve_task(
    task_id: str,
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> dict[str, str] | HTMLResponse:
    """
    Create a marker file to approve a task. Idempotent.
    Returns HTML badge when called from HTMX, JSON otherwise.
    """
    _validate_task_id(task_id)
    project_dir = await get_active_project_dir(settings)
    loomstack_dir = project_dir / ".loomstack"
    marker = approval_marker_path(task_id, loomstack_dir)

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError as exc:
        logger.error("approval_failed", task_id=task_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create approval marker: {exc}",
        ) from exc

    logger.info("task_approved", task_id=task_id)

    if request.headers.get("HX-Request"):
        return HTMLResponse(
            '<span class="badge review-approved">Approved</span>',
            status_code=201,
        )
    return {"status": "approved", "task_id": task_id}


@router.get("/pending-approvals", response_model=PendingApprovalsResponse)
async def list_pending_approvals(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> PendingApprovalsResponse:
    """
    Return a list of tasks that require human_review but haven't been approved.
    """
    project_dir = await get_active_project_dir(settings)
    plan_path = project_dir / "PLAN.md"

    if not plan_path.exists():
        return PendingApprovalsResponse(tasks=[])

    try:
        plan = await parse_plan_file(plan_path)
    except (PlanParseError, OSError) as exc:
        logger.error("plan_parse_failed", path=str(plan_path), error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to parse PLAN.md: {exc}",
        ) from exc

    loomstack_dir = project_dir / ".loomstack"
    pending = []
    for task in plan.tasks:
        if task.human_review and not is_approved(task.task_id, loomstack_dir):
            pending.append(PendingApproval(task_id=task.task_id, description=task.description))

    return PendingApprovalsResponse(tasks=pending)
