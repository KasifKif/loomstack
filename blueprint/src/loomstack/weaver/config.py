"""Weaver configuration via environment / .env file."""

from pydantic_settings import BaseSettings


class WeaverSettings(BaseSettings):
    """Settings for the Weaver dashboard."""

    model_config = {"env_prefix": "WEAVER_"}

    gx10_base_url: str = "http://gx10.local:8080"
    loomstack_project_dir: str = "."
    host: str = "127.0.0.1"
    port: int = 8400


def get_settings() -> WeaverSettings:
    """Return a cached settings instance."""
    return WeaverSettings()
