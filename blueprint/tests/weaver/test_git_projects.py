import pytest
import json
import anyio
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from loomstack.weaver.app import create_app
from loomstack.weaver.config import WeaverSettings, get_settings
from loomstack.weaver.routes.git_projects import Project

@pytest.fixture
def test_data_dir(tmp_path):
    return tmp_path / "weaver_data"

@pytest.fixture
def settings(test_data_dir):
    return WeaverSettings(
        data_dir=str(test_data_dir),
        loomstack_project_dir="."
    )

@pytest.fixture
def client(settings):
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)

def test_projects_page_empty(client):
    response = client.get("/projects")
    assert response.status_code == 200
    assert "No projects cloned yet" in response.text

@pytest.mark.asyncio
async def test_clone_project(client, test_data_dir):
    git_url = "https://github.com/user/test-repo.git"
    repo_name = "test-repo"
    
    # Mock asyncio.create_subprocess_exec
    mock_process = AsyncMock()
    mock_process.returncode = 0
    mock_process.communicate.return_value = (b"", b"")
    
    with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
        # We also need to mock the existence of PLAN.md after clone
        def side_effect(*args, **kwargs):
            (test_data_dir / "workspaces" / repo_name / "PLAN.md").parent.mkdir(parents=True, exist_ok=True)
            (test_data_dir / "workspaces" / repo_name / "PLAN.md").touch()
            return mock_process

        mock_exec.side_effect = side_effect
        
        response = client.post("/api/git-projects", json={"git_url": git_url})
        
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == repo_name
        assert data["git_url"] == git_url
        assert data["has_plan"] is True
        
        # Verify persistence
        projects_file = test_data_dir / "projects.json"
        assert projects_file.exists()
        stored_data = json.loads(projects_file.read_text())
        assert data["id"] in stored_data

@pytest.mark.asyncio
async def test_activate_project(client, test_data_dir):
    # Setup a project
    project_id = "test-id"
    project = Project(
        id=project_id,
        name="test-repo",
        git_url="https://github.com/user/test-repo.git",
        local_path="/tmp/test-repo",
        is_active=False,
        cloned_at="2026-01-01T00:00:00Z"
    )
    
    test_data_dir.mkdir(parents=True, exist_ok=True)
    projects_file = test_data_dir / "projects.json"
    projects_file.write_text(json.dumps({project_id: project.model_dump(mode="json")}))
    
    response = client.post(f"/api/git-projects/{project_id}/activate")
    assert response.status_code == 200
    assert response.json()["is_active"] is True
    
    # Verify persistence
    stored_data = json.loads(projects_file.read_text())
    assert stored_data[project_id]["is_active"] is True

@pytest.mark.asyncio
async def test_pull_project(client, test_data_dir):
    project_id = "test-id"
    local_path = test_data_dir / "workspaces" / "test-repo"
    local_path.mkdir(parents=True, exist_ok=True)
    
    project = Project(
        id=project_id,
        name="test-repo",
        git_url="https://github.com/user/test-repo.git",
        local_path=str(local_path),
        is_active=True,
        cloned_at="2026-01-01T00:00:00Z"
    )
    
    projects_file = test_data_dir / "projects.json"
    projects_file.write_text(json.dumps({project_id: project.model_dump(mode="json")}))
    
    mock_process = AsyncMock()
    mock_process.returncode = 0
    mock_process.communicate.return_value = (b"Already up to date.", b"")
    
    with patch("asyncio.create_subprocess_exec", return_value=mock_process):
        response = client.post(f"/api/git-projects/{project_id}/pull")
        assert response.status_code == 200
        assert response.json()["name"] == "test-repo"

@pytest.mark.asyncio
async def test_delete_project(client, test_data_dir):
    project_id = "test-id"
    project = Project(
        id=project_id,
        name="test-repo",
        git_url="https://github.com/user/test-repo.git",
        local_path="/tmp/test-repo",
        is_active=True,
        cloned_at="2026-01-01T00:00:00Z"
    )
    
    test_data_dir.mkdir(parents=True, exist_ok=True)
    projects_file = test_data_dir / "projects.json"
    projects_file.write_text(json.dumps({project_id: project.model_dump(mode="json")}))
    
    response = client.delete(f"/api/git-projects/{project_id}")
    assert response.status_code == 200
    
    # Verify persistence
    stored_data = json.loads(projects_file.read_text())
    assert project_id not in stored_data
