"""Async httpx client for the GX10 llama-server (OpenAI-compatible API)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from loomstack.weaver.config import WeaverSettings, get_settings

log = structlog.get_logger(__name__)


class GX10Error(Exception):
    """Raised when the GX10 endpoint returns an error or is unreachable."""


class GX10Client:
    """Thin async wrapper around the GX10 OpenAI-compatible chat endpoint."""

    def __init__(self, settings: WeaverSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._base_url = self._settings.gx10_base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str = "qwen3-coder-next",
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        """Send a non-streaming chat completion request; return the reply text."""
        payload = self._build_payload(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, stream=False
        )
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self._base_url}/v1/chat/completions",
                    json=payload,
                )
        except httpx.ConnectError as exc:
            raise GX10Error(f"GX10 connection refused at {self._base_url}") from exc
        except httpx.TimeoutException as exc:
            raise GX10Error(f"GX10 request timed out at {self._base_url}") from exc

        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return self._extract_content(data)

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str = "qwen3-coder-next",
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Stream chat completion tokens; yields each text chunk as a str."""
        payload = self._build_payload(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, stream=True
        )
        try:
            async with (
                httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
                ) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/v1/chat/completions",
                    json=payload,
                ) as response,
            ):
                self._raise_for_status(response)
                async for chunk in self._iter_sse_chunks(response):
                    yield chunk
        except httpx.ConnectError as exc:
            raise GX10Error(f"GX10 connection refused at {self._base_url}") from exc
        except httpx.TimeoutException as exc:
            raise GX10Error(f"GX10 request timed out at {self._base_url}") from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_payload(
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Raise GX10Error for 4xx/5xx responses."""
        if response.status_code >= 400:
            try:
                body = response.text[:500]
            except Exception:
                body = "<unreadable>"
            raise GX10Error(f"GX10 returned HTTP {response.status_code}: {body}")

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        """Pull the assistant message content from a non-streaming response."""
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError) as exc:
            raise GX10Error(f"Unexpected GX10 response structure: {data!r}") from exc

    @staticmethod
    async def _iter_sse_chunks(response: httpx.Response) -> AsyncIterator[str]:
        """Parse SSE lines from llama-server, yield text delta per chunk."""
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                break
            try:
                chunk: dict[str, Any] = json.loads(payload)
            except json.JSONDecodeError:
                log.warning("gx10_client.sse_parse_error", raw=payload[:200])
                continue
            try:
                delta = chunk["choices"][0]["delta"].get("content") or ""
            except (KeyError, IndexError):
                continue
            if delta:
                yield delta
