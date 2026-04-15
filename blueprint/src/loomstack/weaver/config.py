"""Weaver configuration via environment / .env file."""

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


def get_settings() -> WeaverSettings:
    """Return a cached settings instance."""
    return WeaverSettings()
