"""Weaver configuration via environment / .env file."""

import functools

from pydantic_settings import BaseSettings


class WeaverSettings(BaseSettings):
    """Settings for the Weaver dashboard."""

    model_config = {"env_prefix": "WEAVER_"}

    llm_base_url: str = "http://gx10.local:8080"
    llm_api_key: str | None = None
    llm_default_model: str = "qwen3-coder-next"
    loomstack_project_dir: str = "."
    host: str = "127.0.0.1"
    port: int = 8400

    # Discord bot relay (optional — only needed when running discord_bot)
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_user_ids: str = ""  # comma-separated allowlisted user IDs
    discord_channel_ids: str = ""  # comma-separated watched channel IDs


@functools.lru_cache(maxsize=1)
def get_settings() -> WeaverSettings:
    """Return a cached settings instance (parsed once from env)."""
    return WeaverSettings()
