"""Tests for loomstack.weaver.openai_compat_client."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from loomstack.weaver.config import WeaverSettings
from loomstack.weaver.openai_compat_client import LLMClientError, OpenAICompatClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(
    base_url: str = "http://gx10.local:8080",
    api_key: str | None = None,
    model: str = "qwen3-coder-next",
) -> WeaverSettings:
    return WeaverSettings(llm_base_url=base_url, llm_api_key=api_key, llm_default_model=model)


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
        lines.append("")
    if include_done:
        lines.append("data: [DONE]")
    return lines


# ---------------------------------------------------------------------------
# Config / headers
# ---------------------------------------------------------------------------


def test_no_api_key_sends_no_auth_header() -> None:
    client = OpenAICompatClient(settings=_make_settings())
    assert "Authorization" not in client._headers


def test_api_key_sets_bearer_header() -> None:
    client = OpenAICompatClient(settings=_make_settings(api_key="sk-test"))
    assert client._headers["Authorization"] == "Bearer sk-test"


def test_default_model_from_settings() -> None:
    client = OpenAICompatClient(settings=_make_settings(model="gpt-4o"))
    assert client._default_model == "gpt-4o"


# ---------------------------------------------------------------------------
# complete() — non-streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_returns_content() -> None:
    client = OpenAICompatClient(settings=_make_settings())
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
async def test_complete_uses_override_model() -> None:
    """model= kwarg overrides the default."""
    client = OpenAICompatClient(settings=_make_settings())
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = _chat_response("ok")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_response)

        await client.complete([{"role": "user", "content": "Hi"}], model="claude-3-5-sonnet")
        payload = mock_http.post.call_args.kwargs["json"]

    assert payload["model"] == "claude-3-5-sonnet"


@pytest.mark.asyncio
async def test_complete_raises_on_5xx() -> None:
    client = OpenAICompatClient(settings=_make_settings())
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 503
    mock_response.text = "Service Unavailable"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_response)

        with pytest.raises(LLMClientError, match="503"):
            await client.complete([{"role": "user", "content": "Hi"}])


@pytest.mark.asyncio
async def test_complete_raises_on_connection_refused() -> None:
    client = OpenAICompatClient(settings=_make_settings())

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(LLMClientError, match="connection refused"):
            await client.complete([{"role": "user", "content": "Hi"}])


@pytest.mark.asyncio
async def test_complete_raises_on_timeout() -> None:
    client = OpenAICompatClient(settings=_make_settings())

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        with pytest.raises(LLMClientError, match="timed out"):
            await client.complete([{"role": "user", "content": "Hi"}])


@pytest.mark.asyncio
async def test_complete_raises_on_malformed_response() -> None:
    client = OpenAICompatClient(settings=_make_settings())
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {"unexpected": "structure"}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_response)

        with pytest.raises(LLMClientError, match="Unexpected response structure"):
            await client.complete([{"role": "user", "content": "Hi"}])


# ---------------------------------------------------------------------------
# stream_complete() — SSE streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_complete_yields_tokens() -> None:
    client = OpenAICompatClient(settings=_make_settings())
    tokens = ["Hello", ", ", "world", "!"]
    sse = _sse_lines(tokens)

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 200

    async def _aiter_lines() -> Any:
        for line in sse:
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
    client = OpenAICompatClient(settings=_make_settings())

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 200

    async def _aiter_lines() -> Any:
        yield "data: [DONE]"

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
    client = OpenAICompatClient(settings=_make_settings())
    good = json.dumps({"choices": [{"delta": {"content": "ok"}}]})

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 200

    async def _aiter_lines() -> Any:
        for line in ["data: not-valid-json", "", f"data: {good}", "", "data: [DONE]"]:
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
    client = OpenAICompatClient(settings=_make_settings())

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_http.stream = MagicMock(return_value=mock_ctx)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(LLMClientError, match="connection refused"):
            async for _ in client.stream_complete([{"role": "user", "content": "Hi"}]):
                pass


@pytest.mark.asyncio
async def test_stream_complete_raises_on_5xx() -> None:
    client = OpenAICompatClient(settings=_make_settings())

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_response.aread = AsyncMock(return_value=b"Internal Server Error")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_http.stream = MagicMock(return_value=mock_ctx)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(LLMClientError, match="500"):
            async for _ in client.stream_complete([{"role": "user", "content": "Hi"}]):
                pass


@pytest.mark.asyncio
async def test_stream_complete_raises_on_timeout() -> None:
    client = OpenAICompatClient(settings=_make_settings())

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_http.stream = MagicMock(return_value=mock_ctx)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(LLMClientError, match="timed out"):
            async for _ in client.stream_complete([{"role": "user", "content": "Hi"}]):
                pass
