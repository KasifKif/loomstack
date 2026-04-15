"""Tests for loomstack.weaver.routes.tasks."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from loomstack.core.state import RunMeta, TaskStatus
from loomstack.weaver.app import create_app
from loomstack.weaver.config import WeaverSettings, get_settings


@pytest.fixture
def test_plan_file(tmp_path: Path) -> Path:
    """Create a temporary PLAN.md for testing."""
    plan_content = """# Test Project

## Task: TP-001 First task
role: code_worker
acceptance:
  ci: passes
tags: [test]

## Task: TP-002 Second task
role: code_worker
acceptance:
  ci: passes
"""
    plan_path = tmp_path / "PLAN.md"
    plan_path.write_text(plan_content)
    return plan_path


@pytest.fixture
def client(test_plan_file: Path) -> TestClient:
    """Return a TestClient with overridden settings."""
    app = create_app()

    def override_settings() -> WeaverSettings:
        return WeaverSettings(loomstack_project_dir=str(test_plan_file.parent))

    app.dependency_overrides[get_settings] = override_settings
    return TestClient(app)


@pytest.mark.asyncio
async def test_list_tasks_includes_status(client: TestClient) -> None:
    """GET /api/tasks returns the parsed plan with status."""
    with patch("loomstack.weaver.routes.tasks.derive_status") as mock_derive:
        # Mock statuses for two tasks
        mock_derive.side_effect = [TaskStatus.DONE, TaskStatus.IN_PROGRESS]

        response = client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()

        assert data["title"] == "Test Project"
        assert len(data["tasks"]) == 2

        assert data["tasks"][0]["task_id"] == "TP-001"
        assert data["tasks"][0]["status"] == "done"

        assert data["tasks"][1]["task_id"] == "TP-002"
        assert data["tasks"][1]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_get_task_detail_success(client: TestClient) -> None:
    """GET /api/tasks/<id> returns full task detail."""
    mock_run_meta = RunMeta(
        status=TaskStatus.DONE, tier="code_worker", retry_count=0, cost_usd=0.05
    )

    with (
        patch("loomstack.weaver.routes.tasks.derive_status", new_callable=AsyncMock) as mock_status,
        patch("loomstack.weaver.routes.tasks.derive_run_meta", new_callable=AsyncMock) as mock_meta,
    ):
        mock_status.return_value = TaskStatus.DONE
        mock_meta.return_value = mock_run_meta

        response = client.get("/api/tasks/TP-001")
        assert response.status_code == 200
        data = response.json()

        assert data["task_id"] == "TP-001"
        assert data["status"] == "done"
        assert data["run_meta"]["cost_usd"] == 0.05
        assert data["role"] == "code_worker"


def test_get_task_detail_not_found(client: TestClient) -> None:
    """GET /api/tasks/<id> returns 404 if task doesn't exist."""
    response = client.get("/api/tasks/TP-999")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_list_tasks_not_found(test_plan_file: Path) -> None:
    """GET /api/tasks returns 404 if PLAN.md is missing."""
    app = create_app()
    empty_dir = test_plan_file.parent / "empty"
    empty_dir.mkdir()

    def override_settings() -> WeaverSettings:
        return WeaverSettings(loomstack_project_dir=str(empty_dir))

    app.dependency_overrides[get_settings] = override_settings
    client = TestClient(app)

    response = client.get("/api/tasks")
    assert response.status_code == 404


def test_list_tasks_parse_error(tmp_path: Path) -> None:
    """GET /api/tasks returns 500 if PLAN.md is malformed."""
    plan_path = tmp_path / "PLAN.md"
    plan_path.write_text("## Task: TP-001\nmalformed: [yaml")

    app = create_app()

    def override_settings() -> WeaverSettings:
        return WeaverSettings(loomstack_project_dir=str(tmp_path))

    app.dependency_overrides[get_settings] = override_settings
    client = TestClient(app)

    response = client.get("/api/tasks")
    assert response.status_code == 500
