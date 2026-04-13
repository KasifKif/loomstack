"""
Load and validate loomstack.yaml project configuration.

Single point of contact for project-level config. Dispatcher, budget, and
other subsystems receive typed config objects — they never parse loomstack.yaml
directly.

Only the ``budget`` section is fully specified here. Additional sections will
be added as each subsystem is implemented.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


class BudgetConfig(BaseModel):
    """
    Budget caps for the budget subsystem.

    Sourced from two places (in priority order):
    1. Environment: ``GLOBAL_DAILY_CAP_USD`` overrides ``global_daily_cap``.
    2. ``loomstack.yaml::budget_daily_usd`` mapping.

    The ``budget_daily_usd`` map may contain a ``global`` key (treated as the
    global cap) plus per-tier keys (``code_worker``, ``architect``, etc.).
    """

    tier_caps: dict[str, float] = Field(default_factory=dict)
    global_daily_cap: float = float("inf")

    @field_validator("tier_caps")
    @classmethod
    def caps_non_negative(cls, v: dict[str, float]) -> dict[str, float]:
        for tier, cap in v.items():
            if cap < 0:
                raise ValueError(f"tier cap for {tier!r} must be >= 0, got {cap}")
        return v

    @field_validator("global_daily_cap")
    @classmethod
    def global_cap_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"global_daily_cap must be >= 0, got {v}")
        return v

    @classmethod
    def from_yaml_section(cls, raw: dict[str, Any]) -> BudgetConfig:
        """
        Build from the ``budget_daily_usd`` dict in loomstack.yaml.

        The ``global`` key, if present, becomes ``global_daily_cap``.
        All other keys are tier caps.
        The ``GLOBAL_DAILY_CAP_USD`` env var overrides the yaml global cap.
        """
        tier_caps = {k: float(v) for k, v in raw.items() if k != "global"}
        yaml_global = float(raw["global"]) if "global" in raw else float("inf")
        env_global_str = os.environ.get("GLOBAL_DAILY_CAP_USD")
        global_cap = float(env_global_str) if env_global_str else yaml_global
        return cls(tier_caps=tier_caps, global_daily_cap=global_cap)


class LoomstackConfig(BaseModel):
    """Parsed loomstack.yaml. Additional sections added as subsystems grow."""

    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoomstackConfig:
        budget_raw: dict[str, Any] = data.get("budget_daily_usd", {})
        budget = BudgetConfig.from_yaml_section(budget_raw)
        return cls(budget=budget)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when loomstack.yaml cannot be read or parsed."""


def load_config(path: Path) -> LoomstackConfig:
    """
    Read and parse a loomstack.yaml synchronously.

    Called once at daemon startup before the async event loop is running hot,
    so sync I/O is acceptable here.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    try:
        data: dict[str, Any] = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a YAML mapping, got {type(data).__name__}")

    return LoomstackConfig.from_dict(data)
