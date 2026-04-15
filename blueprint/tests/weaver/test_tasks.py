"""Tests for loomstack.weaver.routes.tasks."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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


def test_list_tasks_success(client: TestClient) -> None:
    """GET /api/tasks returns the parsed plan."""
    response = client.get("/api/tasks")
    assert response.status_code == 200
    data = response.json()

    assert data["title"] == "Test Project"
    assert len(data["tasks"]) == 1
    task = data["tasks"][0]
    assert task["task_id"] == "TP-001"
    assert task["role"] == "code_worker"
    assert task["acceptance"]["ci"] == "passes"


def test_list_tasks_not_found(test_plan_file: Path) -> None:
    """GET /api/tasks returns 404 if PLAN.md is missing."""
    app = create_app()

    # Point to a directory that definitely doesn't have PLAN.md
    empty_dir = test_plan_file.parent / "empty"
    empty_dir.mkdir()

    def override_settings() -> WeaverSettings:
        return WeaverSettings(loomstack_project_dir=str(empty_dir))

    app.dependency_overrides[get_settings] = override_settings
    client = TestClient(app)

    response = client.get("/api/tasks")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


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
    assert "failed to parse" in response.json()["detail"].lower()
