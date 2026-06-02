"""Frontend page routes — serve the Lärbild single-page views (lb-eee).

The frontend is a no-build React app (Babel-in-browser, matching the design's
serving model): ``static/index.html`` loads the design-system CSS, React from a
CDN, and the Bibliotek scripts, which fetch live data from ``/api/library`` +
``/api/taxonomy``. This blueprint serves that HTML shell at the app root (and at
``/bibliotek``); the JS/CSS under ``/static`` are served by Flask's built-in
static route, which we repoint at the repo-root ``static/`` directory on
registration (the foundation app factory left it at the default ``app/static``).

Read-only: returns the static page; all data is pulled client-side from the SFW
read endpoints. No restricted view, import, or model lives here.
"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, send_from_directory

views_bp = Blueprint("views", __name__)

# Repo-root static/ holds index.html + css/ + js/ (one level above app/).
_STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


@views_bp.record_once
def _point_static_at_repo_root(setup_state) -> None:
    """Repoint the app's built-in ``/static`` handler at repo-root ``static/``.

    The foundation factory builds ``Flask(__name__)`` with the default
    ``app/static`` folder, which does not exist — the design system CSS and the
    frontend JS live at the repo root. Setting ``static_folder`` here (rather
    than editing the factory's ``Flask()`` call) keeps this self-contained to
    the lb-eee blueprint and merge-clean. Idempotent: a no-op if already set.
    """
    app = setup_state.app
    if Path(app.static_folder or "").resolve() != _STATIC_DIR:
        app.static_folder = str(_STATIC_DIR)


def _index_response():
    """Serve the Bibliotek SPA shell (static/index.html)."""
    return send_from_directory(_STATIC_DIR, "index.html")


@views_bp.get("/")
def index():
    """App root → the Bibliotek view (the default landing screen)."""
    return _index_response()


@views_bp.get("/bibliotek")
def bibliotek():
    """Explicit Bibliotek route (same SPA shell; the view owns its own nav)."""
    return _index_response()


@views_bp.get("/skapa")
def skapa():
    """Serve the "Skapa bild" funnel page (lb-e1p, P3.1).

    Its own no-build React shell (static/skapa.html): it loads the same design-
    system CSS and React CDN, plus the funnel scripts (skapa_data.js +
    skapa.jsx), which bootstrap from the live SoT endpoints (/api/taxonomy,
    /api/resolve-lora) and submit to /api/jobs. A separate shell keeps the
    Bibliotek SPA untouched. SFW-only: educational image funnel.
    """
    return send_from_directory(_STATIC_DIR, "skapa.html")
