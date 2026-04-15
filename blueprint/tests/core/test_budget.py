"""Tests for blueprint/src/loomstack/core/budget.py."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from loomstack.core.budget import Budget, BudgetExceeded, _read_ledger_sync
from loomstack.core.config import BudgetConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_config(
    tier_caps: dict[str, float] | None = None,
    global_cap: float = float("inf"),
) -> BudgetConfig:
    return BudgetConfig(
        tier_caps=tier_caps or {},
        global_daily_cap=global_cap,
    )


def write_ledger(path: Path, entries: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# BudgetConfig
# ---------------------------------------------------------------------------


class TestBudgetConfig:
    def test_defaults(self) -> None:
        c = BudgetConfig()
        assert c.tier_caps == {}
        assert c.global_daily_cap == float("inf")

    def test_negative_tier_cap_raises(self) -> None:
        with pytest.raises(Exception):
            BudgetConfig(tier_caps={"code_worker": -1.0})

    def test_negative_global_cap_raises(self) -> None:
        with pytest.raises(Exception):
            BudgetConfig(global_daily_cap=-0.01)

    def test_from_yaml_section_basic(self) -> None:
        raw = {"code_worker": 5.0, "architect": 2.0, "global": 10.0}
        c = BudgetConfig.from_yaml_section(raw)
        assert c.tier_caps == {"code_worker": 5.0, "architect": 2.0}
        assert c.global_daily_cap == 10.0

    def test_from_yaml_section_no_global(self) -> None:
        c = BudgetConfig.from_yaml_section({"code_worker": 3.0})
        assert c.global_daily_cap == float("inf")

    def test_from_yaml_section_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GLOBAL_DAILY_CAP_USD", "7.5")
        c = BudgetConfig.from_yaml_section({"global": 100.0})
        assert c.global_daily_cap == 7.5

    def test_from_yaml_section_empty(self) -> None:
        c = BudgetConfig.from_yaml_section({})
        assert c.tier_caps == {}
        assert c.global_daily_cap == float("inf")


# ---------------------------------------------------------------------------
# Ledger reader
# ---------------------------------------------------------------------------


class TestReadLedger:
    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        path.write_text("")
        tier_spent, global_spent = _read_ledger_sync(path, date.today())
        assert tier_spent == {}
        assert global_spent == 0.0

    def test_missing_file(self, tmp_path: Path) -> None:
        tier_spent, global_spent = _read_ledger_sync(
            tmp_path / "ledger.jsonl", date.today()
        )
        assert global_spent == 0.0

    def test_reads_today_charges(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        today = datetime.now(tz=UTC).date()
        ts = datetime(today.year, today.month, today.day, 10, 0, tzinfo=UTC).isoformat()
        write_ledger(
            path,
            [
                {"ts": ts, "tier": "code_worker", "task_id": "T-001", "usd": 0.05, "type": "charge"},
                {"ts": ts, "tier": "code_worker", "task_id": "T-002", "usd": 0.03, "type": "charge"},
                {"ts": ts, "tier": "architect", "task_id": "T-003", "usd": 0.10, "type": "charge"},
            ],
        )
        tier_spent, global_spent = _read_ledger_sync(path, today)
        assert tier_spent["code_worker"] == pytest.approx(0.08)
        assert tier_spent["architect"] == pytest.approx(0.10)
        assert global_spent == pytest.approx(0.18)

    def test_ignores_other_days(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        write_ledger(
            path,
            [
                {
                    "ts": "2020-01-01T00:00:00+00:00",
                    "tier": "code_worker",
                    "task_id": "T-001",
                    "usd": 9.99,
                    "type": "charge",
                }
            ],
        )
        tier_spent, global_spent = _read_ledger_sync(path, date.today())
        assert global_spent == 0.0

    def test_ignores_non_charge_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        today = datetime.now(tz=UTC).date()
        ts = datetime(today.year, today.month, today.day, tzinfo=UTC).isoformat()
        write_ledger(
            path,
            [{"ts": ts, "tier": "code_worker", "task_id": "T-001", "usd": 5.0, "type": "check"}],
        )
        _, global_spent = _read_ledger_sync(path, today)
        assert global_spent == 0.0

    def test_ignores_malformed_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        path.write_text("not json\n{incomplete\n")
        _, global_spent = _read_ledger_sync(path, date.today())
        assert global_spent == 0.0


# ---------------------------------------------------------------------------
# Budget.check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBudgetCheck:
    async def _make(
        self, tmp_path: Path, **config_kwargs: object
    ) -> Budget:
        config = make_config(**config_kwargs)  # type: ignore[arg-type]
        return await Budget.create(config, tmp_path / ".loomstack" / "ledger.jsonl")

    async def test_check_passes_under_cap(self, tmp_path: Path) -> None:
        b = await self._make(tmp_path, tier_caps={"code_worker": 5.0})
        assert await b.check("code_worker", 1.0, "T-001") is None

    async def test_check_passes_no_cap(self, tmp_path: Path) -> None:
        b = await self._make(tmp_path)
        assert await b.check("code_worker", 999.0, "T-001") is None

    async def test_check_returns_exceeded_tier_cap(self, tmp_path: Path) -> None:
        b = await self._make(tmp_path, tier_caps={"code_worker": 1.0})
        await b.charge("code_worker", 0.80, "T-001")
        result = await b.check("code_worker", 0.30, "T-002")
        assert isinstance(result, BudgetExceeded)
        assert result.tier == "code_worker"
        assert result.cap_usd == 1.0
        assert result.spent_usd == pytest.approx(0.80)

    async def test_check_returns_exceeded_global_cap(self, tmp_path: Path) -> None:
        b = await self._make(tmp_path, global_cap=1.0)
        await b.charge("code_worker", 0.70, "T-001")
        await b.charge("architect", 0.20, "T-002")
        result = await b.check("reviewer", 0.20, "T-003")
        assert isinstance(result, BudgetExceeded)
        assert result.cap_usd == 1.0
        assert result.spent_usd == pytest.approx(0.90)

    async def test_check_exact_cap_passes(self, tmp_path: Path) -> None:
        b = await self._make(tmp_path, tier_caps={"code_worker": 1.0})
        await b.charge("code_worker", 0.70, "T-001")
        assert await b.check("code_worker", 0.30, "T-002") is None

    async def test_check_exceeds_exact_cap_returns_exceeded(self, tmp_path: Path) -> None:
        b = await self._make(tmp_path, tier_caps={"code_worker": 1.0})
        await b.charge("code_worker", 0.70, "T-001")
        assert isinstance(await b.check("code_worker", 0.31, "T-002"), BudgetExceeded)


# ---------------------------------------------------------------------------
# Budget.charge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBudgetCharge:
    async def test_charge_updates_memory(self, tmp_path: Path) -> None:
        config = make_config()
        b = await Budget.create(config, tmp_path / "ledger.jsonl")
        await b.charge("code_worker", 0.05, "T-001")
        assert await b.daily_spend("code_worker") == pytest.approx(0.05)
        assert await b.daily_spend() == pytest.approx(0.05)

    async def test_charge_writes_ledger(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"
        b = await Budget.create(make_config(), ledger)
        await b.charge("architect", 0.12, "T-007", model="qwen3-coder", tokens_in=100, tokens_out=50)
        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["tier"] == "architect"
        assert entry["task_id"] == "T-007"
        assert entry["usd"] == pytest.approx(0.12)
        assert entry["type"] == "charge"
        assert entry["model"] == "qwen3-coder"
        assert entry["tokens_in"] == 100
        assert entry["tokens_out"] == 50

    async def test_charge_accumulates(self, tmp_path: Path) -> None:
        b = await Budget.create(make_config(), tmp_path / "ledger.jsonl")
        await b.charge("code_worker", 0.10, "T-001")
        await b.charge("code_worker", 0.20, "T-002")
        assert await b.daily_spend("code_worker") == pytest.approx(0.30)
        assert await b.daily_spend() == pytest.approx(0.30)

    async def test_charge_multi_tier(self, tmp_path: Path) -> None:
        b = await Budget.create(make_config(), tmp_path / "ledger.jsonl")
        await b.charge("code_worker", 0.10, "T-001")
        await b.charge("architect", 0.50, "T-002")
        assert await b.daily_spend("code_worker") == pytest.approx(0.10)
        assert await b.daily_spend("architect") == pytest.approx(0.50)
        assert await b.daily_spend() == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# Budget.daily_spend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBudgetDailySpend:
    async def test_zero_on_fresh(self, tmp_path: Path) -> None:
        b = await Budget.create(make_config(), tmp_path / "ledger.jsonl")
        assert await b.daily_spend() == 0.0
        assert await b.daily_spend("code_worker") == 0.0

    async def test_unknown_tier_returns_zero(self, tmp_path: Path) -> None:
        b = await Budget.create(make_config(), tmp_path / "ledger.jsonl")
        assert await b.daily_spend("nonexistent_tier") == 0.0


# ---------------------------------------------------------------------------
# Ledger loading on startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBudgetStartupLoad:
    async def test_loads_existing_ledger(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"
        today = datetime.now(tz=UTC).date()
        ts = datetime(today.year, today.month, today.day, tzinfo=UTC).isoformat()
        write_ledger(
            ledger,
            [
                {"ts": ts, "tier": "code_worker", "task_id": "T-001", "usd": 0.25, "type": "charge"},
                {"ts": ts, "tier": "architect", "task_id": "T-002", "usd": 0.50, "type": "charge"},
            ],
        )
        b = await Budget.create(make_config(), ledger)
        assert await b.daily_spend("code_worker") == pytest.approx(0.25)
        assert await b.daily_spend("architect") == pytest.approx(0.50)
        assert await b.daily_spend() == pytest.approx(0.75)

    async def test_check_uses_loaded_spend(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.jsonl"
        today = datetime.now(tz=UTC).date()
        ts = datetime(today.year, today.month, today.day, tzinfo=UTC).isoformat()
        write_ledger(
            ledger,
            [{"ts": ts, "tier": "code_worker", "task_id": "T-001", "usd": 0.90, "type": "charge"}],
        )
        b = await Budget.create(
            make_config(tier_caps={"code_worker": 1.0}), ledger
        )
        assert isinstance(await b.check("code_worker", 0.20, "T-002"), BudgetExceeded)


# ---------------------------------------------------------------------------
# Rollover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBudgetRollover:
    async def test_rollover_resets_counters(self, tmp_path: Path) -> None:
        b = await Budget.create(make_config(), tmp_path / "ledger.jsonl")
        await b.charge("code_worker", 5.0, "T-001")
        assert await b.daily_spend() == pytest.approx(5.0)

        # Simulate midnight rollover by patching _utc_today
        from datetime import timedelta
        tomorrow = datetime.now(tz=UTC).date() + timedelta(days=1)
        with patch("loomstack.core.budget._utc_today", return_value=tomorrow):
            assert await b.daily_spend() == 0.0

    async def test_rollover_allows_previously_capped_tier(self, tmp_path: Path) -> None:
        b = await Budget.create(
            make_config(tier_caps={"code_worker": 1.0}),
            tmp_path / "ledger.jsonl",
        )
        await b.charge("code_worker", 0.90, "T-001")

        from datetime import timedelta
        tomorrow = datetime.now(tz=UTC).date() + timedelta(days=1)
        with patch("loomstack.core.budget._utc_today", return_value=tomorrow):
            assert await b.check("code_worker", 0.80, "T-002") is None


# ---------------------------------------------------------------------------
# BudgetExceeded
# ---------------------------------------------------------------------------


class TestBudgetExceeded:
    def test_fields(self) -> None:
        resets_at = datetime(2026, 4, 14, tzinfo=UTC)
        exc = BudgetExceeded(
            tier="architect",
            cap_usd=2.0,
            spent_usd=1.80,
            estimated_usd=0.30,
            resets_at=resets_at,
        )
        assert exc.tier == "architect"
        assert exc.cap_usd == 2.0
        assert exc.spent_usd == 1.80
        assert exc.estimated_usd == 0.30
        assert exc.resets_at == resets_at
        assert "architect" in str(exc)
        assert "2.0" in str(exc)
