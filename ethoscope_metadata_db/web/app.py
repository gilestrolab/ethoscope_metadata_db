"""The census web UI: FastAPI app factory, settings, database access and identity.

Run it with ``ethocensus-web`` (see :func:`main`). Settings come from the
environment (or a ``.env`` file):

``ETHOCENSUS_DB``           the census SQLite file (opened read-only)
``ETHOCENSUS_USER_HEADER``  header carrying the logged-in user, set by the SSO
                            proxy (default ``X-Forwarded-Email``)
``ETHOCENSUS_REQUIRE_USER`` ``1`` (default): refuse requests without that header.
                            Set ``0`` only for local development.

Identity is delegated to oauth2-proxy (OIDC with the lab's Keycloak): the app
must only be reachable through that proxy, which strips any client-supplied
copy of the header.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import charts
from .filters import NONE

HERE = Path(__file__).parent


@dataclass(frozen=True)
class Settings:
    """Where the census is and how users are identified."""

    census_db: str
    user_header: str = "X-Forwarded-Email"
    require_user: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        """Read settings from the environment, loading ``.env`` first if present."""
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:  # python-dotenv is optional
            pass
        return cls(
            census_db=os.environ["ETHOCENSUS_DB"],
            user_header=os.environ.get("ETHOCENSUS_USER_HEADER", "X-Forwarded-Email"),
            require_user=os.environ.get("ETHOCENSUS_REQUIRE_USER", "1") != "0",
        )


def open_census(path: str) -> sqlite3.Connection:
    """Open the census strictly read-only (the UI never writes to it)."""
    uri = f"file:{urllib.parse.quote(os.path.abspath(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one read-only census connection per request."""
    conn = open_census(request.app.state.settings.census_db)
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request) -> str | None:
    return request.headers.get(request.app.state.settings.user_header)


def render(request: Request, name: str, **context) -> HTMLResponse:
    """Render a template with the common context (user, request)."""
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request, name, {"user": current_user(request), **context})


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "–"


def _fmt_size(value) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1000
    return f"{size:,.1f} GB"


def _fmt_hours(value) -> str:
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return "–"
    return f"{hours / 24:.1f} d" if hours >= 48 else f"{hours:.1f} h"


def _fmt_mtime(value) -> str:
    try:
        return dt.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "–"


def create_app(settings: Settings) -> FastAPI:
    """Build the web app for one census file."""
    from .routes import files, overview, runs, search

    app = FastAPI(title="Ethoscope census", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(int=_fmt_int, size=_fmt_size, hours=_fmt_hours, mtime=_fmt_mtime)
    templates.env.globals.update(
        NONE=NONE,
        legend=charts.legend,
        SERIES_LIST=charts.SERIES,
        series_of=lambda category: charts.CATEGORY_SERIES.get(category, "orphan"),
    )
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def require_user(request: Request, call_next):
        open_path = request.url.path == "/health" or request.url.path.startswith("/static/")
        if settings.require_user and not open_path and not current_user(request):
            return HTMLResponse(
                "<h1>Not signed in</h1><p>This service is only reachable through the lab "
                "sign-in.</p>",
                status_code=401,
            )
        return await call_next(request)

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            conn = open_census(settings.census_db)
            row = conn.execute(
                "SELECT value FROM schema_info WHERE key = 'summarised_at'"
            ).fetchone()
            conn.close()
        except sqlite3.Error as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return JSONResponse({"ok": row is not None, "summarised_at": row[0] if row else None})

    for module in (overview, runs, files, search):
        app.include_router(module.router)
    return app


def main() -> None:
    """Console entry point ``ethocensus-web``: serve on localhost (behind the SSO proxy)."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="ethocensus-web", description="Census web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8095)
    args = parser.parse_args()
    uvicorn.run(create_app(Settings.from_env()), host=args.host, port=args.port)
