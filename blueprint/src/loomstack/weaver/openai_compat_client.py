"""Async httpx client for any OpenAI-compatible /v1/chat/completions endpoint."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from loomstack.weaver.config import WeaverSettings, get_settings

log = structlog.get_logger(__name__)


class LLMClientError(Exception):
    """Raised when the LLM endpoint returns an error or is unreachable."""


class OpenAICompatClient:
    """Async client for any OpenAI-compatible /v1/chat/completions endpoint.

    Configured via WeaverSettings (WEAVER_LLM_BASE_URL, WEAVER_LLM_API_KEY,
    WEAVER_LLM_DEFAULT_MODEL). Works with llama-server, vLLM, OpenAI, Gemini
    OpenAI-compat endpoints, or any other compatible server.
    """

    def __init__(self, settings: WeaverSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._base_url = self._settings.llm_base_url.rstrip("/")
        self._default_model = self._settings.llm_default_model
        self._headers = self._build_headers(self._settings.llm_api_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        """Send a non-streaming chat completion request; return the reply text."""
        payload = self._build_payload(
            messages,
            model=model or self._default_model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        try:
            async with httpx.AsyncClient(timeout=120.0, headers=self._headers) as client:
                response = await client.post(
                    f"{self._base_url}/v1/chat/completions",
                    json=payload,
                )
        except httpx.ConnectError as exc:
            raise LLMClientError(f"LLM endpoint connection refused at {self._base_url}") from exc
        except httpx.TimeoutException as exc:
            raise LLMClientError(f"LLM endpoint timed out at {self._base_url}") from exc

        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return self._extract_content(data)

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Stream chat completion tokens; yields each text chunk as a str."""
        payload = self._build_payload(
            messages,
            model=model or self._default_model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        try:
            async with (
                httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
                    headers=self._headers,
                ) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/v1/chat/completions",
                    json=payload,
                ) as response,
            ):
                if response.status_code >= 400:
                    await response.aread()
                    body = response.text[:500]
                    raise LLMClientError(
                        f"LLM endpoint returned HTTP {response.status_code}: {body}"
                    )
                async for chunk in self._iter_sse_chunks(response):
                    yield chunk
        except httpx.ConnectError as exc:
            raise LLMClientError(f"LLM endpoint connection refused at {self._base_url}") from exc
        except httpx.TimeoutException as exc:
            raise LLMClientError(f"LLM endpoint timed out at {self._base_url}") from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_headers(api_key: str | None) -> dict[str, str]:
        if api_key:
            return {"Authorization": f"Bearer {api_key}"}
        return {}

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
        """Raise LLMClientError for 4xx/5xx responses (non-streaming only)."""
        if response.status_code >= 400:
            body = response.text[:500]
            raise LLMClientError(f"LLM endpoint returned HTTP {response.status_code}: {body}")

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        """Pull the assistant message content from a non-streaming response."""
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError) as exc:
            raise LLMClientError(
                f"Unexpected response structure from LLM endpoint: {data!r}"
            ) from exc

    @staticmethod
    async def _iter_sse_chunks(response: httpx.Response) -> AsyncIterator[str]:
        """Parse SSE lines, yield text delta per chunk."""
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
                log.warning("openai_compat_client.sse_parse_error", raw=payload[:200])
                continue
            try:
                delta = chunk["choices"][0]["delta"].get("content") or ""
            except (KeyError, IndexError):
                continue
            if delta:
                yield delta
