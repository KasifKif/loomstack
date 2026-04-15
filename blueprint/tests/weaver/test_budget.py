"""Tests for loomstack.weaver.routes.budget."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from loomstack.weaver.app import create_app
from loomstack.weaver.config import WeaverSettings, get_settings
from loomstack.weaver.routes.budget import (
    _entries_for_day,
    _read_ledger_entries,
    _tier_breakdown,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    tier: str = "code_worker",
    usd: float = 0.05,
    task_id: str = "T-001",
    model: str = "qwen3-coder",
    ts: str | None = None,
    tokens_in: int = 100,
    tokens_out: int = 50,
) -> dict[str, Any]:
    if ts is None:
        ts = datetime.now(tz=UTC).isoformat()
    return {
        "ts": ts,
        "tier": tier,
        "task_id": task_id,
        "usd": usd,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "type": "charge",
    }


def _write_ledger(entries: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Unit tests — ledger reading helpers
# ---------------------------------------------------------------------------


def test_read_ledger_entries_empty_file() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
    assert _read_ledger_entries(f.name) == []


def test_read_ledger_entries_filters_non_charge() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "info", "msg": "started"}) + "\n")
        f.write(json.dumps(_make_entry()) + "\n")
    entries = _read_ledger_entries(f.name)
    assert len(entries) == 1


def test_read_ledger_entries_skips_malformed_json() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("not json\n")
        f.write(json.dumps(_make_entry()) + "\n")
    entries = _read_ledger_entries(f.name)
    assert len(entries) == 1


def test_read_ledger_entries_missing_file() -> None:
    assert _read_ledger_entries("/nonexistent/ledger.jsonl") == []


def test_entries_for_day_filters_correctly() -> None:
    today = datetime.now(tz=UTC).date()
    yesterday = today - timedelta(days=1)
    entries = [
        _make_entry(ts=datetime(today.year, today.month, today.day, 10, tzinfo=UTC).isoformat()),
        _make_entry(
            ts=datetime(yesterday.year, yesterday.month, yesterday.day, 10, tzinfo=UTC).isoformat()
        ),
    ]
    result = _entries_for_day(entries, today)
    assert len(result) == 1


def test_tier_breakdown_sums() -> None:
    entries = [
        _make_entry(tier="code_worker", usd=0.05),
        _make_entry(tier="code_worker", usd=0.03),
        _make_entry(tier="architect", usd=0.50),
    ]
    breakdown = _tier_breakdown(entries)
    assert breakdown == {"code_worker": 0.08, "architect": 0.50}


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------


def _make_client(ledger_entries: list[dict[str, Any]]) -> tuple[TestClient, Path]:
    tmp = Path(tempfile.mkdtemp())
    loomstack_dir = tmp / "project"
    ledger_path = loomstack_dir / ".loomstack" / "ledger.jsonl"
    _write_ledger(ledger_entries, ledger_path)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: WeaverSettings(
        loomstack_project_dir=str(loomstack_dir),
    )
    return TestClient(app), tmp


def test_get_budget_today_with_entries() -> None:
    today_ts = datetime.now(tz=UTC).isoformat()
    entries = [
        _make_entry(tier="code_worker", usd=0.05, ts=today_ts),
        _make_entry(tier="code_worker", usd=0.03, ts=today_ts),
        _make_entry(tier="architect", usd=0.50, ts=today_ts),
    ]
    client, _ = _make_client(entries)
    resp = client.get("/api/budget/today")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_usd"] == 0.58
    assert data["per_tier"]["code_worker"] == 0.08
    assert data["per_tier"]["architect"] == 0.5


def test_get_budget_today_empty_ledger() -> None:
    client, _ = _make_client([])
    resp = client.get("/api/budget/today")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_usd"] == 0.0
    assert data["per_tier"] == {}


def test_get_budget_history_returns_days() -> None:
    today = datetime.now(tz=UTC)
    yesterday = today - timedelta(days=1)
    entries = [
        _make_entry(usd=0.10, ts=today.isoformat()),
        _make_entry(usd=0.20, ts=yesterday.isoformat()),
    ]
    client, _ = _make_client(entries)
    resp = client.get("/api/budget/history?days=3")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    # Most recent day should be last
    assert data[-1]["total_usd"] == 0.10


def test_get_budget_recent_returns_newest_first() -> None:
    entries = [
        _make_entry(task_id="T-001", ts="2026-04-14T10:00:00+00:00"),
        _make_entry(task_id="T-002", ts="2026-04-14T11:00:00+00:00"),
        _make_entry(task_id="T-003", ts="2026-04-14T12:00:00+00:00"),
    ]
    client, _ = _make_client(entries)
    resp = client.get("/api/budget/recent?n=2")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["task_id"] == "T-003"  # newest first
    assert data[1]["task_id"] == "T-002"


def test_get_budget_recent_no_ledger() -> None:
    # No entries at all
    client, _ = _make_client([])
    resp = client.get("/api/budget/recent")

    assert resp.status_code == 200
    assert resp.json() == []
