"""Taxonomy endpoint — exposes the backend-authoritative LGR22 taxonomy (lb-582).

``GET /api/taxonomy`` returns the canonical taxonomy (subjects + areas + central
content, image types, video types, narration voices, base LoRA, LoRA matrix) as
JSON so the frontend bootstraps from one source of truth instead of shipping its
own copy of ``data.js`` (risk R7 — slug drift).

Pure read of the frozen :mod:`app.taxonomy` SoT; no DB, no external service.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from .. import taxonomy

taxonomy_bp = Blueprint("taxonomy", __name__)


@taxonomy_bp.get("/api/taxonomy")
def get_taxonomy():
    """Return the whole LGR22 taxonomy as JSON (the frontend bootstrap source)."""
    return jsonify(taxonomy.as_dict()), 200
