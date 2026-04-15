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

    from loomstack.weaver.routes.approvals import router as approvals_router
    from loomstack.weaver.routes.budget import router as budget_router
    from loomstack.weaver.routes.chat import router as chat_router
    from loomstack.weaver.routes.dashboard import router as dashboard_router
    from loomstack.weaver.routes.health import router as health_router
    from loomstack.weaver.routes.tasks import router as tasks_router

    app.include_router(dashboard_router)
    app.include_router(tasks_router)
    app.include_router(chat_router)
    app.include_router(health_router)
    app.include_router(budget_router)
    app.include_router(approvals_router)

    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
