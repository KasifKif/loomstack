"""Dispatcher lifecycle routes for Weaver."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from loomstack.weaver.config import WeaverSettings, get_data_dir, get_settings

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["dispatcher"])


# ---------------------------------------------------------------------------
# In-memory dispatcher status (lives on app.state)
# ---------------------------------------------------------------------------


@dataclass
class DispatcherStatus:
    """Mutable status visible to the template layer."""

    is_running: bool = False
    started_at: str | None = None
    last_cycle_at: str | None = None
    last_dispatched: int = 0
    total_dispatched: int = 0
    error: str | None = None
    active_project: str | None = None


def _get_status(request: Request) -> DispatcherStatus:
    if not hasattr(request.app.state, "dispatcher_status"):
        request.app.state.dispatcher_status = DispatcherStatus()
    status: DispatcherStatus = request.app.state.dispatcher_status
    return status


# ---------------------------------------------------------------------------
# Helpers — load context for templates
# ---------------------------------------------------------------------------


async def _load_template_context(settings: WeaverSettings) -> tuple[int, str | None]:
    """Return (worker_count, active_project_name)."""
    from loomstack.weaver.routes.git_projects import Project
    from loomstack.weaver.routes.workers import Worker
    from loomstack.weaver.store import JsonStore

    data_dir = get_data_dir(settings)
    workers = await JsonStore(data_dir, "workers.json", Worker).load_all()
    projects = await JsonStore(data_dir, "projects.json", Project).load_all()
    active_project = next((p for p in projects.values() if p.is_active), None)
    return len(workers), active_project.name if active_project else None


# ---------------------------------------------------------------------------
# Builder — construct a Dispatcher from Weaver stores
# ---------------------------------------------------------------------------


async def _build_dispatcher(
    settings: WeaverSettings,
) -> tuple[Any, str]:
    """
    Build a Dispatcher instance from the configured workers, providers, and
    active git project.

    Returns (dispatcher, project_name).
    Raises HTTPException on misconfiguration.
    """
    from loomstack.agents.architect import Architect
    from loomstack.agents.classifier import Classifier
    from loomstack.agents.code_worker import CodeWorker
    from loomstack.agents.reviewer import Reviewer
    from loomstack.core.budget import Budget
    from loomstack.core.config import BudgetConfig, ConfigError, load_config
    from loomstack.core.dispatcher import Dispatcher
    from loomstack.weaver.routes.git_projects import Project
    from loomstack.weaver.routes.providers import Provider
    from loomstack.weaver.routes.workers import Worker
    from loomstack.weaver.store import JsonStore

    data_dir = get_data_dir(settings)

    # 1. Find active project
    project_store: JsonStore[Project] = JsonStore(data_dir, "projects.json", Project)
    projects = await project_store.load_all()
    active = next((p for p in projects.values() if p.is_active), None)

    if active is None:
        raise HTTPException(status_code=422, detail="No active project — activate one first")

    repo_path = Path(active.local_path)
    if not repo_path.exists():
        raise HTTPException(status_code=422, detail=f"Project path does not exist: {repo_path}")

    claude_md_path = repo_path / "CLAUDE.md"

    # 2. Load providers
    provider_store: JsonStore[Provider] = JsonStore(data_dir, "providers.json", Provider)
    providers = await provider_store.load_all()

    # 3. Load workers and build agents
    worker_store: JsonStore[Worker] = JsonStore(data_dir, "workers.json", Worker)
    workers = await worker_store.load_all()

    if not workers:
        raise HTTPException(status_code=422, detail="No workers configured")

    agents: dict[str, Any] = {}
    agent_classes = {
        "code_worker": CodeWorker,
        "reviewer": Reviewer,
        "architect": Architect,
    }

    for worker in workers.values():
        if worker.agent_tier not in agent_classes:
            continue  # skip classifier tier

        provider = providers.get(worker.provider_id)
        if provider is None:
            logger.warning(
                "dispatcher.missing_provider",
                worker=worker.name,
                provider_id=worker.provider_id,
            )
            continue

        cls = agent_classes[worker.agent_tier]
        agents[worker.agent_tier] = cls(
            endpoint=provider.base_url,
            model=worker.model_name,
            repo_path=repo_path,
            claude_md_path=claude_md_path,
        )

    if not agents:
        raise HTTPException(
            status_code=422,
            detail="No valid agent tiers could be built — check worker/provider config",
        )

    # 4. Budget
    loomstack_dir = repo_path / ".loomstack"
    ledger_path = loomstack_dir / "ledger.jsonl"
    yaml_path = repo_path / "loomstack.yaml"

    try:
        config = load_config(yaml_path)
        budget_config = config.budget
    except ConfigError:
        budget_config = BudgetConfig()

    budget = await Budget.create(budget_config, ledger_path)

    # 5. Classifier (keyword-based, no config)
    classifier = Classifier()

    # 6. Dispatcher
    dispatcher = Dispatcher(
        repo_path=repo_path,
        agents=agents,
        budget=budget,
        classifier=classifier,
        plan_path=repo_path / "PLAN.md",
        loomstack_dir=loomstack_dir,
    )

    return dispatcher, active.name


# ---------------------------------------------------------------------------
# Background loop wrapper
# ---------------------------------------------------------------------------


async def _dispatch_loop(
    status: DispatcherStatus,
    dispatcher: Any,
    interval_s: int = 30,
) -> None:
    """Run dispatch cycles, updating status after each one."""
    try:
        while True:
            try:
                results = await dispatcher.run_once()
                status.last_cycle_at = datetime.now(UTC).isoformat()
                status.last_dispatched = len(results)
                status.total_dispatched += len(results)
                status.error = None
            except Exception as exc:
                status.error = str(exc)[:200]
                logger.exception("dispatcher.cycle_error")
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        status.is_running = False
        raise


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/dispatcher", response_class=HTMLResponse)
async def dispatcher_page(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Any:
    status = _get_status(request)
    templates = cast("Jinja2Templates", request.app.state.templates)
    worker_count, active_project_name = await _load_template_context(settings)

    return templates.TemplateResponse(
        request,
        "dispatcher.html",
        {
            "active": "dispatcher",
            "status": status,
            "worker_count": worker_count,
            "active_project_name": active_project_name,
        },
    )


@router.get("/api/dispatcher/status", response_class=HTMLResponse)
async def dispatcher_status_fragment(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Any:
    status = _get_status(request)
    templates = cast("Jinja2Templates", request.app.state.templates)
    worker_count, active_project_name = await _load_template_context(settings)

    return templates.TemplateResponse(
        request,
        "dispatcher_status_partial.html",
        {
            "status": status,
            "worker_count": worker_count,
            "active_project_name": active_project_name,
        },
    )


@router.post("/api/dispatcher/start", response_class=HTMLResponse)
async def start_dispatcher(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Any:
    status = _get_status(request)

    # Check if already running
    task: asyncio.Task[None] | None = getattr(request.app.state, "dispatcher_task", None)
    if task is not None and not task.done():
        raise HTTPException(status_code=409, detail="Dispatcher is already running")

    # Build and launch
    dispatcher, project_name = await _build_dispatcher(settings)

    status.is_running = True
    status.started_at = datetime.now(UTC).isoformat()
    status.error = None
    status.active_project = project_name
    status.total_dispatched = 0

    bg_task = asyncio.create_task(_dispatch_loop(status, dispatcher))
    request.app.state.dispatcher_task = bg_task

    logger.info("dispatcher.started", project=project_name)

    return await dispatcher_status_fragment(request, settings)


@router.post("/api/dispatcher/stop", response_class=HTMLResponse)
async def stop_dispatcher(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Any:
    status = _get_status(request)

    task: asyncio.Task[None] | None = getattr(request.app.state, "dispatcher_task", None)
    if task is None or task.done():
        raise HTTPException(status_code=409, detail="Dispatcher is not running")

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)

    status.is_running = False
    request.app.state.dispatcher_task = None

    logger.info("dispatcher.stopped")

    return await dispatcher_status_fragment(request, settings)
