"""Tests for loomstack.weaver.gx10_client."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from loomstack.weaver.config import WeaverSettings
from loomstack.weaver.gx10_client import GX10Client, GX10Error

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(base_url: str = "http://gx10.local:8080") -> WeaverSettings:
    return WeaverSettings(gx10_base_url=base_url)


def _chat_response(content: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _sse_lines(tokens: list[str], include_done: bool = True) -> list[str]:
    """Build SSE event lines for a list of token strings."""
    lines: list[str] = []
    for tok in tokens:
        chunk = {"choices": [{"delta": {"content": tok}}]}
        lines.append(f"data: {json.dumps(chunk)}")
        lines.append("")  # blank line between events
    if include_done:
        lines.append("data: [DONE]")
    return lines


# ---------------------------------------------------------------------------
# complete() — non-streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_returns_content() -> None:
    client = GX10Client(settings=_make_settings())
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = _chat_response("Hello, world!")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_response)

        result = await client.complete([{"role": "user", "content": "Hi"}])

    assert result == "Hello, world!"


@pytest.mark.asyncio
async def test_complete_raises_on_5xx() -> None:
    client = GX10Client(settings=_make_settings())
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 503
    mock_response.text = "Service Unavailable"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GX10Error, match="503"):
            await client.complete([{"role": "user", "content": "Hi"}])


@pytest.mark.asyncio
async def test_complete_raises_on_connection_refused() -> None:
    client = GX10Client(settings=_make_settings())

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(GX10Error, match="connection refused"):
            await client.complete([{"role": "user", "content": "Hi"}])


@pytest.mark.asyncio
async def test_complete_raises_on_timeout() -> None:
    client = GX10Client(settings=_make_settings())

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        with pytest.raises(GX10Error, match="timed out"):
            await client.complete([{"role": "user", "content": "Hi"}])


@pytest.mark.asyncio
async def test_complete_raises_on_malformed_response() -> None:
    client = GX10Client(settings=_make_settings())
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {"unexpected": "structure"}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GX10Error, match="Unexpected GX10 response"):
            await client.complete([{"role": "user", "content": "Hi"}])


# ---------------------------------------------------------------------------
# stream_complete() — SSE streaming
# ---------------------------------------------------------------------------


async def _collect_stream(client: GX10Client, messages: list[dict[str, str]]) -> list[str]:
    chunks: list[str] = []
    async for tok in await _stream_gen(client, messages):
        chunks.append(tok)
    return chunks


async def _stream_gen(client: GX10Client, messages: list[dict[str, str]]) -> Any:
    """Wrapper because stream_complete is an async generator."""
    return client.stream_complete(messages)


@pytest.mark.asyncio
async def test_stream_complete_yields_tokens() -> None:
    client = GX10Client(settings=_make_settings())
    tokens = ["Hello", ", ", "world", "!"]
    sse_lines = _sse_lines(tokens)

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 200

    async def _aiter_lines() -> Any:
        for line in sse_lines:
            yield line

    mock_response.aiter_lines = _aiter_lines

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_http.stream = MagicMock(return_value=mock_ctx)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        collected: list[str] = []
        async for tok in client.stream_complete([{"role": "user", "content": "Hi"}]):
            collected.append(tok)

    assert collected == tokens


@pytest.mark.asyncio
async def test_stream_complete_skips_done_sentinel() -> None:
    """[DONE] line must not produce a chunk."""
    client = GX10Client(settings=_make_settings())
    sse_lines = ["data: [DONE]"]

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 200

    async def _aiter_lines() -> Any:
        for line in sse_lines:
            yield line

    mock_response.aiter_lines = _aiter_lines

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_http.stream = MagicMock(return_value=mock_ctx)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        collected: list[str] = []
        async for tok in client.stream_complete([{"role": "user", "content": "Hi"}]):
            collected.append(tok)

    assert collected == []


@pytest.mark.asyncio
async def test_stream_complete_skips_malformed_json() -> None:
    """Malformed JSON lines should be silently skipped."""
    client = GX10Client(settings=_make_settings())
    good_chunk = json.dumps({"choices": [{"delta": {"content": "ok"}}]})
    sse_lines = [
        "data: not-valid-json",
        "",
        f"data: {good_chunk}",
        "",
        "data: [DONE]",
    ]

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 200

    async def _aiter_lines() -> Any:
        for line in sse_lines:
            yield line

    mock_response.aiter_lines = _aiter_lines

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_http.stream = MagicMock(return_value=mock_ctx)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        collected: list[str] = []
        async for tok in client.stream_complete([{"role": "user", "content": "Hi"}]):
            collected.append(tok)

    assert collected == ["ok"]


@pytest.mark.asyncio
async def test_stream_complete_raises_on_connection_refused() -> None:
    client = GX10Client(settings=_make_settings())

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_http.stream = MagicMock(return_value=mock_ctx)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(GX10Error, match="connection refused"):
            async for _ in client.stream_complete([{"role": "user", "content": "Hi"}]):
                pass


@pytest.mark.asyncio
async def test_stream_complete_raises_on_5xx() -> None:
    client = GX10Client(settings=_make_settings())

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_http.stream = MagicMock(return_value=mock_ctx)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(GX10Error, match="500"):
            async for _ in client.stream_complete([{"role": "user", "content": "Hi"}]):
                pass


@pytest.mark.asyncio
async def test_stream_complete_raises_on_timeout() -> None:
    client = GX10Client(settings=_make_settings())

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_http.stream = MagicMock(return_value=mock_ctx)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(GX10Error, match="timed out"):
            async for _ in client.stream_complete([{"role": "user", "content": "Hi"}]):
                pass
