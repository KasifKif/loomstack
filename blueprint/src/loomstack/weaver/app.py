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

    from loomstack.weaver.routes.chat import router as chat_router
    from loomstack.weaver.routes.tasks import router as tasks_router

    app.include_router(tasks_router)
    app.include_router(chat_router)

    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index() -> dict[str, str]:
        return {"status": "ok", "service": "weaver"}

    return app
