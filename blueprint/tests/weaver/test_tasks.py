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
        return WeaverSettings(
            loomstack_project_dir=str(test_plan_file.parent),
            data_dir=str(test_plan_file.parent / ".weaver-data"),
        )

    app.dependency_overrides[get_settings] = override_settings
    return TestClient(app)


def test_list_tasks_includes_status(client: TestClient) -> None:
    """GET /api/tasks returns the parsed plan with status."""
    with patch(
        "loomstack.weaver.routes.tasks.derive_status", new_callable=AsyncMock
    ) as mock_derive:
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


def test_get_task_detail_success(client: TestClient) -> None:
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


def test_tasks_page_renders_html(client: TestClient) -> None:
    """GET /tasks renders the HTML task list page."""
    with patch(
        "loomstack.weaver.routes.tasks.derive_status", new_callable=AsyncMock
    ) as mock_derive:
        mock_derive.side_effect = [TaskStatus.DONE, TaskStatus.IN_PROGRESS]

        response = client.get("/tasks")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

        # Check for task IDs and badge text
        html = response.text
        assert "TP-001" in html
        assert "TP-002" in html
        assert "done" in html
        assert "in_progress" in html
        assert "Dependency Graph" in html


def test_tasks_page_htmx_partial(client: TestClient) -> None:
    """GET /tasks returns only the tbody partial for HTMX requests."""
    with patch(
        "loomstack.weaver.routes.tasks.derive_status", new_callable=AsyncMock
    ) as mock_derive:
        mock_derive.side_effect = [TaskStatus.DONE, TaskStatus.IN_PROGRESS]

        response = client.get(
            "/tasks", headers={"HX-Request": "true", "HX-Target": "task-table-body"}
        )
        assert response.status_code == 200
        html = response.text

        # Should contain rows but NOT the full page structure
        assert "TP-001" in html
        assert "<tr" in html
        assert "<html" not in html
        assert "Dependency Graph" not in html


def test_task_detail_html_partial(client: TestClient) -> None:
    """GET /tasks/<id> returns the HTML detail partial."""
    with (
        patch("loomstack.weaver.routes.tasks.derive_status", new_callable=AsyncMock) as mock_status,
        patch("loomstack.weaver.routes.tasks.derive_run_meta", new_callable=AsyncMock) as mock_meta,
    ):
        mock_status.return_value = TaskStatus.DONE
        mock_meta.return_value = RunMeta(status=TaskStatus.DONE)

        response = client.get("/tasks/TP-001")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

        html = response.text
        assert "TP-001" in html
        assert "Run Metadata" in html


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
        return WeaverSettings(
            loomstack_project_dir=str(empty_dir),
            data_dir=str(empty_dir / ".weaver-data"),
        )

    app.dependency_overrides[get_settings] = override_settings
    client = TestClient(app)

    response = client.get("/api/tasks")
    assert response.status_code == 404


def test_list_tasks_missing_loomstack_dir(test_plan_file: Path) -> None:
    """GET /api/tasks returns PENDING for all tasks if .loomstack/ is missing."""
    app = create_app()

    def override_settings() -> WeaverSettings:
        # test_plan_file.parent exists but has no .loomstack/
        return WeaverSettings(
            loomstack_project_dir=str(test_plan_file.parent),
            data_dir=str(test_plan_file.parent / ".weaver-data"),
        )

    app.dependency_overrides[get_settings] = override_settings
    client = TestClient(app)

    response = client.get("/api/tasks")
    assert response.status_code == 200
    data = response.json()
    for task in data["tasks"]:
        assert task["status"] == "pending"


def test_list_tasks_parse_error(tmp_path: Path) -> None:
    """GET /api/tasks returns 422 if PLAN.md is malformed."""
    plan_path = tmp_path / "PLAN.md"
    plan_path.write_text("## Task: TP-001\nmalformed: [yaml")

    app = create_app()

    def override_settings() -> WeaverSettings:
        return WeaverSettings(
            loomstack_project_dir=str(tmp_path),
            data_dir=str(tmp_path / ".weaver-data"),
        )

    app.dependency_overrides[get_settings] = override_settings
    client = TestClient(app)

    response = client.get("/api/tasks")
    assert response.status_code == 422


def test_task_id_invalid_format_rejected(client: TestClient) -> None:
    """Task IDs not matching [A-Z]{2,4}-\\d+ pattern are rejected with 400."""
    for bad_id in ["lowercase-001", "TOOLONG-001", "TP_001", "A-1inject"]:
        response = client.get(f"/api/tasks/{bad_id}")
        assert response.status_code == 400, f"Expected 400 for {bad_id!r}"
