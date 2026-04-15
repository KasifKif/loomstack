"""Tests for loomstack.weaver.discord_bot."""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from loomstack.weaver.config import WeaverSettings
from loomstack.weaver.discord_bot import WeaverBot, _parse_ids, _split_message

# ---------------------------------------------------------------------------
# _parse_ids
# ---------------------------------------------------------------------------


def test_parse_ids_basic() -> None:
    assert _parse_ids("123,456,789") == {123, 456, 789}


def test_parse_ids_whitespace() -> None:
    assert _parse_ids(" 1 , 2 , 3 ") == {1, 2, 3}


def test_parse_ids_empty() -> None:
    assert _parse_ids("") == set()


def test_parse_ids_single() -> None:
    assert _parse_ids("42") == {42}


# ---------------------------------------------------------------------------
# _split_message
# ---------------------------------------------------------------------------


def test_split_message_short() -> None:
    assert _split_message("hello") == ["hello"]


def test_split_message_exact_limit() -> None:
    text = "x" * 1990
    assert _split_message(text) == [text]


def test_split_message_newline_split() -> None:
    line = "a" * 990
    text = f"{line}\n{line}\n{line}"  # ~2981 chars
    chunks = _split_message(text)
    assert len(chunks) == 2
    for chunk in chunks:
        assert len(chunk) <= 1990


def test_split_message_no_newline_hard_split() -> None:
    # Single long word — must split at limit
    text = "x" * 3000
    chunks = _split_message(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == 1990
    assert len(chunks[1]) == 1010


def test_split_message_preserves_content() -> None:
    text = "hello\nworld\nfoo"
    chunks = _split_message(text)
    # Re-joining (any order) should recover all non-whitespace content
    joined = "\n".join(chunks)
    for word in ("hello", "world", "foo"):
        assert word in joined


# ---------------------------------------------------------------------------
# WeaverBot helpers (unit — no real Discord connection)
# ---------------------------------------------------------------------------


def _make_settings(**kwargs: str) -> WeaverSettings:
    defaults: dict[str, str] = {
        "discord_bot_token": "fake-token",
        "discord_guild_id": "111",
        "discord_user_ids": "1,2,3",
        "discord_channel_ids": "10,20",
    }
    defaults.update(kwargs)
    return WeaverSettings(**defaults)  # type: ignore[arg-type]


def _make_bot(settings: WeaverSettings | None = None) -> WeaverBot:
    if settings is None:
        settings = _make_settings()
    with patch("loomstack.weaver.discord_bot.OpenAICompatClient"):
        return WeaverBot(settings=settings)


def test_bot_init_parses_ids() -> None:
    bot = _make_bot()
    assert bot._allowed_users == {1, 2, 3}
    assert bot._allowed_channels == {10, 20}
    assert bot._guild_id == 111


def test_get_history_creates_deque() -> None:
    bot = _make_bot()
    hist = bot._get_history(999)
    assert isinstance(hist, deque)
    assert hist.maxlen == 20


def test_get_history_same_object_returned() -> None:
    bot = _make_bot()
    h1 = bot._get_history(5)
    h2 = bot._get_history(5)
    assert h1 is h2


# ---------------------------------------------------------------------------
# on_message — DM flow
# ---------------------------------------------------------------------------


def _make_message(
    *,
    author_id: int = 1,
    is_dm: bool = True,
    channel_id: int = 10,
    content: str = "hello",
    is_own: bool = False,
    mentions_bot: bool = False,
    user_id: int | None = None,
) -> MagicMock:
    import discord

    msg = MagicMock(spec=discord.Message)
    author = MagicMock()
    author.id = author_id
    msg.author = author
    msg.clean_content = content
    msg.channel = MagicMock(spec=discord.DMChannel if is_dm else discord.TextChannel)
    msg.channel.id = channel_id
    msg.reply = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_on_message_ignores_own() -> None:
    bot = _make_bot()
    msg = MagicMock()
    msg.author = bot.user  # same object → own message
    await bot.on_message(msg)
    # Should return early — no history, no LLM call
    assert len(bot._histories) == 0


@pytest.mark.asyncio
async def test_on_message_ignores_non_allowed_dm() -> None:

    bot = _make_bot()
    msg = _make_message(author_id=999, is_dm=True)
    # author is not in allowed_users
    msg.author = MagicMock()
    msg.author.id = 999
    msg.author.__eq__ = lambda self, other: False
    await bot.on_message(msg)
    assert len(bot._histories) == 0


@pytest.mark.asyncio
async def test_on_message_dm_allowed_user_calls_llm() -> None:
    import discord

    bot = _make_bot()
    reply_text = "pong"
    bot._llm.complete = AsyncMock(return_value=reply_text)  # type: ignore[method-assign]

    fake_user = MagicMock()
    fake_user.id = 0
    fake_user.__eq__ = lambda self, other: False

    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.id = 1  # in allowed_users
    msg.author.__eq__ = lambda self, other: False
    msg.channel = MagicMock(spec=discord.DMChannel)
    msg.channel.id = 99
    msg.channel.typing = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    msg.clean_content = "ping"
    msg.reply = AsyncMock()
    msg.mentions = []

    with patch.object(type(bot), "user", new_callable=PropertyMock, return_value=fake_user):
        await bot.on_message(msg)

    bot._llm.complete.assert_awaited_once()
    msg.reply.assert_awaited_once_with("pong", mention_author=False)
    hist = bot._get_history(1)
    assert list(hist) == [
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
    ]


@pytest.mark.asyncio
async def test_on_message_llm_error_pops_history() -> None:
    import discord

    from loomstack.weaver.openai_compat_client import LLMClientError

    bot = _make_bot()
    bot._llm.complete = AsyncMock(side_effect=LLMClientError("boom"))  # type: ignore[method-assign]

    fake_user = MagicMock()
    fake_user.id = 0
    fake_user.__eq__ = lambda self, other: False

    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.id = 2  # in allowed_users
    msg.author.__eq__ = lambda self, other: False
    msg.channel = MagicMock(spec=discord.DMChannel)
    msg.channel.id = 99
    msg.channel.typing = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    msg.clean_content = "trigger error"
    msg.reply = AsyncMock()
    msg.mentions = []

    with patch.object(type(bot), "user", new_callable=PropertyMock, return_value=fake_user):
        await bot.on_message(msg)

    # History must not be corrupted — user turn was popped
    hist = bot._get_history(2)
    assert len(hist) == 0
    # Error reply was sent
    call_args = msg.reply.call_args
    assert "boom" in call_args.args[0]


@pytest.mark.asyncio
async def test_on_message_channel_requires_mention() -> None:
    import discord

    bot = _make_bot()
    bot._llm.complete = AsyncMock(return_value="hi")  # type: ignore[method-assign]

    fake_user = MagicMock()
    fake_user.id = 55
    fake_user.__eq__ = lambda self, other: False

    # Message in allowed channel but bot NOT mentioned
    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.id = 999  # not in allowed_users — uses channel path
    msg.author.__eq__ = lambda self, other: False
    msg.channel = MagicMock(spec=discord.TextChannel)
    msg.channel.id = 10  # in allowed_channels
    msg.clean_content = "hello everyone"
    msg.mentions = []  # bot not mentioned

    with patch.object(type(bot), "user", new_callable=PropertyMock, return_value=fake_user):
        await bot.on_message(msg)

    bot._llm.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_strips_mention_prefix() -> None:
    import discord

    bot = _make_bot()
    reply_text = "ok"
    bot._llm.complete = AsyncMock(return_value=reply_text)  # type: ignore[method-assign]

    fake_user = MagicMock()
    fake_user.id = 55
    fake_user.__eq__ = lambda self, other: False

    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.id = 999
    msg.author.__eq__ = lambda self, other: False
    msg.channel = MagicMock(spec=discord.TextChannel)
    msg.channel.id = 10
    msg.channel.typing = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    msg.clean_content = "<@55> do a thing"
    msg.mentions = [fake_user]
    msg.reply = AsyncMock()

    with patch.object(type(bot), "user", new_callable=PropertyMock, return_value=fake_user):
        await bot.on_message(msg)

    # The text passed to LLM should not contain the mention
    call_args = bot._llm.complete.call_args
    messages: list[dict[str, str]] = call_args.args[0]
    assert messages[-1]["content"] == "do a thing"
