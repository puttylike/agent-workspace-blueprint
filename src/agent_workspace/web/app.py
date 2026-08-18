"""FastAPI application factory for the read-only Agent Ops dashboard."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .routes import SourceCache, create_router
from .wiki import WikiIndex


_PACKAGE_ROOT = Path(__file__).resolve().parent
_TEMPLATE_ROOT = _PACKAGE_ROOT / "templates"
_STATIC_ROOT = _PACKAGE_ROOT / "static"


def _member(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _nested(value: Any, *names: str) -> Any:
    current = value
    for name in names:
        current = _member(current, name)
        if current is None:
            return None
    return current


def _load_config(config: Any | None) -> Any | None:
    if config is not None and not isinstance(config, (str, os.PathLike)):
        return config
    config_path = config or os.environ.get("AGENT_OPS_CONFIG")
    if not config_path:
        return None
    try:
        from agent_workspace.config import load_app_config

        return load_app_config(Path(config_path))
    except Exception:
        # The web process still starts in a degraded, visibly unavailable mode.
        # Doctor/CLI owns detailed configuration diagnostics.
        return None


def _build_status_service(config: Any | None) -> Any | None:
    if config is None:
        return None
    try:
        from agent_workspace.controller.status_service import StatusService

        return StatusService.from_config(config)
    except Exception:
        return None


def _build_wiki_service(config: Any | None) -> WikiIndex | None:
    if config is None:
        return None
    knowledge_root = _member(config, "knowledge_root")
    database_path = _nested(config, "runtime", "sqlite_cache") or _member(
        config, "sqlite_cache"
    )
    if not knowledge_root or not database_path:
        return None
    return WikiIndex(knowledge_root, database_path)


def create_app(
    config: Any | None = None,
    *,
    status_service: Any | None = None,
    wiki_service: WikiIndex | None = None,
) -> FastAPI:
    """Create an app using injected readers or a validated private config.

    Dependency injection keeps fixtures and the public blueprint free of real
    workspace, repository, agent, and session identifiers.
    """

    loaded_config = _load_config(config)
    app = FastAPI(
        title="Agent Ops",
        version="0.1.0",
        description="Read-only observability for registered agent workspaces.",
        docs_url=None,
        redoc_url=None,
    )
    app.state.config = loaded_config
    app.state.status_service = status_service or _build_status_service(loaded_config)
    app.state.wiki_service = wiki_service or _build_wiki_service(loaded_config)
    app.state.source_cache = SourceCache()

    # Starlette's template helper selects HTML autoescaping for ``.html`` files.
    templates = Jinja2Templates(directory=str(_TEMPLATE_ROOT))
    app.mount("/static", StaticFiles(directory=str(_STATIC_ROOT)), name="static")
    app.include_router(create_router(templates))

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> HTMLResponse:
        if request.url.path.startswith("/api/"):
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=404, content={"detail": "Not found"})
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "request": request,
                "title": "Not found",
                "active": "",
                "read_only": True,
                "status_code": 404,
                "message": "The requested read-only view was not found.",
            },
            status_code=404,
        )

    return app


# Import-string deployments may provide AGENT_OPS_CONFIG. The CLI normally uses
# create_app with its already validated configuration object.
app = create_app()


__all__ = ["app", "create_app"]
