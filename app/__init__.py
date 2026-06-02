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
    _start_background_workers(app)
    return app


def _init_database(app: Flask) -> None:
    """Create the SQLite DB and run pending migrations on boot (lb-ah2).

    Reconcile-on-boot: idempotent, so every gunicorn worker calling this is
    safe. ``INIT_DB`` (default True) lets a test skip filesystem writes; tests
    that exercise the DB instead point ``DB_PATH`` at a temp file.

    After migrations, two reconcile passes run (both conservative + idempotent +
    best-effort — neither ever blocks boot):

    1. Orphaned ``running`` jobs (lb-qtr, partial): a render left ``running`` by a
       crashed/restarted worker is marked ``failed`` so the durable record
       reflects reality instead of a phantom in-flight job.
    2. Full storage reconcile (lb-qtr): DB ↔ disk ↔ library three-way
       consistency — dangling library rows are flagged (soft ``missing_since``,
       never deleted), orphaned files + mis-sorted assets are reported. Bounded +
       skippable so it can't stall boot on a huge directory scan. Gated by
       ``RECONCILE_STORAGE`` (default on; off under TESTING).
    """
    if not app.config.get("INIT_DB", True):
        return
    from .db import init_db
    from .jobs import reconcile_stuck_jobs
    from .reconcile import reconcile_storage_on_boot

    init_db(app.config["DB_PATH"])
    reconcile_stuck_jobs()
    reconcile_storage_on_boot(app)


def _register_blueprints(app: Flask) -> None:
    """Register every SFW blueprint.

    The health blueprint is the foundation and always present. Later phases
    append their blueprints here (library, jobs, film, lessons, …). Imports
    stay local to keep the factory's import graph flat and side-effect free at
    package import time.
    """
    from .gpu_telemetry import gpu_bp
    from .routes.batch import batch_bp
    from .routes.films import films_bp
    from .routes.health import health_bp
    from .routes.jobs import jobs_bp
    from .routes.library import library_bp
    from .routes.lora import lora_bp
    from .routes.portraits import portraits_bp
    from .routes.taxonomy import taxonomy_bp
    from .routes.tts import tts_bp
    from .routes.views import views_bp

    app.register_blueprint(health_bp)
    # lb-582: the LGR22 taxonomy single source of truth, exposed at
    # GET /api/taxonomy so the frontend bootstraps from the backend.
    app.register_blueprint(taxonomy_bp)
    # lb-5rc: the auto-sorted library listing (taxonomy tree + per-node asset
    # counts) at GET /api/library — the Bibliotek 3-level browse source.
    app.register_blueprint(library_bp)
    # lb-9tz: backend-authoritative LoRA resolution at POST/GET /api/resolve-lora
    # — the Skapa funnel sends only its (subject, imageType) selection; the
    # backend decides the LoRA and the client can never override it.
    app.register_blueprint(lora_bp)
    # lb-c2r: per-GPU telemetry + render-target registry at GET /api/gpus
    # (snapshot) and /api/gpus/stream (SSE) — backs the top-bar GPU pills and
    # the P4 `auto` router's input.
    app.register_blueprint(gpu_bp)
    # lb-3gi: render-job submission + progress — POST /api/jobs (enqueue, 202),
    # GET /api/jobs/<id> (state), GET /api/jobs/<id>/events (SSE). The LoRA is
    # never accepted from the client; the worker resolves it server-side.
    app.register_blueprint(jobs_bp)
    # lb-zht/lb-y9c: batch layer — POST /api/jobs/batch parses a CSV/txt/JSON
    # spec, validates every row against the taxonomy SoT (LoRA dropped), and fans
    # the valid rows out into N individual jobs through the lb-3gi bridge under
    # one batch_id; GET /api/jobs/batch/<id>[/events] rolls up aggregate progress
    # from the jobs table. Fan-out only enqueues — the render stays the worker's
    # deferred job.
    app.register_blueprint(batch_bp)
    # lb-ly1/lb-wdu/lb-xlr: Porträtt consent API — register a consented face
    # (under-18 requires guardian consent, computed server-side), gate every
    # compositor use (consentOk recomputed from the DB, else 403), and GDPR-
    # delete a reference (cascade files off disk + audit). The consent rule +
    # storage live in app.portraits; this blueprint is the thin HTTP layer.
    app.register_blueprint(portraits_bp)
    # lb-ayi: Swedish narration TTS (Piper sv-SE on VM600) — POST /api/tts
    # ({text, voice} -> audio/wav, voices sv-standard | sv-lattlast) backs the
    # film voice-over (lb-4vw) and the Skapa-film voice preview. GPU-independent.
    app.register_blueprint(tts_bp)
    # lb-4vw: film pipeline — POST /api/films (enqueue a storyboard→keyframes→
    # narration→ffmpeg-mux film, 202), GET /api/films/templates (the 6 film
    # templates), GET /api/films/<id>[/events] (state + SSE; a film is a jobs row
    # so it reuses the still-job SSE). The LoRA is backend-authoritative per
    # keyframe (SFW guard); live keyframe render is deferred (imagegen sweep).
    app.register_blueprint(films_bp)
    # lb-eee: serve the no-build React frontend (the Bibliotek view) at / and
    # /bibliotek; repoints Flask's /static handler at the repo-root static/ dir.
    app.register_blueprint(views_bp)


def _start_background_workers(app: Flask) -> None:
    """Start long-lived background daemons (lb-c2r).

    The GPU telemetry poller SSHes out to VM100/vm153, so it must NOT run under
    tests (it would hang/fail on the sandboxed CI host). It is gated on
    ``START_GPU_TELEMETRY`` — default on in production, default off when
    ``TESTING`` is set. Imports stay local so the factory's import graph stays
    flat and a unit test that never boots workers pays nothing.
    """
    start_telemetry = app.config.get(
        "START_GPU_TELEMETRY", not app.config.get("TESTING", False)
    )
    if start_telemetry:
        from .gpu_telemetry import start_telemetry_daemon

        start_telemetry_daemon()
