"""Standalone admin UI for routing, model state and component health."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .snapshot import build_snapshot

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


def _static_version() -> str:
    """Return a simple asset version so browsers reload changed UI files."""
    candidates = [_STATIC_DIR / "app.js", _STATIC_DIR / "app.css", _STATIC_DIR / "index.html"]
    latest_mtime = max(int(path.stat().st_mtime) for path in candidates if path.exists())
    return str(latest_mtime)

app = FastAPI(
    title="GPU Hub Ops UI",
    version="0.1.0",
    description="Independent admin UI for routing, wake/sleep state and component health.",
)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    """Simple health check for the admin service itself."""
    return {"status": "ok"}


@app.get("/api/overview", tags=["ops"])
async def overview() -> JSONResponse:
    """Return the aggregated snapshot consumed by the browser UI."""
    return JSONResponse(content=await build_snapshot())


@app.get("/", response_class=HTMLResponse, tags=["ui"])
async def index() -> HTMLResponse:
    """Serve the standalone admin dashboard."""
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        "__REFRESH_INTERVAL_SECONDS__",
        str(settings.refresh_interval_seconds),
    )
    html = html.replace("__STATIC_VERSION__", _static_version())
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
