"""The census web UI: FastAPI app factory, settings, database access and identity.

Run it with ``ethocensus-web`` (see :func:`main`). Settings come from the
environment (or a ``.env`` file):

``ETHOCENSUS_DB``           the census SQLite file (opened read-only)
``ETHOCENSUS_CURATION``     the curation SQLite file (labels, people, notes, audit;
                            default ``curation.sqlite`` next to the census)
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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import charts
from .curation import membership
from .curation import store as curation_store
from .filters import NONE

HERE = Path(__file__).parent


@dataclass(frozen=True)
class Settings:
    """Where the census is and how users are identified."""

    census_db: str
    user_header: str = "X-Forwarded-Email"
    require_user: bool = True
    curation_db: str | None = None

    @property
    def curation_path(self) -> str:
        return self.curation_db or os.path.join(
            os.path.dirname(os.path.abspath(self.census_db)), "curation.sqlite"
        )

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
            curation_db=os.environ.get("ETHOCENSUS_CURATION"),
        )


def open_census(path: str) -> sqlite3.Connection:
    """Open the census strictly read-only (the UI never writes to it)."""
    uri = f"file:{urllib.parse.quote(os.path.abspath(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _uri(path: str) -> str:
    return f"file:{urllib.parse.quote(os.path.abspath(path))}?mode=ro"


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: the census, read-only, with the curation file attached as ``cur``."""
    settings: Settings = request.app.state.settings
    conn = open_census(settings.census_db)
    conn.execute("ATTACH DATABASE ? AS cur", (_uri(settings.curation_path),))
    try:
        yield conn
    finally:
        conn.close()


def get_curation(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: the curation file, writable (for edits only)."""
    conn = curation_store.connect(request.app.state.settings.curation_path)
    try:
        yield conn
    finally:
        conn.close()


def refresh_membership(request: Request, cur: sqlite3.Connection) -> None:
    """Rebuild label and person membership after an edit."""
    census = open_census(request.app.state.settings.census_db)
    try:
        membership.ensure_fresh(census, cur, force=True)
    finally:
        census.close()


def back_to(url: str | None, fallback: str = "/", **message: str) -> RedirectResponse:
    """Redirect (303) to a local URL, with an optional ``notice`` or ``error`` message."""
    target = url if url and url.startswith("/") and not url.startswith("//") else fallback
    if message:
        joiner = "&" if "?" in target else "?"
        target = f"{target}{joiner}{urllib.parse.urlencode(message)}"
    return RedirectResponse(target, status_code=303)


def current_user(request: Request) -> str | None:
    return request.headers.get(request.app.state.settings.user_header)


def render(request: Request, name: str, **context) -> HTMLResponse:
    """Render a template with the common context (user, request)."""
    templates: Jinja2Templates = request.app.state.templates
    message = {k: request.query_params.get(k) for k in ("notice", "error")}
    context = {"user": current_user(request), **message, **context}
    return templates.TemplateResponse(request, name, context)


def _same_origin(request: Request) -> bool:
    """Edits must come from this site's own pages (CSRF guard): Origin or Referer host."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    return urllib.parse.urlsplit(source).netloc == request.headers.get("host")


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
    from .routes import changes, files, labels, overview, people, runs, search

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

    curation_store.connect(settings.curation_path).close()
    try:
        census = open_census(settings.census_db)
        cur = curation_store.connect(settings.curation_path)
        if census.execute("SELECT 1 FROM sqlite_master WHERE name = 'db_summary'").fetchone():
            membership.ensure_fresh(census, cur)
        census.close()
        cur.close()
    except sqlite3.Error:  # no census yet: /health reports it
        pass

    @app.middleware("http")
    async def require_user(request: Request, call_next):
        open_path = request.url.path == "/health" or request.url.path.startswith("/static/")
        if settings.require_user and not open_path and not current_user(request):
            return HTMLResponse(
                "<h1>Not signed in</h1><p>This service is only reachable through the lab "
                "sign-in.</p>",
                status_code=401,
            )
        if request.method not in ("GET", "HEAD") and not _same_origin(request):
            return HTMLResponse("<h1>Refused</h1><p>Cross-site request.</p>", status_code=403)
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

    for module in (overview, runs, files, search, labels, people, changes):
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
