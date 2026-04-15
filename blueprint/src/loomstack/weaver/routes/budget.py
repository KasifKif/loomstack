"""Read-only budget API routes for Weaver.

Reads directly from the ledger.jsonl file — never writes.
Does not depend on the Budget class (which is designed for the daemon lifecycle).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, cast

import structlog
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi.responses import Response

from loomstack.weaver.config import WeaverSettings, get_settings

log = structlog.get_logger(__name__)

router = APIRouter(tags=["budget"])


# ---------------------------------------------------------------------------
# Ledger reading (sync — called via route, cheap for typical ledger sizes)
# ---------------------------------------------------------------------------


def _read_ledger_entries(ledger_path: str) -> list[dict[str, Any]]:
    """Read all valid charge entries from the ledger file."""
    from pathlib import Path

    path = Path(ledger_path)
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("ledger_malformed_line", path=ledger_path, line=raw[:120])
                continue
            if entry.get("type") != "charge":
                continue
            entries.append(entry)
    return entries


def _entries_for_day(entries: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    """Filter entries to a specific UTC day."""
    result: list[dict[str, Any]] = []
    for entry in entries:
        try:
            entry_day = datetime.fromisoformat(entry["ts"]).date()
        except (KeyError, ValueError):
            continue
        if entry_day == day:
            result.append(entry)
    return result


def _tier_breakdown(entries: list[dict[str, Any]]) -> dict[str, float]:
    """Sum USD per tier from a list of entries."""
    breakdown: dict[str, float] = {}
    for entry in entries:
        tier = str(entry.get("tier", "unknown"))
        usd = float(entry.get("usd", 0.0))
        breakdown[tier] = breakdown.get(tier, 0.0) + usd
    return breakdown


def _ledger_path(settings: WeaverSettings) -> str:
    from pathlib import Path

    return str(Path(settings.loomstack_project_dir) / ".loomstack" / "ledger.jsonl")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TodayBudgetResponse(BaseModel):
    date: str
    total_usd: float
    per_tier: dict[str, float]


class DailyHistoryEntry(BaseModel):
    date: str
    total_usd: float


class RecentChargeEntry(BaseModel):
    ts: str
    tier: str
    task_id: str
    usd: float
    model: str
    tokens_in: int
    tokens_out: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/budget/today", response_model=TodayBudgetResponse)
async def get_budget_today(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> TodayBudgetResponse:
    """Return today's spend total and per-tier breakdown."""
    entries = _read_ledger_entries(_ledger_path(settings))
    today = datetime.now(tz=UTC).date()
    today_entries = _entries_for_day(entries, today)
    breakdown = _tier_breakdown(today_entries)
    total = sum(breakdown.values())
    return TodayBudgetResponse(
        date=today.isoformat(),
        total_usd=round(total, 4),
        per_tier={k: round(v, 4) for k, v in sorted(breakdown.items())},
    )


@router.get("/api/budget/history")
async def get_budget_history(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> list[DailyHistoryEntry]:
    """Return daily spend totals for the last N days."""
    entries = _read_ledger_entries(_ledger_path(settings))
    today = datetime.now(tz=UTC).date()
    result: list[DailyHistoryEntry] = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        day_entries = _entries_for_day(entries, day)
        total = sum(float(e.get("usd", 0.0)) for e in day_entries)
        result.append(DailyHistoryEntry(date=day.isoformat(), total_usd=round(total, 4)))
    return result


@router.get("/api/budget/recent")
async def get_budget_recent(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
    n: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[RecentChargeEntry]:
    """Return the most recent N charge entries."""
    entries = _read_ledger_entries(_ledger_path(settings))
    recent = entries[-n:] if len(entries) > n else entries
    recent.reverse()  # newest first
    return [
        RecentChargeEntry(
            ts=str(e.get("ts", "")),
            tier=str(e.get("tier", "unknown")),
            task_id=str(e.get("task_id", "")),
            usd=float(e.get("usd", 0.0)),
            model=str(e.get("model", "")),
            tokens_in=int(e.get("tokens_in", 0)),
            tokens_out=int(e.get("tokens_out", 0)),
        )
        for e in recent
    ]


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@router.get("/budget", include_in_schema=False)
async def budget_page(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    """Render the budget dashboard page."""
    entries = _read_ledger_entries(_ledger_path(settings))
    today_date = datetime.now(tz=UTC).date()
    today_entries = _entries_for_day(entries, today_date)
    breakdown = _tier_breakdown(today_entries)
    total = sum(breakdown.values())
    today_data = TodayBudgetResponse(
        date=today_date.isoformat(),
        total_usd=round(total, 4),
        per_tier={k: round(v, 4) for k, v in sorted(breakdown.items())},
    )

    recent = entries[-50:] if len(entries) > 50 else entries
    recent.reverse()
    recent_data = [
        RecentChargeEntry(
            ts=str(e.get("ts", "")),
            tier=str(e.get("tier", "unknown")),
            task_id=str(e.get("task_id", "")),
            usd=float(e.get("usd", 0.0)),
            model=str(e.get("model", "")),
            tokens_in=int(e.get("tokens_in", 0)),
            tokens_out=int(e.get("tokens_out", 0)),
        )
        for e in recent
    ]

    return cast(
        "Response",
        request.app.state.templates.TemplateResponse(
            request,
            "budget.html",
            {"active": "budget", "today": today_data, "recent": recent_data},
        ),
    )


@router.get("/api/budget-fragment", include_in_schema=False)
async def budget_fragment(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> Response:
    """HTMX partial: re-renders the today's spend panel."""
    entries = _read_ledger_entries(_ledger_path(settings))
    today_date = datetime.now(tz=UTC).date()
    today_entries = _entries_for_day(entries, today_date)
    breakdown = _tier_breakdown(today_entries)
    total = sum(breakdown.values())
    today_data = TodayBudgetResponse(
        date=today_date.isoformat(),
        total_usd=round(total, 4),
        per_tier={k: round(v, 4) for k, v in sorted(breakdown.items())},
    )
    return cast(
        "Response",
        request.app.state.templates.TemplateResponse(
            request,
            "budget_fragment.html",
            {"today": today_data},
        ),
    )
