"""Tests for loomstack.weaver.routes.chat."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from loomstack.weaver.app import create_app
from loomstack.weaver.config import WeaverSettings, get_settings
from loomstack.weaver.openai_compat_client import LLMClientError
from loomstack.weaver.routes import chat as chat_module

# ---------------------------------------------------------------------------
# Chat page (GET /chat)
# ---------------------------------------------------------------------------


def test_chat_page_renders() -> None:
    app = create_app()
    with TestClient(app) as c:
        resp = c.get("/chat")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "ws/chat" in resp.text
    assert 'class="active"' in resp.text  # nav active state


def test_chat_page_contains_key_elements() -> None:
    app = create_app()
    with TestClient(app) as c:
        resp = c.get("/chat")
    assert 'id="messages"' in resp.text
    assert 'id="msg-input"' in resp.text
    assert 'id="send-btn"' in resp.text
    assert 'id="new-btn"' in resp.text


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_conversations() -> Any:
    """Reset in-memory conversation store between tests."""
    chat_module._conversations.clear()
    yield
    chat_module._conversations.clear()


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: WeaverSettings()
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /api/chat — REST
# ---------------------------------------------------------------------------


def test_post_chat_returns_reply(client: TestClient) -> None:
    with patch(
        "loomstack.weaver.routes.chat.OpenAICompatClient.complete",
        new_callable=AsyncMock,
        return_value="Hello back!",
    ):
        resp = client.post("/api/chat", json={"message": "Hi"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "Hello back!"
    assert "conversation_id" in data


def test_post_chat_uses_provided_conversation_id(client: TestClient) -> None:
    with patch(
        "loomstack.weaver.routes.chat.OpenAICompatClient.complete",
        new_callable=AsyncMock,
        return_value="reply",
    ):
        resp = client.post("/api/chat", json={"message": "Hi", "conversation_id": "conv-abc"})

    assert resp.json()["conversation_id"] == "conv-abc"


def test_post_chat_accumulates_history(client: TestClient) -> None:
    """Second message in same conversation includes prior turn in history."""
    calls: list[list[dict[str, str]]] = []

    async def mock_complete(messages: list[dict[str, str]], **_: Any) -> str:
        calls.append(list(messages))
        return "reply"

    with patch(
        "loomstack.weaver.routes.chat.OpenAICompatClient.complete", side_effect=mock_complete
    ):
        client.post("/api/chat", json={"message": "First", "conversation_id": "c1"})
        client.post("/api/chat", json={"message": "Second", "conversation_id": "c1"})

    assert len(calls[1]) == 3  # system turn 1 user + assistant + turn 2 user
    assert calls[1][0]["content"] == "First"
    assert calls[1][1]["role"] == "assistant"
    assert calls[1][2]["content"] == "Second"


def test_post_chat_empty_message_returns_422(client: TestClient) -> None:
    resp = client.post("/api/chat", json={"message": ""})
    assert resp.status_code == 422


def test_post_chat_llm_error_returns_502(client: TestClient) -> None:
    with patch(
        "loomstack.weaver.routes.chat.OpenAICompatClient.complete",
        new_callable=AsyncMock,
        side_effect=LLMClientError("connection refused"),
    ):
        resp = client.post("/api/chat", json={"message": "Hi"})

    assert resp.status_code == 502
    assert resp.json()["detail"] == "LLM request failed"


def test_post_chat_llm_error_does_not_corrupt_history(client: TestClient) -> None:
    """Failed LLM call must not leave a dangling user message in history."""
    with patch(
        "loomstack.weaver.routes.chat.OpenAICompatClient.complete",
        new_callable=AsyncMock,
        side_effect=LLMClientError("boom"),
    ):
        client.post("/api/chat", json={"message": "Hi", "conversation_id": "c2"})

    assert "c2" not in chat_module._conversations or len(chat_module._conversations["c2"]) == 0


# ---------------------------------------------------------------------------
# WebSocket /ws/chat
# ---------------------------------------------------------------------------


def test_ws_chat_streams_tokens(client: TestClient) -> None:
    async def mock_stream(*_: Any, **__: Any) -> Any:
        for tok in ["Hello", " world"]:
            yield tok

    with (
        patch("loomstack.weaver.routes.chat.OpenAICompatClient.stream_complete", mock_stream),
        client.websocket_connect("/ws/chat") as ws,
    ):
        ws.send_json({"message": "Hi", "conversation_id": "ws1"})
        messages = []
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg["type"] == "done":
                break

    token_msgs = [m for m in messages if m["type"] == "token"]
    assert [m["content"] for m in token_msgs] == ["Hello", " world"]
    assert messages[-1] == {"type": "done"}


def test_ws_chat_empty_message_sends_error(client: TestClient) -> None:
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"message": "  ", "conversation_id": "ws2"})
        msg = ws.receive_json()

    assert msg["type"] == "error"
    assert "empty" in msg["content"].lower()


def test_ws_chat_malformed_json_sends_error(client: TestClient) -> None:
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_text("this is not json")
        msg = ws.receive_json()
        # Connection should survive — send a valid message after
        ws.send_json({"message": "hi", "conversation_id": "bad-json-test"})

    assert msg["type"] == "error"
    assert "json" in msg["content"].lower()


def test_ws_chat_llm_error_sends_error_then_done(client: TestClient) -> None:
    async def bad_stream(*_: Any, **__: Any) -> Any:
        raise LLMClientError("timeout")
        yield  # make it a generator

    with (
        patch("loomstack.weaver.routes.chat.OpenAICompatClient.stream_complete", bad_stream),
        client.websocket_connect("/ws/chat") as ws,
    ):
        ws.send_json({"message": "Hi", "conversation_id": "ws3"})
        error_msg = ws.receive_json()
        done_msg = ws.receive_json()

    assert error_msg["type"] == "error"
    assert done_msg["type"] == "done"


def test_ws_chat_maintains_history_across_turns(client: TestClient) -> None:
    """Second WS message in same conversation sees prior assistant reply."""
    seen: list[list[dict[str, str]]] = []

    async def mock_stream(_self: Any, messages: list[dict[str, str]], **__: Any) -> Any:
        seen.append(list(messages))
        yield "reply"

    with (
        patch("loomstack.weaver.routes.chat.OpenAICompatClient.stream_complete", mock_stream),
        client.websocket_connect("/ws/chat") as ws,
    ):
        ws.send_json({"message": "Turn1", "conversation_id": "ws4"})
        while ws.receive_json()["type"] != "done":
            pass
        ws.send_json({"message": "Turn2", "conversation_id": "ws4"})
        while ws.receive_json()["type"] != "done":
            pass

    assert len(seen[1]) == 3
    assert seen[1][0]["content"] == "Turn1"
    assert seen[1][1]["role"] == "assistant"
    assert seen[1][2]["content"] == "Turn2"
