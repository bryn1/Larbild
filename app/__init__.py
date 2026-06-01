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

from flask import Flask

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
    _register_blueprints(app)
    return app


def _init_database(app: Flask) -> None:
    """Create the SQLite DB and run pending migrations on boot (lb-ah2).

    Reconcile-on-boot: idempotent, so every gunicorn worker calling this is
    safe. ``INIT_DB`` (default True) lets a test skip filesystem writes; tests
    that exercise the DB instead point ``DB_PATH`` at a temp file.
    """
    if not app.config.get("INIT_DB", True):
        return
    from .db import init_db

    init_db(app.config["DB_PATH"])


def _register_blueprints(app: Flask) -> None:
    """Register every SFW blueprint.

    The health blueprint is the foundation and always present. Later phases
    append their blueprints here (library, jobs, film, lessons, …). Imports
    stay local to keep the factory's import graph flat and side-effect free at
    package import time.
    """
    from .routes.health import health_bp
    from .routes.taxonomy import taxonomy_bp

    app.register_blueprint(health_bp)
    # lb-582: the LGR22 taxonomy single source of truth, exposed at
    # GET /api/taxonomy so the frontend bootstraps from the backend.
    app.register_blueprint(taxonomy_bp)
