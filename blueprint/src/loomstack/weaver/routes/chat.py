"""WebSocket and REST chat routes for Weaver."""

from __future__ import annotations

import uuid
from collections import deque
from typing import TYPE_CHECKING, Annotated, Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi.responses import Response

from loomstack.weaver.config import WeaverSettings, get_settings
from loomstack.weaver.openai_compat_client import LLMClientError, OpenAICompatClient

log = structlog.get_logger(__name__)

router = APIRouter(tags=["chat"])


@router.get("/chat")
async def chat_page(request: Request) -> Response:
    """Render the chat UI."""
    return cast(
        "Response",
        request.app.state.templates.TemplateResponse(request, "chat.html", {"active": "chat"}),
    )


# In-memory conversation store: conversation_id -> deque of message dicts.
# Deques are capped at _HISTORY_MAX; oldest messages drop off automatically.
_HISTORY_MAX = 50
_conversations: dict[str, deque[dict[str, str]]] = {}


def _get_history(conversation_id: str) -> deque[dict[str, str]]:
    if conversation_id not in _conversations:
        _conversations[conversation_id] = deque(maxlen=_HISTORY_MAX)
    return _conversations[conversation_id]


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/ws/chat")
async def ws_chat(
    websocket: WebSocket,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> None:
    """Stream chat completion tokens over a WebSocket.

    Client sends: {"message": str, "conversation_id": str (optional)}
    Server sends:
      {"type": "token", "content": str}  — one per streamed token
      {"type": "done"}                   — when generation is complete
      {"type": "error", "content": str}  — on LLM or validation failure
    """
    await websocket.accept()
    client = OpenAICompatClient(settings=settings)

    try:
        while True:
            data: Any = await websocket.receive_json()

            message = data.get("message", "").strip()
            if not message:
                await websocket.send_json({"type": "error", "content": "Empty message."})
                continue

            conversation_id: str = data.get("conversation_id") or str(uuid.uuid4())
            history = _get_history(conversation_id)
            history.append({"role": "user", "content": message})

            reply_chunks: list[str] = []
            try:
                async for token in client.stream_complete(list(history)):
                    reply_chunks.append(token)
                    await websocket.send_json({"type": "token", "content": token})
            except LLMClientError as exc:
                log.warning("ws_chat.llm_error", error=str(exc))
                await websocket.send_json({"type": "error", "content": str(exc)})
                # Pop the user message we already appended so history stays clean.
                history.pop()
                await websocket.send_json({"type": "done"})
                continue

            history.append({"role": "assistant", "content": "".join(reply_chunks)})
            await websocket.send_json({"type": "done"})

    except WebSocketDisconnect:
        log.info("ws_chat.disconnected")


# ---------------------------------------------------------------------------
# REST endpoint
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    conversation_id: str


@router.post("/api/chat", response_model=ChatResponse)
async def post_chat(
    body: ChatRequest,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> ChatResponse:
    """Non-streaming chat; returns the full reply once generation is complete."""
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Empty message.")

    conversation_id = body.conversation_id or str(uuid.uuid4())
    history = _get_history(conversation_id)
    history.append({"role": "user", "content": message})

    client = OpenAICompatClient(settings=settings)
    try:
        reply = await client.complete(list(history))
    except LLMClientError as exc:
        history.pop()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    history.append({"role": "assistant", "content": reply})
    return ChatResponse(reply=reply, conversation_id=conversation_id)
