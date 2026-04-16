from __future__ import annotations

import asyncio
import datetime
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

from loomstack.weaver.config import WeaverSettings, get_data_dir, get_settings
from loomstack.weaver.store import JsonStore

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["git-projects"])


class ProjectCreate(BaseModel):
    git_url: str


class Project(BaseModel):
    id: str
    name: str
    git_url: str
    local_path: str
    is_active: bool = False
    cloned_at: str  # ISO 8601
    has_plan: bool = False


def get_project_store(
    settings: Annotated[WeaverSettings, Depends(get_settings)],
) -> JsonStore[Project]:
    data_dir = get_data_dir(settings)
    return JsonStore(data_dir, "projects.json", Project)


_GIT_URL_RE = re.compile(
    r"^(https?://|git://|ssh://|git@)[^\s]+$",
)


def _validate_git_url(url: str) -> None:
    """Reject URLs that don't look like valid git remotes."""
    if not _GIT_URL_RE.match(url):
        raise HTTPException(
            status_code=422,
            detail="Invalid git URL — must start with https://, http://, git://, ssh://, or git@",
        )


def extract_repo_name(url: str) -> str:
    return url.split("/")[-1].removesuffix(".git")


@router.get("/projects", response_class=HTMLResponse)
async def projects_page(
    request: Request,
    store: Annotated[JsonStore[Project], Depends(get_project_store)],
) -> Any:
    projects = await store.load_all()
    templates = cast("Jinja2Templates", request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "projects_manage.html",
        {
            "active": "projects",
            "git_projects": sorted(projects.values(), key=lambda p: p.name),
        },
    )


@router.post("/api/git-projects")
async def clone_project(
    request: Request,
    settings: Annotated[WeaverSettings, Depends(get_settings)],
    store: Annotated[JsonStore[Project], Depends(get_project_store)],
) -> Any:
    form = await request.form()
    url = str(form.get("git_url", "")).strip()
    if not url:
        raise HTTPException(status_code=422, detail="Git URL is required")
    _validate_git_url(url)

    repo_name = extract_repo_name(url)
    data_dir = get_data_dir(settings)
    dest = data_dir / "workspaces" / repo_name

    if dest.exists():
        raise HTTPException(status_code=409, detail=f"Project directory {repo_name} already exists")

    dest.parent.mkdir(parents=True, exist_ok=True)

    logger.info("cloning_repo", url=url, dest=str(dest))
    process = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        url,
        str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        logger.error("clone_failed", url=url, stderr=stderr.decode())
        raise HTTPException(status_code=500, detail=f"Git clone failed: {stderr.decode()}")

    has_plan = (dest / "PLAN.md").exists()

    project = Project(
        id=str(uuid.uuid4()),
        name=repo_name,
        git_url=url,
        local_path=str(dest),
        cloned_at=datetime.datetime.now(datetime.UTC).isoformat(),
        has_plan=has_plan,
    )

    await store.upsert(project.id, project)

    if request.headers.get("HX-Request"):
        templates = cast("Jinja2Templates", request.app.state.templates)
        return templates.TemplateResponse(
            request,
            "project_row_partial.html",
            {"project": project},
        )

    return project


@router.post("/api/git-projects/{id}/activate")
async def activate_project(
    id: str,
    request: Request,
    store: Annotated[JsonStore[Project], Depends(get_project_store)],
) -> Any:
    projects = await store.load_all()
    if id not in projects:
        raise HTTPException(status_code=404, detail="Project not found")
    for p in projects.values():
        p.is_active = p.id == id
    await store.save_all(projects)
    if request.headers.get("HX-Request"):
        templates = cast("Jinja2Templates", request.app.state.templates)
        return templates.TemplateResponse(
            request, "project_row_partial.html", {"project": projects[id]}
        )
    return projects[id]


@router.post("/api/git-projects/{id}/pull")
async def pull_project(
    id: str,
    request: Request,
    store: Annotated[JsonStore[Project], Depends(get_project_store)],
) -> Any:
    project = await store.get(id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        project.local_path,
        "pull",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Git pull failed: {stderr.decode()}")
    project.has_plan = (Path(project.local_path) / "PLAN.md").exists()
    await store.upsert(project.id, project)
    if request.headers.get("HX-Request"):
        templates = cast("Jinja2Templates", request.app.state.templates)
        return templates.TemplateResponse(request, "project_row_partial.html", {"project": project})
    return project


@router.delete("/api/git-projects/{id}")
async def delete_project(
    id: str,
    request: Request,
    store: Annotated[JsonStore[Project], Depends(get_project_store)],
) -> Any:
    deleted = await store.delete(id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found")

    if request.headers.get("HX-Request"):
        return HTMLResponse(content="")

    return {"status": "deleted", "id": id}
