"""Discord bot relay for Weaver.

Standalone entry point: python -m loomstack.weaver.discord_bot

Per-user conversation history (in-memory, capped at 20 messages).
Responds to DMs from allowlisted users and messages in configured channels.

Required env vars (read via WeaverSettings):
  WEAVER_DISCORD_BOT_TOKEN   — Discord bot token
  WEAVER_DISCORD_GUILD_ID    — Guild (server) ID to operate in
  WEAVER_DISCORD_USER_IDS    — Comma-separated allowlisted user IDs (DMs)
  WEAVER_DISCORD_CHANNEL_IDS — Comma-separated channel IDs to listen in
"""

from __future__ import annotations

import asyncio
import sys
from collections import deque

import discord
import structlog

from loomstack.weaver.config import WeaverSettings
from loomstack.weaver.openai_compat_client import LLMClientError, OpenAICompatClient

log = structlog.get_logger(__name__)

_HISTORY_MAX = 20


def _parse_ids(raw: str) -> set[int]:
    """Parse a comma-separated string of integer IDs."""
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


class WeaverBot(discord.Client):
    def __init__(self, settings: WeaverSettings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self._settings = settings
        self._llm = OpenAICompatClient(settings=settings)
        self._guild_id = int(settings.discord_guild_id)
        self._allowed_users = _parse_ids(settings.discord_user_ids)
        self._allowed_channels = _parse_ids(settings.discord_channel_ids)
        # Per-user history: user_id -> deque of message dicts
        self._histories: dict[int, deque[dict[str, str]]] = {}

    def _get_history(self, user_id: int) -> deque[dict[str, str]]:
        if user_id not in self._histories:
            self._histories[user_id] = deque(maxlen=_HISTORY_MAX)
        return self._histories[user_id]

    async def on_ready(self) -> None:
        log.info("discord_bot.ready", user=str(self.user))

    async def on_message(self, message: discord.Message) -> None:
        # Ignore own messages
        if message.author == self.user:
            return

        author_id = message.author.id
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_allowed_channel = message.channel.id in self._allowed_channels

        # Only respond to: DMs from allowlisted users OR messages in watched channels
        if is_dm:
            if author_id not in self._allowed_users:
                return
        elif is_allowed_channel:
            # In channels, only respond if mentioned or message starts with bot prefix
            if self.user not in message.mentions:
                return
        else:
            return

        text = message.clean_content.strip()
        # Strip mention prefix if present
        if self.user and f"<@{self.user.id}>" in text:
            text = text.replace(f"<@{self.user.id}>", "").strip()
        if self.user and f"<@!{self.user.id}>" in text:
            text = text.replace(f"<@!{self.user.id}>", "").strip()

        if not text:
            return

        history = self._get_history(author_id)
        history.append({"role": "user", "content": text})

        async with message.channel.typing():
            try:
                reply = await self._llm.complete(list(history))
            except LLMClientError as exc:
                log.warning("discord_bot.llm_error", error=str(exc), user=author_id)
                history.pop()  # Don't corrupt history on failure
                await message.reply(f"⚠️ LLM error: {exc}", mention_author=False)
                return

        history.append({"role": "assistant", "content": reply})

        # Discord message limit is 2000 chars — split if needed
        for chunk in _split_message(reply):
            await message.reply(chunk, mention_author=False)

        log.info("discord_bot.replied", user=author_id, chars=len(reply))


def _split_message(text: str, limit: int = 1990) -> list[str]:
    """Split a reply into ≤limit-char chunks on newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def main() -> None:
    settings = WeaverSettings()
    if not settings.discord_bot_token:
        log.error("discord_bot.missing_token")
        sys.exit(1)

    bot = WeaverBot(settings=settings)
    asyncio.run(bot.start(settings.discord_bot_token))


if __name__ == "__main__":
    main()
