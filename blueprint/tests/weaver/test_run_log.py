"""Tests for loomstack.weaver.routes.tasks run log view."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loomstack.weaver.app import create_app
from loomstack.weaver.config import WeaverSettings, get_settings


@pytest.fixture
def test_plan_file(tmp_path: Path) -> Path:
    """Create a temporary PLAN.md for testing."""
    plan_content = """# Test Project

## Task: TP-001 Log task
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


def test_view_run_log_success(client: TestClient, test_plan_file: Path) -> None:
    """GET /tasks/{id}/log renders the run log."""
    task_id = "TP-001"
    run_dir = test_plan_file.parent / ".loomstack" / "runs"
    run_dir.mkdir(parents=True)

    run_content = """---
status: done
tier: code_worker
cost_usd: 0.0521
pr_url: https://github.com/test/pr/1
---

# Run Log for TP-001

This is the log body.
- Item 1
- Item 2

```python
print("hello")
```
"""
    (run_dir / f"{task_id}.md").write_text(run_content)

    response = client.get(f"/tasks/{task_id}/log")
    assert response.status_code == 200
    html = response.text

    assert "Run Log: TP-001" in html
    assert "done" in html
    assert "$0.0521" in html
    assert "https://github.com/test/pr/1" in html
    assert "This is the log body." in html
    assert "<ul>" in html  # Check markdown rendering
    assert "<code>" in html


def test_view_run_log_in_progress_has_htmx(client: TestClient, test_plan_file: Path) -> None:
    """GET /tasks/{id}/log includes HTMX polling for in-progress tasks."""
    task_id = "TP-001"
    run_dir = test_plan_file.parent / ".loomstack" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)

    run_content = """---
status: in_progress
---
Working...
"""
    (run_dir / f"{task_id}.md").write_text(run_content)

    response = client.get(f"/tasks/{task_id}/log")
    assert response.status_code == 200
    html = response.text

    assert 'hx-trigger="every 10s"' in html
    assert f'hx-get="/tasks/{task_id}/log"' in html


def test_view_run_log_not_found(client: TestClient) -> None:
    """GET /tasks/{id}/log returns 404 if log file missing."""
    response = client.get("/tasks/TP-999/log")
    assert response.status_code == 404
