"""Tests for loomstack.weaver.routes.approvals."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loomstack.weaver.app import create_app
from loomstack.weaver.config import WeaverSettings, get_settings


@pytest.fixture
def test_plan_file(tmp_path: Path) -> Path:
    """Create a temporary PLAN.md with human_review tasks."""
    plan_content = """# Test Project

## Task: TP-001 Manual task
role: code_worker
human_review: true
acceptance:
  ci: passes

## Task: TP-002 Auto task
role: code_worker
acceptance:
  ci: passes

## Task: TP-003 Another manual task
role: code_worker
human_review: true
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


def test_approve_task_creates_file(client: TestClient, test_plan_file: Path) -> None:
    """POST /api/approve/{task_id} creates a marker file."""
    task_id = "TP-001"
    response = client.post(f"/api/approve/{task_id}")
    assert response.status_code == 201
    assert response.json() == {"status": "approved", "task_id": task_id}

    marker_path = test_plan_file.parent / ".loomstack" / "approvals" / task_id
    assert marker_path.exists()


def test_approve_task_idempotent(client: TestClient, test_plan_file: Path) -> None:
    """POST /api/approve/{task_id} is idempotent."""
    task_id = "TP-001"
    # First call
    client.post(f"/api/approve/{task_id}")
    # Second call
    response = client.post(f"/api/approve/{task_id}")
    assert response.status_code == 201
    assert response.json() == {"status": "approved", "task_id": task_id}


def test_list_pending_approvals(client: TestClient, test_plan_file: Path) -> None:
    """GET /api/pending-approvals returns tasks requiring review."""
    response = client.get("/api/pending-approvals")
    assert response.status_code == 200
    data = response.json()

    # TP-001 and TP-003 have human_review: true
    task_ids = [t["task_id"] for t in data["tasks"]]
    assert "TP-001" in task_ids
    assert "TP-003" in task_ids
    assert "TP-002" not in task_ids
    assert len(task_ids) == 2

    # Approve one
    client.post("/api/approve/TP-001")

    response = client.get("/api/pending-approvals")
    data = response.json()
    task_ids = [t["task_id"] for t in data["tasks"]]
    assert "TP-001" not in task_ids
    assert "TP-003" in task_ids
    assert len(task_ids) == 1


def test_approve_invalid_task_id_rejected(client: TestClient) -> None:
    """POST /api/approve with invalid task ID returns 400."""
    response = client.post("/api/approve/bad-id")
    assert response.status_code == 400
