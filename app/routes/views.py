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


# NOTE: the /skapa generate funnel was removed with the diffusion render path.
# Lärbild is now an authored-SVG library: assets are imported (see
# scripts/import_skola_svgs.py), not generated through the UI. Only the Bibliotek
# browse + review views remain.


# ── Public read-only views (bilder.sibbamala.com) ───────────────────────────
# A SEPARATE blueprint registered only by the public-read-only factory path. It
# serves ONLY the Bibliotek browse shell at / and /bibliotek. The served HTML has
# a tiny flag injected so the Bibliotek SPA renders its read-only chrome (no
# review controls, no review-filter tabs).

# Marker the public shell injects so the no-build React frontend knows it is the
# internet-facing instance. Placed just before </head>.
_PUBLIC_FLAG_SCRIPT = "<script>window.__LARBILD_PUBLIC_READONLY__=true;</script>"


def _public_index_html() -> str:
    """Read static/index.html and inject the public-read-only flag.

    Read-only: only serves the Bibliotek shell, with one ``<script>`` line added
    so the SPA hides the owner-only controls. The file itself is never modified
    on disk.
    """
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if _PUBLIC_FLAG_SCRIPT in html:
        return html
    if "</head>" in html:
        return html.replace("</head>", f"  {_PUBLIC_FLAG_SCRIPT}\n</head>", 1)
    # No <head> (shouldn't happen) — prepend so the flag is still set first.
    return _PUBLIC_FLAG_SCRIPT + html


def register_public_views(app) -> None:
    """Register the read-only browse views on the public instance.

    Builds a dedicated ``public_views`` blueprint exposing GET ``/`` and GET
    ``/bibliotek`` only — both serving the flag-injected Bibliotek shell — and
    repoints Flask's ``/static`` handler at the repo-root ``static/`` dir (so the
    vendored React/Babel + CSS + JS still load).
    """
    public_views_bp = Blueprint("public_views", __name__)

    @public_views_bp.record_once
    def _point_static(setup_state):  # noqa: ANN202 — Flask hook
        a = setup_state.app
        if Path(a.static_folder or "").resolve() != _STATIC_DIR:
            a.static_folder = str(_STATIC_DIR)

    @public_views_bp.get("/")
    def public_index():  # noqa: ANN202
        return _public_index_html()

    @public_views_bp.get("/bibliotek")
    def public_bibliotek():  # noqa: ANN202
        return _public_index_html()

    app.register_blueprint(public_views_bp)
