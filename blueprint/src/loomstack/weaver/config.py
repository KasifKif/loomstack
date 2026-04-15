"""Weaver configuration via environment / .env file."""

import functools
from pathlib import Path

from pydantic_settings import BaseSettings


class WeaverSettings(BaseSettings):
    """Settings for the Weaver dashboard."""

    model_config = {"env_prefix": "WEAVER_"}

    llm_base_url: str = "http://gx10.local:8081"
    llm_api_key: str | None = None
    llm_default_model: str = "qwen3-coder-next"
    loomstack_project_dir: str = "."
    # Comma-separated additional project directories for multi-project mode.
    # When set, all named projects appear in the sidebar selector.
    loomstack_project_dirs: str = ""
    host: str = "127.0.0.1"
    port: int = 8400

    # Discord bot relay (optional — only needed when running discord_bot)
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_user_ids: str = ""  # comma-separated allowlisted user IDs
    discord_channel_ids: str = ""  # comma-separated watched channel IDs


def parse_project_dirs(settings: WeaverSettings) -> dict[str, str]:
    """Return {name: path_str} for all configured project directories.

    Always includes the primary ``loomstack_project_dir`` as the first entry.
    Additional entries come from ``loomstack_project_dirs`` (comma-separated).
    Names are derived from the final path component.  Duplicate names are
    silently skipped (first wins).
    """
    dirs: dict[str, str] = {}
    primary = settings.loomstack_project_dir
    primary_name = Path(primary).name or "project"
    dirs[primary_name] = primary
    raw = settings.loomstack_project_dirs.strip()
    if raw:
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            name = Path(entry).name or entry
            if name not in dirs:
                dirs[name] = entry
    return dirs


@functools.lru_cache(maxsize=1)
def get_settings() -> WeaverSettings:
    """Return a cached settings instance (parsed once from env)."""
    return WeaverSettings()
