"""
Budget enforcement and ledger accounting.

Two-phase API (mirrors the agent contract in CLAUDE.md):

    allowed = await budget.check(tier, estimated_usd, task_id)
    if not allowed:
        # requeue, notify operator — budget never decides for the caller
        ...
    await budget.charge(tier, actual_usd, task_id, model=..., tokens_in=..., tokens_out=...)

``check`` returns False (and populates ``last_exceeded``) if the call would
breach either a per-tier or global daily cap. The caller (dispatcher or higher)
decides what to do — requeue, page the operator, skip, etc.

``charge`` appends an entry to ``.loomstack/ledger.jsonl`` (portalocker write
for cross-process safety) and updates the in-memory running totals. Includes
model, tokens_in, tokens_out for cost breakdown in the ``loomstack cost`` CLI.

Daily caps reset at midnight UTC. The Budget object detects a day rollover on
each call and resets its in-memory counters, then re-reads the ledger to
confirm nothing was written by other processes during the gap.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import portalocker

if TYPE_CHECKING:
    from pathlib import Path

    from loomstack.core.config import BudgetConfig

    pass

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BudgetExceeded:
    """
    Returned by ``check`` when a call would breach a daily cap.

    The caller decides how to respond (requeue, notify operator, etc.).
    """

    def __init__(
        self,
        tier: str,
        cap_usd: float,
        spent_usd: float,
        estimated_usd: float,
        resets_at: datetime,
    ) -> None:
        self.tier = tier
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd
        self.estimated_usd = estimated_usd
        self.resets_at = resets_at

    def __str__(self) -> str:
        return (
            f"budget exceeded for tier={self.tier!r}: "
            f"spent={self.spent_usd:.4f} + estimated={self.estimated_usd:.4f} "
            f"> cap={self.cap_usd:.4f} (resets {self.resets_at.isoformat()})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_today() -> date:
    return datetime.now(tz=UTC).date()


def _next_midnight_utc() -> datetime:
    today = datetime.now(tz=UTC).date()
    from datetime import timedelta

    tomorrow = today + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Ledger I/O (runs in thread via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _append_ledger_sync(path: Path, entry: dict[str, object]) -> None:
    """Append one JSON line to the ledger file with an exclusive file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(path), mode="a", encoding="utf-8", timeout=10) as fh:
        fh.write(json.dumps(entry) + "\n")


def _read_ledger_sync(path: Path, day: date) -> tuple[dict[str, float], float]:
    """
    Read ledger and return (tier_spent, global_spent) for ``day``.

    Ignores lines that are malformed or from other days.
    """
    tier_spent: dict[str, float] = {}
    global_spent: float = 0.0

    if not path.exists():
        return tier_spent, global_spent

    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "charge":
                continue
            try:
                entry_day = datetime.fromisoformat(entry["ts"]).date()
            except (KeyError, ValueError):
                continue
            if entry_day != day:
                continue
            usd = float(entry.get("usd", 0.0))
            tier = str(entry.get("tier", "unknown"))
            tier_spent[tier] = tier_spent.get(tier, 0.0) + usd
            global_spent += usd

    return tier_spent, global_spent


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class Budget:
    """
    Stateful budget tracker for a single daemon lifecycle.

    Create via ``Budget.create()`` — the async factory loads today's spend
    from the ledger before the event loop starts dispatching tasks.
    """

    def __init__(self, config: BudgetConfig, ledger_path: Path) -> None:
        self._config = config
        self._ledger_path = ledger_path
        self._lock = asyncio.Lock()
        self._day = _utc_today()
        self._tier_spent: dict[str, float] = {}
        self._global_spent: float = 0.0

    @classmethod
    async def create(cls, config: BudgetConfig, ledger_path: Path) -> Budget:
        """Async factory: instantiate and load today's spend from the ledger."""
        budget = cls(config, ledger_path)
        await budget._reload_from_ledger()
        return budget

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(
        self, tier: str, estimated_usd: float, task_id: str
    ) -> BudgetExceeded | None:
        """
        Return ``None`` if the call is within budget, or a ``BudgetExceeded``
        value if it would breach either the tier cap or the global daily cap.

        Does not write to the ledger. The caller decides what to do on
        a non-None return (requeue, notify operator, etc.).
        """
        async with self._lock:
            await self._maybe_rollover()
            return self._check_caps(tier, estimated_usd)

    async def charge(
        self,
        tier: str,
        actual_usd: float,
        task_id: str,
        *,
        model: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """
        Record actual spend: append to ledger and update in-memory totals.

        Uses portalocker for cross-process safety on the ledger file.
        ``model``, ``tokens_in``, ``tokens_out`` are stored for the cost CLI.
        """
        entry: dict[str, object] = {
            "ts": _now_iso(),
            "tier": tier,
            "task_id": task_id,
            "usd": actual_usd,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "type": "charge",
        }
        async with self._lock:
            await self._maybe_rollover()
            await asyncio.to_thread(_append_ledger_sync, self._ledger_path, entry)
            self._tier_spent[tier] = self._tier_spent.get(tier, 0.0) + actual_usd
            self._global_spent += actual_usd

    async def daily_spend(self, tier: str | None = None) -> float:
        """
        Return today's spend in USD.

        If ``tier`` is given, returns spend for that tier only.
        If ``tier`` is None, returns total global spend.
        """
        async with self._lock:
            await self._maybe_rollover()
            if tier is not None:
                return self._tier_spent.get(tier, 0.0)
            return self._global_spent

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_caps(self, tier: str, estimated_usd: float) -> BudgetExceeded | None:
        """Return a BudgetExceeded if either cap is breached; None otherwise. Must hold _lock."""
        tier_cap = self._config.tier_caps.get(tier)
        if tier_cap is not None:
            spent = self._tier_spent.get(tier, 0.0)
            if spent + estimated_usd > tier_cap:
                return BudgetExceeded(
                    tier=tier,
                    cap_usd=tier_cap,
                    spent_usd=spent,
                    estimated_usd=estimated_usd,
                    resets_at=_next_midnight_utc(),
                )
        if self._global_spent + estimated_usd > self._config.global_daily_cap:
            return BudgetExceeded(
                tier=tier,
                cap_usd=self._config.global_daily_cap,
                spent_usd=self._global_spent,
                estimated_usd=estimated_usd,
                resets_at=_next_midnight_utc(),
            )
        return None

    async def _maybe_rollover(self) -> None:
        """If UTC day has advanced, reset counters and re-read the ledger. Must hold _lock."""
        today = _utc_today()
        if today != self._day:
            self._day = today
            self._tier_spent = {}
            self._global_spent = 0.0
            await self._reload_from_ledger_locked()

    async def _reload_from_ledger(self) -> None:
        async with self._lock:
            await self._reload_from_ledger_locked()

    async def _reload_from_ledger_locked(self) -> None:
        """Read ledger for current day via thread pool. Must hold _lock."""
        tier_spent, global_spent = await asyncio.to_thread(
            _read_ledger_sync, self._ledger_path, self._day
        )
        self._tier_spent = tier_spent
        self._global_spent = global_spent
