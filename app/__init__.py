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

    _register_blueprints(app)
    return app


def _register_blueprints(app: Flask) -> None:
    """Register every SFW blueprint.

    The health blueprint is the foundation and always present. Later phases
    append their blueprints here (library, jobs, taxonomy, film, lessons, …).
    Imports stay local to keep the factory's import graph flat and side-effect
    free at package import time.
    """
    from .routes.health import health_bp

    app.register_blueprint(health_bp)
