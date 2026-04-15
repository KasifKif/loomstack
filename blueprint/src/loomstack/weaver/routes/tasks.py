"""Task-related API routes for Weaver."""

from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException

from loomstack.core.plan_parser import Plan, parse_plan_file
from loomstack.weaver.config import WeaverSettings, get_settings

logger = structlog.get_logger()

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
async def list_tasks(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Plan:
    """
    Return the full task list parsed from PLAN.md.
    """
    plan_path = Path(settings.loomstack_project_dir) / "PLAN.md"

    if not plan_path.exists():
        logger.error("plan_file_not_found", path=str(plan_path))
        raise HTTPException(status_code=404, detail="PLAN.md not found in project directory")

    try:
        plan = await parse_plan_file(plan_path)
        return plan
    except Exception as exc:
        logger.exception("plan_parse_failed", path=str(plan_path))
        raise HTTPException(status_code=500, detail=f"Failed to parse PLAN.md: {exc}") from exc
