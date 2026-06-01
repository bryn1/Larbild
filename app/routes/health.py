"""Health / liveness endpoint — the Lärbild shell's first route.

``GET /healthz`` is the deploy + uptime probe. P0.3's larbild.service and the
deploy script poll it to confirm the app booted on port 6767. Kept dependency-
free so it answers even when downstream services (Redis, ComfyUI, the DB) are
unconfigured or down — it reports process liveness, not deep health.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

health_bp = Blueprint("health", __name__)


@health_bp.get("/healthz")
def healthz():
    """Liveness probe. Always 200 ``{"status": "ok"}`` when the process serves."""
    return jsonify(status="ok"), 200
