"""GX10 health monitoring routes for Weaver."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, cast

import httpx
import structlog
from fastapi import APIRouter, Depends, Request

if TYPE_CHECKING:
    from fastapi.responses import Response

from loomstack.weaver.config import WeaverSettings, get_settings

log = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@dataclass
class GX10Status:
    is_healthy: bool
    model_id: str | None
    slots_active: int
    slots_total: int
    context_per_slot: int
    context_total: int
    error: str | None = None


async def fetch_gx10_status(base_url: str, api_key: str | None = None) -> GX10Status:
    """Poll GX10 endpoints and return a consolidated status."""
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    base = base_url.rstrip("/")

    try:
        async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
            health_resp, models_resp, slots_resp = await _gather_requests(client, base)
    except httpx.ConnectError:
        return GX10Status(
            is_healthy=False,
            model_id=None,
            slots_active=0,
            slots_total=0,
            context_per_slot=0,
            context_total=0,
            error="Connection refused",
        )
    except httpx.TimeoutException:
        return GX10Status(
            is_healthy=False,
            model_id=None,
            slots_active=0,
            slots_total=0,
            context_per_slot=0,
            context_total=0,
            error="Timeout",
        )

    # /health
    is_healthy = health_resp.status_code == 200

    # /v1/models — first model id
    model_id: str | None = None
    if models_resp.status_code == 200:
        try:
            data: dict[str, Any] = models_resp.json()
            model_id = str(data["data"][0]["id"])
        except (KeyError, IndexError, ValueError):
            log.warning("health.models_parse_error", body=models_resp.text[:200])

    # /slots — active slot count, total, per-slot and total context
    slots_active = 0
    slots_total = 0
    context_per_slot = 0
    context_total = 0
    if slots_resp.status_code == 200:
        try:
            slots: list[dict[str, Any]] = slots_resp.json()
            slots_total = len(slots)
            for slot in slots:
                if slot.get("state") == 1:  # 1 = processing
                    slots_active += 1
                slot_ctx = int(slot.get("n_ctx", 0))
                context_per_slot = max(context_per_slot, slot_ctx)
                context_total += slot_ctx
        except (ValueError, TypeError, KeyError):
            log.warning("health.slots_parse_error", body=slots_resp.text[:200])

    return GX10Status(
        is_healthy=is_healthy,
        model_id=model_id,
        slots_active=slots_active,
        slots_total=slots_total,
        context_per_slot=context_per_slot,
        context_total=context_total,
    )


async def _gather_requests(
    client: httpx.AsyncClient, base: str
) -> tuple[httpx.Response, httpx.Response, httpx.Response]:
    """Fire all three GX10 probe requests concurrently."""
    results = await asyncio.gather(
        client.get(f"{base}/health"),
        client.get(f"{base}/v1/models"),
        client.get(f"{base}/slots"),
        return_exceptions=True,
    )
    # Replace exceptions with synthetic 503 responses
    responses: list[httpx.Response] = []
    for r in results:
        if isinstance(r, BaseException):
            responses.append(httpx.Response(503, request=httpx.Request("GET", base)))
        else:
            responses.append(r)
    return responses[0], responses[1], responses[2]


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@router.get("/api/health")
async def get_health(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> dict[str, Any]:
    """Return GX10 status as JSON."""
    status = await fetch_gx10_status(settings.llm_base_url, settings.llm_api_key)
    return {
        "is_healthy": status.is_healthy,
        "model_id": status.model_id,
        "slots_active": status.slots_active,
        "slots_total": status.slots_total,
        "context_per_slot": status.context_per_slot,
        "context_total": status.context_total,
        "error": status.error,
    }


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------


@router.get("/api/health-fragment")
async def health_fragment(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    """HTMX partial: re-renders just the status panel every 15 s."""
    status = await fetch_gx10_status(settings.llm_base_url, settings.llm_api_key)
    return cast(
        "Response",
        request.app.state.templates.TemplateResponse(
            request,
            "health_fragment.html",
            {"status": status},
        ),
    )


@router.get("/health")
async def health_page(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    """Render the health dashboard page."""
    status = await fetch_gx10_status(settings.llm_base_url, settings.llm_api_key)
    return cast(
        "Response",
        request.app.state.templates.TemplateResponse(
            request,
            "health.html",
            {"active": "health", "status": status},
        ),
    )
