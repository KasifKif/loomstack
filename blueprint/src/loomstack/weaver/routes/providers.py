"""Providers CRUD routes for Weaver."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from loomstack.weaver.config import WeaverSettings, get_data_dir, get_settings
from loomstack.weaver.store import JsonStore

if TYPE_CHECKING:
    from fastapi.responses import Response
    from fastapi.templating import Jinja2Templates

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["providers"])


class Provider(BaseModel):
    id: str
    name: str
    provider_type: Literal["anthropic", "gemini", "openai-compat"]
    base_url: str
    api_key: str
    cost_per_input_token: float
    cost_per_output_token: float
    rate_limit_rpm: int = -1
    token_limit: int = -1


def _make_store(settings: WeaverSettings) -> JsonStore[Provider]:
    return JsonStore(get_data_dir(settings), "providers.json", Provider)


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-") or "provider"


def _mask_key(key: str) -> str:
    """Show only the last 4 characters of an API key."""
    if len(key) <= 4:
        return "*" * len(key)
    return "****" + key[-4:]


@router.get("/providers", response_class=HTMLResponse)
async def providers_page(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    store = _make_store(settings)
    items = await store.load_all()
    templates = cast("Jinja2Templates", request.app.state.templates)
    return cast(
        "Response",
        templates.TemplateResponse(
            request,
            "providers.html",
            {"active": "providers", "providers": list(items.values())},
        ),
    )


@router.get("/api/providers")
async def list_providers(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> JSONResponse:
    store = _make_store(settings)
    items = await store.load_all()
    masked = []
    for p in items.values():
        d = p.model_dump()
        d["api_key"] = _mask_key(p.api_key)
        masked.append(d)
    return JSONResponse(masked)


@router.post("/api/providers")
async def create_provider(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    body = await request.json()
    provider_id = _slugify(body.get("name", ""))
    store = _make_store(settings)
    existing = await store.get(provider_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Provider '{provider_id}' already exists")
    try:
        provider = Provider(id=provider_id, **{k: v for k, v in body.items() if k != "id"})
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await store.upsert(provider_id, provider)
    logger.info("provider_created", id=provider_id)

    if request.headers.get("HX-Request"):
        templates = cast("Jinja2Templates", request.app.state.templates)
        return cast(
            "Response",
            templates.TemplateResponse(
                request,
                "provider_row_partial.html",
                {"provider": provider},
                status_code=201,
            ),
        )
    return JSONResponse(provider.model_dump(), status_code=201)


@router.put("/api/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    store = _make_store(settings)
    existing = await store.get(provider_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    body = await request.json()
    try:
        updated = existing.model_copy(update={k: v for k, v in body.items() if k != "id"})
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await store.upsert(provider_id, updated)
    logger.info("provider_updated", id=provider_id)

    if request.headers.get("HX-Request"):
        templates = cast("Jinja2Templates", request.app.state.templates)
        return cast(
            "Response",
            templates.TemplateResponse(
                request,
                "provider_row_partial.html",
                {"provider": updated},
            ),
        )
    return JSONResponse(updated.model_dump())


@router.delete("/api/providers/{provider_id}")
async def delete_provider(
    provider_id: str,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    store = _make_store(settings)
    deleted = await store.delete(provider_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    logger.info("provider_deleted", id=provider_id)
    return HTMLResponse("", status_code=200)


@router.get("/api/providers-table", response_class=HTMLResponse)
async def providers_table(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    store = _make_store(settings)
    items = await store.load_all()
    templates = cast("Jinja2Templates", request.app.state.templates)
    return cast(
        "Response",
        templates.TemplateResponse(
            request,
            "providers_table_partial.html",
            {"providers": list(items.values())},
        ),
    )
