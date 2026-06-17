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
        _start_background_workers(app)
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
    from .routes.lessons import lessons_bp
    from .routes.library import library_bp
    from .routes.lora import lora_bp
    from .routes.media import media_bp
    from .routes.portraits import portraits_bp
    from .routes.taxonomy import taxonomy_bp
    from .routes.tts import tts_bp
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
    # lb-lesson: the Lärbild pedagogical-video funnel — POST /api/lessons (enqueue
    # a manuscript → per-beat keyframe+bildtext+narration → merge → a level-tagged
    # lesson/short VIDEO in the library, 202), GET /api/lessons/schema (the
    # manuscript schema the Opus step produces), POST /api/lessons/preview (parse +
    # estimate, no render), GET /api/lessons/<id>[/events] (state + SSE; a lesson is
    # a jobs row so it reuses the still-job SSE). allLevels renders all 3 levels
    # from one keyframe render. The LoRA is backend-authoritative per keyframe (SFW
    # guard); live keyframe render is deferred (imagegen sweep). Built on P6 (film).
    app.register_blueprint(lessons_bp)
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
