"""Tests for loomstack.weaver.routes.health."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from loomstack.weaver.app import create_app
from loomstack.weaver.config import WeaverSettings, get_settings
from loomstack.weaver.routes.health import GX10Status, fetch_gx10_status

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_resp(status_code: int, json_body: Any = None) -> httpx.Response:
    content = b""
    if json_body is not None:
        import json

        content = json.dumps(json_body).encode()
    return httpx.Response(
        status_code,
        content=content,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "http://gx10.local"),
    )


def _mock_gather(health: int, models: Any, slots: Any) -> AsyncMock:
    health_r = _make_resp(health)
    models_r = _make_resp(200, models) if models is not None else _make_resp(503)
    slots_r = _make_resp(200, slots) if slots is not None else _make_resp(503)

    async def fake_gather(
        client: Any, base: str
    ) -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        return health_r, models_r, slots_r

    return fake_gather  # type: ignore[return-value]


MODELS_RESP = {"data": [{"id": "qwen3-coder-next"}]}
SLOTS_RESP = [
    {"state": 1, "n_ctx": 8192},
    {"state": 0, "n_ctx": 8192},
]


# ---------------------------------------------------------------------------
# fetch_gx10_status unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_status_healthy() -> None:
    with patch(
        "loomstack.weaver.routes.health._gather_requests",
        side_effect=_mock_gather(200, MODELS_RESP, SLOTS_RESP),
    ):
        status = await fetch_gx10_status("http://gx10.local:8080")

    assert status.is_healthy is True
    assert status.model_id == "qwen3-coder-next"
    assert status.slots_active == 1
    assert status.slots_total == 2
    assert status.context_window == 8192
    assert status.error is None


@pytest.mark.asyncio
async def test_fetch_status_unhealthy_health_endpoint() -> None:
    with patch(
        "loomstack.weaver.routes.health._gather_requests",
        side_effect=_mock_gather(503, MODELS_RESP, SLOTS_RESP),
    ):
        status = await fetch_gx10_status("http://gx10.local:8080")

    assert status.is_healthy is False
    assert status.model_id == "qwen3-coder-next"  # models still parsed


@pytest.mark.asyncio
async def test_fetch_status_no_slots() -> None:
    with patch(
        "loomstack.weaver.routes.health._gather_requests",
        side_effect=_mock_gather(200, MODELS_RESP, None),
    ):
        status = await fetch_gx10_status("http://gx10.local:8080")

    assert status.slots_total == 0
    assert status.slots_active == 0


@pytest.mark.asyncio
async def test_fetch_status_connection_refused() -> None:
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        status = await fetch_gx10_status("http://gx10.local:8080")

    assert status.is_healthy is False
    assert status.error == "Connection refused"


@pytest.mark.asyncio
async def test_fetch_status_timeout() -> None:
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        status = await fetch_gx10_status("http://gx10.local:8080")

    assert status.is_healthy is False
    assert status.error == "Timeout"


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: WeaverSettings()
    return TestClient(app)


def test_get_health_json_healthy(client: TestClient) -> None:
    healthy = GX10Status(
        is_healthy=True,
        model_id="qwen3-coder-next",
        slots_active=1,
        slots_total=2,
        context_window=8192,
    )
    with patch(
        "loomstack.weaver.routes.health.fetch_gx10_status",
        new_callable=AsyncMock,
        return_value=healthy,
    ):
        resp = client.get("/api/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_healthy"] is True
    assert data["model_id"] == "qwen3-coder-next"
    assert data["slots_active"] == 1
    assert data["slots_total"] == 2


def test_get_health_json_offline(client: TestClient) -> None:
    offline = GX10Status(
        is_healthy=False,
        model_id=None,
        slots_active=0,
        slots_total=0,
        context_window=0,
        error="Connection refused",
    )
    with patch(
        "loomstack.weaver.routes.health.fetch_gx10_status",
        new_callable=AsyncMock,
        return_value=offline,
    ):
        resp = client.get("/api/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_healthy"] is False
    assert data["error"] == "Connection refused"


def test_health_page_renders(client: TestClient) -> None:
    healthy = GX10Status(
        is_healthy=True,
        model_id="qwen3-coder-next",
        slots_active=0,
        slots_total=4,
        context_window=4096,
    )
    with patch(
        "loomstack.weaver.routes.health.fetch_gx10_status",
        new_callable=AsyncMock,
        return_value=healthy,
    ):
        resp = client.get("/health")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "qwen3-coder-next" in resp.text
    assert "Online" in resp.text


def test_health_page_shows_offline(client: TestClient) -> None:
    offline = GX10Status(
        is_healthy=False,
        model_id=None,
        slots_active=0,
        slots_total=0,
        context_window=0,
        error="Timeout",
    )
    with patch(
        "loomstack.weaver.routes.health.fetch_gx10_status",
        new_callable=AsyncMock,
        return_value=offline,
    ):
        resp = client.get("/health")

    assert "Offline" in resp.text
    assert "Timeout" in resp.text


def test_health_fragment_renders(client: TestClient) -> None:
    healthy = GX10Status(
        is_healthy=True, model_id="qwen3", slots_active=1, slots_total=2, context_window=2048
    )
    with patch(
        "loomstack.weaver.routes.health.fetch_gx10_status",
        new_callable=AsyncMock,
        return_value=healthy,
    ):
        resp = client.get("/api/health-fragment")

    assert resp.status_code == 200
    assert "qwen3" in resp.text
    assert "1 / 2 active" in resp.text
