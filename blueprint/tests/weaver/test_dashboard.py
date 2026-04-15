"""Tests for loomstack.weaver.routes.dashboard."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from loomstack.weaver.app import create_app
from loomstack.weaver.config import WeaverSettings, get_settings
from loomstack.weaver.routes.dashboard import TaskCounts, _count_tasks, _read_status_from_run_file
from loomstack.weaver.routes.health import GX10Status

# ---------------------------------------------------------------------------
# _read_status_from_run_file
# ---------------------------------------------------------------------------


def test_read_status_extracts_from_frontmatter(tmp_path: Any) -> None:
    run_file = tmp_path / "LS-001.md"
    run_file.write_text("---\nstatus: done\ntier: code_worker\n---\nSome content\n")
    assert _read_status_from_run_file(run_file) == "done"


def test_read_status_returns_pending_for_no_status(tmp_path: Any) -> None:
    run_file = tmp_path / "LS-001.md"
    run_file.write_text("---\ntier: code_worker\n---\n")
    assert _read_status_from_run_file(run_file) == "pending"


def test_read_status_returns_pending_for_missing_file(tmp_path: Any) -> None:
    assert _read_status_from_run_file(tmp_path / "nonexistent.md") == "pending"


# ---------------------------------------------------------------------------
# _count_tasks
# ---------------------------------------------------------------------------


def test_count_tasks_no_plan(tmp_path: Any) -> None:
    counts, approvals = _count_tasks(str(tmp_path))
    assert counts.pending == 0
    assert approvals == 0


def test_count_tasks_with_plan_and_runs(tmp_path: Any) -> None:
    # Write a minimal PLAN.md
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "# Test Plan\n\n"
        "## Task: LS-001 Do thing one\n"
        "role: code_worker\n"
        "acceptance:\n  ci: passes\n\n"
        "## Task: LS-002 Do thing two\n"
        "role: code_worker\n"
        "human_review: true\n"
        "acceptance:\n  ci: passes\n\n"
        "## Task: LS-003 Do thing three\n"
        "role: architect\n"
        "acceptance:\n  ci: passes\n\n"
    )

    # Create run files for LS-001 (done) and LS-002 (in_progress)
    runs = tmp_path / ".loomstack" / "runs"
    runs.mkdir(parents=True)
    (runs / "LS-001.md").write_text("---\nstatus: done\n---\n")
    (runs / "LS-002.md").write_text("---\nstatus: in_progress\n---\n")

    counts, approvals = _count_tasks(str(tmp_path))
    assert counts.done == 1
    assert counts.in_progress == 1
    assert counts.pending == 1  # LS-003 has no run file
    assert approvals == 1  # LS-002 has human_review, not done, no approval file


def test_count_tasks_approval_clears_pending(tmp_path: Any) -> None:
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "# Test\n\n"
        "## Task: LS-001 Review this\n"
        "role: architect\n"
        "human_review: true\n"
        "acceptance:\n  ci: passes\n\n"
    )
    runs = tmp_path / ".loomstack" / "runs"
    runs.mkdir(parents=True)
    (runs / "LS-001.md").write_text("---\nstatus: proposed\n---\n")

    # Approval file exists
    approvals = tmp_path / ".loomstack" / "approvals"
    approvals.mkdir(parents=True)
    (approvals / "LS-001").write_text("")

    counts, pending = _count_tasks(str(tmp_path))
    assert pending == 0  # approved, so not pending


# ---------------------------------------------------------------------------
# Dashboard route
# ---------------------------------------------------------------------------


def _make_client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: WeaverSettings()
    return TestClient(app)


def test_dashboard_renders() -> None:
    client = _make_client()
    healthy = GX10Status(
        is_healthy=True,
        model_id="qwen3-coder-next",
        slots_active=1,
        slots_total=4,
        context_window=32768,
    )
    with (
        patch(
            "loomstack.weaver.routes.dashboard.fetch_gx10_status",
            new_callable=AsyncMock,
            return_value=healthy,
        ),
        patch(
            "loomstack.weaver.routes.dashboard._read_ledger_entries",
            return_value=[],
        ),
        patch(
            "loomstack.weaver.routes.dashboard._count_tasks",
            return_value=(TaskCounts(done=3, pending=2), 1),
        ),
    ):
        resp = client.get("/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Dashboard" in resp.text
    assert "Online" in resp.text
    assert "3 done" in resp.text
    assert "2 pending" in resp.text
