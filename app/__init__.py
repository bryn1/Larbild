"""Lärbild Flask backend — application factory.

Lärbild is the SFW educational sibling of imagegen. This package reuses
imagegen's backend *conventions* (env-driven config, blueprint registration,
session hardening, a persistent secret) but is an independent, SFW-only
codebase: it imports nothing from imagegen and contains no restricted route,
model, or feature. SFW from line one.

`create_app()` is the single entry point. Both `flask run`
(``FLASK_APP=app:create_app``) and gunicorn (``wsgi:app``) go through it.
Later phases register their own blueprints in :func:`_register_blueprints`.
"""
from __future__ import annotations

from typing import Mapping, Optional

from flask import Flask, jsonify

from .config import Config

__all__ = ["create_app"]


def create_app(overrides: Optional[Mapping] = None) -> Flask:
    """Build and configure a Lärbild Flask app.

    Args:
        overrides: optional mapping merged over the env-derived config after
            load. Tests pass e.g. ``{"TESTING": True}``.

    Returns:
        A fully wired Flask app with every SFW blueprint registered.
    """
    app = Flask(__name__)
    app.config.from_object(Config())
    if overrides:
        app.config.update(dict(overrides))

    _init_database(app)
    if app.config.get("PUBLIC_READONLY", False):
        _install_readonly_guard(app)
        _register_public_blueprints(app)
    else:
        _register_blueprints(app)
    return app


def _install_readonly_guard(app: Flask) -> None:
    """Belt-and-suspenders: reject every mutating method on the public instance.

    The public-read-only factory already declines to import or register any
    write/generate/admin/review blueprint (so that code is absent from the
    process, not merely hidden). This ``before_request`` is the second,
    independent layer: it short-circuits ANY request whose method is not a safe,
    read-only method (GET/HEAD/OPTIONS) with 405 before it can reach a view — so
    even if a mutating route were ever reachable, the method itself is refused.

    OPTIONS is allowed through so CORS/preflight and Flask's automatic-OPTIONS
    still answer; HEAD is GET-without-a-body. Everything else (POST/PUT/PATCH/
    DELETE/…) is a hard 405 with an ``Allow: GET, HEAD, OPTIONS`` header.
    """
    safe_methods = {"GET", "HEAD", "OPTIONS"}
    # The SINGLE public write the read-only instance permits: a visitor flagging
    # an image as wrong/broken (POST /api/flag, the crowdsource feedback path).
    # Everything else mutating stays a hard 405. This exemption is intentionally
    # an exact-path allowlist of ONE so no other write can slip through.
    write_exempt = {"/api/flag"}

    @app.before_request
    def _reject_mutations():  # noqa: ANN202 — Flask hook
        from flask import request

        if request.method in safe_methods:
            return None
        # Allow the one sanctioned public write (the flag endpoint) to proceed to
        # its hardened view; refuse every other mutating method.
        if request.method == "POST" and request.path in write_exempt:
            return None
        return (
            jsonify(error="read-only instance: method not allowed"),
            405,
            {"Allow": "GET, HEAD, OPTIONS"},
        )


def _register_public_blueprints(app: Flask) -> None:
    """Register ONLY the read-only browse blueprints (PUBLIC_READONLY mode).

    This is the internet-facing instance behind bilder.sibbamala.com. It serves
    exactly the Bibliotek browse experience over APPROVED images and nothing
    else. The generation/admin/review/regenerate/jobs/batch/films/lessons/
    portraits/TTS/LoRA/GPU blueprints are deliberately NEVER IMPORTED here — the
    privileged code is absent from this process, not conditionally hidden inside
    a registered blueprint.

    Registered (all GET-only):
      * health  — GET /healthz liveness.
      * taxonomy — GET /api/taxonomy (LGR22 subject/area tree, the browse SoT).
      * library — GET /api/library (tree + per-node counts) and
        GET /api/library/items (the gallery). In this mode both are forced to
        approved-only (review_ok = 1); the POST review + POST regenerate views
        are NOT attached to the blueprint (see app.routes.library).
      * media — GET /media/<file_path> (serve an approved artefact's bytes,
        path-traversal-safe).
      * views — GET / and /bibliotek (the SPA shell). /skapa (the generate
        funnel) is NOT registered here.

    Imports stay local so importing the package never drags in a privileged
    module at import time.
    """
    from .routes.flags import public_flag_bp
    from .routes.health import health_bp
    from .routes.library import library_bp
    from .routes.media import media_bp
    from .routes.taxonomy import taxonomy_bp
    from .routes.views import register_public_views

    app.register_blueprint(health_bp)
    app.register_blueprint(taxonomy_bp)
    app.register_blueprint(library_bp)
    app.register_blueprint(media_bp)
    # The ONE public write: POST /api/flag (visitor reports a wrong/broken image
    # in the crowdsource gallery). The LAN-only flags REVIEW blueprint is NOT
    # registered here — the public instance can write a flag but never read flag
    # state. The before_request guard above exempts exactly /api/flag.
    app.register_blueprint(public_flag_bp)
    # views: register only the read-only browse pages (/, /bibliotek) — never the
    # /skapa generate funnel. A dedicated helper builds a Skapa-free blueprint so
    # the generate page is not even routable on the public instance.
    register_public_views(app)


def _init_database(app: Flask) -> None:
    """Create the SQLite DB and run pending migrations on boot (lb-ah2).

    Reconcile-on-boot: idempotent, so every gunicorn worker calling this is
    safe. ``INIT_DB`` (default True) lets a test skip filesystem writes; tests
    that exercise the DB instead point ``DB_PATH`` at a temp file.

    After migrations, the storage reconcile pass runs (conservative + idempotent +
    best-effort — it never blocks boot):

    Full storage reconcile (lb-qtr): DB ↔ disk ↔ library three-way consistency —
    dangling library rows are flagged (soft ``missing_since``, never deleted),
    orphaned files + mis-sorted assets are reported. Bounded + skippable so it
    can't stall boot on a huge directory scan. Gated by ``RECONCILE_STORAGE``
    (default on; off under TESTING).
    """
    if app.config.get("PUBLIC_READONLY", False):
        # Public read-only instance: bind connect() to the shared DB for READING
        # only — no mkdir, no migrations, no reconcile (those all write, and this
        # process must never touch the DB the private :6868 instance owns).
        from .db import configure_db_path

        configure_db_path(app.config["DB_PATH"])
        return
    if not app.config.get("INIT_DB", True):
        return
    from .db import init_db
    from .reconcile import reconcile_storage_on_boot

    init_db(app.config["DB_PATH"])
    reconcile_storage_on_boot(app)


def _register_blueprints(app: Flask) -> None:
    """Register every SFW blueprint (the LAN-only authored-SVG library tool).

    Lärbild is an authored-SVG library: the diffusion/AI render path (jobs,
    batch, films, lessons-render, portraits, TTS, LoRA, GPU telemetry) was
    removed when the project committed permanently to authored SVG content. What
    remains is the curriculum-aligned gallery + browse, the owner's review +
    visitor-flag workflow, and the asset-lessons grouping over existing assets.

    The health blueprint is the foundation and always present. Imports stay local
    to keep the factory's import graph flat and side-effect free at package
    import time.
    """
    from .routes.health import health_bp
    from .routes.library import library_bp
    from .routes.media import media_bp
    from .routes.taxonomy import taxonomy_bp
    from .routes.views import views_bp

    app.register_blueprint(health_bp)
    # lb-582: the LGR22 taxonomy single source of truth, exposed at
    # GET /api/taxonomy so the frontend bootstraps from the backend.
    app.register_blueprint(taxonomy_bp)
    # lb-5rc: the auto-sorted library listing (taxonomy tree + per-node asset
    # counts) at GET /api/library — the Bibliotek 3-level browse source. The
    # node-level item listing (the gallery's source) is GET /api/library/items.
    app.register_blueprint(library_bp)
    # lb-gallery: serve a generated artefact's bytes at GET /media/<file_path>
    # (STORAGE_DIR-relative, path-traversal-safe). Without it the Bibliotek
    # gallery knows which images exist but every <img> 404s — the "counts update
    # but no images show" symptom. SFW: only reaches files under STORAGE_DIR.
    app.register_blueprint(media_bp)
    # lb-public-flags: the visitor-flag table's two surfaces — the public write
    # POST /api/flag (registered here too so it works on the LAN tool) and the
    # owner's LAN-only review API (GET /api/flags + POST /api/flags/<id>/resolve).
    from .routes.flags import flags_admin_bp, public_flag_bp

    app.register_blueprint(public_flag_bp)
    app.register_blueprint(flags_admin_bp)
    # lb-zdp: asset-lessons — group EXISTING library assets into a named, ordered
    # lesson at /api/asset-lessons*. Distinct routes + distinct tables
    # (asset_lessons / asset_lesson_items, migration 0009), reusing
    # library._row_to_item for the asset shape. Owner-only: registered here
    # (private LAN) but NOT in the public read-only factory above.
    from .routes.asset_lessons import asset_lessons_bp

    app.register_blueprint(asset_lessons_bp)
    # lb-eee: serve the no-build React frontend (the Bibliotek view) at / and
    # /bibliotek; repoints Flask's /static handler at the repo-root static/ dir.
    app.register_blueprint(views_bp)
