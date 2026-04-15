"""Tests for loomstack.weaver.routes.projects and config.parse_project_dirs."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from loomstack.weaver.app import create_app
from loomstack.weaver.config import WeaverSettings, get_settings, parse_project_dirs

# ---------------------------------------------------------------------------
# parse_project_dirs unit tests
# ---------------------------------------------------------------------------

_PLAN = (
    "# Test Plan\n\n"
    "## Task: TS-001 First task\n"
    "role: code_worker\n"
    "acceptance:\n"
    "  pr_opens_against: main\n"
    "  ci: passes\n"
    "notes: test\n"
)


def test_parse_project_dirs_primary_only() -> None:
    settings = WeaverSettings(loomstack_project_dir="/home/user/meshcord")
    dirs = parse_project_dirs(settings)
    assert list(dirs.keys()) == ["meshcord"]
    assert dirs["meshcord"] == "/home/user/meshcord"


def test_parse_project_dirs_multiple() -> None:
    settings = WeaverSettings(
        loomstack_project_dir="/home/user/meshcord",
        loomstack_project_dirs="/home/user/fateweaver, /home/user/loomstack",
    )
    assert list(parse_project_dirs(settings).keys()) == ["meshcord", "fateweaver", "loomstack"]


def test_parse_project_dirs_duplicate_name_primary_wins() -> None:
    settings = WeaverSettings(
        loomstack_project_dir="/home/user/meshcord",
        loomstack_project_dirs="/home/other/meshcord,  ,",
    )
    dirs = parse_project_dirs(settings)
    assert len(dirs) == 1
    assert dirs["meshcord"] == "/home/user/meshcord"


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------


def _make_project(entries: list[dict] | None = None) -> Path:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "PLAN.md").write_text(_PLAN, encoding="utf-8")
    if entries is not None:
        ledger = tmp / ".loomstack" / "ledger.jsonl"
        ledger.parent.mkdir(parents=True)
        with ledger.open("w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
    return tmp


def _client(primary: Path, extras: list[Path] | None = None) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: WeaverSettings(
        loomstack_project_dir=str(primary),
        loomstack_project_dirs=",".join(str(p) for p in (extras or [])),
    )
    return TestClient(app)


def test_list_projects_single() -> None:
    proj = _make_project()
    resp = _client(proj).get("/api/projects")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == proj.name


def test_list_projects_multiple() -> None:
    p1, p2 = _make_project(), _make_project()
    names = [d["name"] for d in _client(p1, [p2]).get("/api/projects").json()]
    assert p1.name in names and p2.name in names


def test_get_project_tasks_returns_plan() -> None:
    proj = _make_project()
    resp = _client(proj).get(f"/api/{proj.name}/tasks")
    assert resp.status_code == 200
    assert resp.json()["tasks"][0]["task_id"] == "TS-001"


def test_get_project_tasks_unknown_404() -> None:
    assert _client(_make_project()).get("/api/doesnotexist/tasks").status_code == 404


def test_get_project_tasks_missing_plan_404() -> None:
    proj = _make_project()
    (proj / "PLAN.md").unlink()
    assert _client(proj).get(f"/api/{proj.name}/tasks").status_code == 404


def test_get_project_budget_today() -> None:
    ts = datetime.now(tz=UTC).isoformat()
    proj = _make_project(
        entries=[
            {
                "type": "charge",
                "ts": ts,
                "tier": "code_worker",
                "usd": 0.10,
                "task_id": "TS-001",
                "model": "qwen",
                "tokens_in": 100,
                "tokens_out": 50,
            }
        ]
    )
    resp = _client(proj).get(f"/api/{proj.name}/budget/today")
    assert resp.status_code == 200
    assert resp.json()["total_usd"] == 0.10


def test_get_project_budget_unknown_404() -> None:
    assert _client(_make_project()).get("/api/doesnotexist/budget/today").status_code == 404


def test_project_tasks_page_renders_html() -> None:
    proj = _make_project()
    resp = _client(proj).get(f"/projects/{proj.name}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "TS-001" in resp.text
