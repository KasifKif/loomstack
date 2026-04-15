"""FastAPI application factory for Weaver."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    """Build and return the Weaver FastAPI application."""
    app = FastAPI(title="Weaver", docs_url=None, redoc_url=None)

    templates_dir = _HERE / "templates"
    static_dir = _HERE / "static"

    from loomstack.weaver.routes.budget import router as budget_router
    from loomstack.weaver.routes.chat import router as chat_router
    from loomstack.weaver.routes.dashboard import router as dashboard_router
    from loomstack.weaver.routes.health import router as health_router
    from loomstack.weaver.routes.projects import router as projects_router
    from loomstack.weaver.routes.tasks import router as tasks_router

    app.include_router(dashboard_router)
    app.include_router(tasks_router)
    app.include_router(chat_router)
    app.include_router(health_router)
    app.include_router(budget_router)
    app.include_router(projects_router)

    app.state.templates = Jinja2Templates(directory=str(templates_dir))

    # Inject project list into every template so the sidebar can show a selector.
    from loomstack.weaver.config import get_settings, parse_project_dirs

    app.state.templates.env.globals["weaver_projects"] = lambda: list(
        parse_project_dirs(get_settings()).keys()
    )

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
