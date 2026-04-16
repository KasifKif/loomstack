"""Worker tier configuration routes for Weaver."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from loomstack.weaver.config import WeaverSettings, get_data_dir, get_settings
from loomstack.weaver.routes.providers import Provider
from loomstack.weaver.store import JsonStore

if TYPE_CHECKING:
    from fastapi.responses import Response
    from fastapi.templating import Jinja2Templates

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["workers"])

AgentTier = Literal[
    "classifier",
    "mac_worker",
    "code_worker",
    "content_worker",
    "reviewer",
    "architect",
    "researcher",
    "test_runner",
]

AGENT_TIERS: list[str] = [
    "classifier",
    "mac_worker",
    "code_worker",
    "content_worker",
    "reviewer",
    "architect",
    "researcher",
    "test_runner",
]


class Worker(BaseModel):
    id: str
    name: str
    agent_tier: AgentTier
    provider_id: str
    model_name: str
    timeout_seconds: int = 300


def _make_worker_store(settings: WeaverSettings) -> JsonStore[Worker]:
    return JsonStore(get_data_dir(settings), "workers.json", Worker)


def _make_provider_store(settings: WeaverSettings) -> JsonStore[Provider]:
    return JsonStore(get_data_dir(settings), "providers.json", Provider)


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-") or "worker"


@router.get("/workers", response_class=HTMLResponse)
async def workers_page(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    worker_store = _make_worker_store(settings)
    provider_store = _make_provider_store(settings)
    workers = await worker_store.load_all()
    providers = await provider_store.load_all()
    templates = cast("Jinja2Templates", request.app.state.templates)
    return cast(
        "Response",
        templates.TemplateResponse(
            request,
            "workers.html",
            {
                "active": "workers",
                "workers": list(workers.values()),
                "providers": list(providers.values()),
                "agent_tiers": AGENT_TIERS,
            },
        ),
    )


@router.get("/api/workers")
async def list_workers(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> JSONResponse:
    store = _make_worker_store(settings)
    items = await store.load_all()
    return JSONResponse([w.model_dump() for w in items.values()])


@router.post("/api/workers")
async def create_worker(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    body = await request.json()
    worker_id = _slugify(body.get("name", ""))
    worker_store = _make_worker_store(settings)
    existing = await worker_store.get(worker_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Worker '{worker_id}' already exists")

    # Cross-validate provider_id
    provider_id = body.get("provider_id", "")
    provider_store = _make_provider_store(settings)
    if not await provider_store.get(provider_id):
        raise HTTPException(status_code=422, detail=f"Provider '{provider_id}' not found")

    try:
        worker = Worker(id=worker_id, **{k: v for k, v in body.items() if k != "id"})
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await worker_store.upsert(worker_id, worker)
    logger.info("worker_created", id=worker_id)

    if request.headers.get("HX-Request"):
        provider = await provider_store.get(provider_id)
        templates = cast("Jinja2Templates", request.app.state.templates)
        return cast(
            "Response",
            templates.TemplateResponse(
                request,
                "worker_row_partial.html",
                {"worker": worker, "provider": provider},
                status_code=201,
            ),
        )
    return JSONResponse(worker.model_dump(), status_code=201)


@router.put("/api/workers/{worker_id}")
async def update_worker(
    worker_id: str,
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    worker_store = _make_worker_store(settings)
    existing = await worker_store.get(worker_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Worker '{worker_id}' not found")
    body = await request.json()

    # Cross-validate provider_id if changed
    provider_id = body.get("provider_id", existing.provider_id)
    provider_store = _make_provider_store(settings)
    if not await provider_store.get(provider_id):
        raise HTTPException(status_code=422, detail=f"Provider '{provider_id}' not found")

    try:
        updated = existing.model_copy(update={k: v for k, v in body.items() if k != "id"})
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await worker_store.upsert(worker_id, updated)
    logger.info("worker_updated", id=worker_id)

    if request.headers.get("HX-Request"):
        provider = await provider_store.get(provider_id)
        templates = cast("Jinja2Templates", request.app.state.templates)
        return cast(
            "Response",
            templates.TemplateResponse(
                request,
                "worker_row_partial.html",
                {"worker": updated, "provider": provider},
            ),
        )
    return JSONResponse(updated.model_dump())


@router.delete("/api/workers/{worker_id}")
async def delete_worker(
    worker_id: str,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    store = _make_worker_store(settings)
    deleted = await store.delete(worker_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Worker '{worker_id}' not found")
    logger.info("worker_deleted", id=worker_id)
    return HTMLResponse("", status_code=200)
