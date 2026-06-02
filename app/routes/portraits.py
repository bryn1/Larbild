"""Porträtt consent API — the create/use/delete gates (lb-ly1 / lb-wdu / lb-xlr).

Thin HTTP layer over :mod:`app.portraits` (which owns the consent rule + DB +
storage). The contract that matters:

  * ``POST   /api/portraits``            — register a consented reference. The
    server computes ``is_minor`` from a birthdate/age and REQUIRES guardian
    consent for a minor (lb-ly1). A client-supplied ``consentOk`` is ignored.
  * ``GET    /api/portraits``            — list references; ``?usable=1`` returns
    only the consent-OK ones the use path may surface (lb-xlr).
  * ``GET    /api/portraits/<id>``       — one reference (recomputed consent_ok).
  * ``POST   /api/portraits/<id>/use``   — gate a compositor use: the server
    RECOMPUTES consentOk from the DB and returns 403 if not consented; writes an
    audit row either way (lb-wdu).
  * ``DELETE /api/portraits/<id>``       — GDPR delete: cascade reference files
    off disk + the DB row, audited (lb-xlr).

Never trust the client for consent: ``is_minor`` is derived server-side and
``consentOk`` is recomputed from the DB on every use. SFW-only: consent records,
an audit trail, and reference-image storage; no compositor / swap engine here.
"""
from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from .. import portraits
from ..db import new_id

portraits_bp = Blueprint("portraits", __name__)

# Accept only still-image reference uploads (SFW reference photos). Keep the set
# tight; the suffix also names the stored file.
_ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_IMAGE_BYTES = 16 * 1024 * 1024  # 16 MiB per reference image


def _body() -> dict:
    """Pull the JSON/form body as a dict (empty dict if absent/non-object)."""
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
    if request.form:
        return dict(request.form)
    return {}


def _suffix_for(filename: Optional[str], default: str = ".png") -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in _ALLOWED_IMAGE_SUFFIXES else default


def _save_reference_image(data: dict) -> tuple[Optional[str], Optional[tuple[dict, int]]]:
    """Persist the uploaded reference image under STORAGE_DIR/portraits.

    Two input shapes:
      * multipart ``image`` file (the browser path), or
      * JSON ``image_b64`` (base64 bytes) + optional ``image_name`` (the
        programmatic/test path).

    Returns ``(rel_path, None)`` on success or ``(None, (err, status))``. The
    returned path is STORAGE_DIR-relative (what the DB stores); the bytes are
    written into the isolated portraits/ subdir under a fresh id.
    """
    file_storage = request.files.get("image")
    raw: Optional[bytes] = None
    suffix = ".png"

    if file_storage is not None and file_storage.filename:
        suffix = _suffix_for(file_storage.filename)
        raw = file_storage.read()
    elif data.get("image_b64"):
        try:
            raw = base64.b64decode(str(data["image_b64"]), validate=True)
        except (binascii.Error, ValueError):
            return None, ({"error": "image_b64 is not valid base64",
                           "code": "bad_image"}, 400)
        suffix = _suffix_for(data.get("image_name"))
    else:
        return None, ({"error": "a reference image is required (multipart 'image' "
                       "or JSON 'image_b64')", "code": "image_required"}, 400)

    if not raw:
        return None, ({"error": "reference image is empty", "code": "bad_image"}, 400)
    if len(raw) > _MAX_IMAGE_BYTES:
        return None, ({"error": "reference image too large",
                       "code": "image_too_large", "max_bytes": _MAX_IMAGE_BYTES}, 413)

    rel_name = f"{new_id()}{suffix}"
    dest = portraits.portraits_dir() / rel_name
    dest.write_bytes(raw)
    # STORAGE_DIR-relative path is what the DB row carries.
    return f"portraits/{rel_name}", None


@portraits_bp.post("/api/portraits")
def create_portrait():
    """Register a consented portrait reference (lb-ly1).

    Server-authoritative gate:
      * ``is_minor`` is computed from ``birthdate`` (ISO) or ``age`` — never read
        from the client.
      * an under-18 reference WITHOUT ``guardian_consent`` is rejected (422)
        before anything is stored; the DB CHECK is the backstop.
      * a client-supplied ``consentOk`` / ``is_minor`` is ignored.

    On success: stores the reference image + the faces row, writes an audit row,
    returns ``201`` with the created record (consent_ok recomputed server-side).
    """
    data = _body()

    # Compute is_minor server-side FIRST (rejects missing/invalid age input).
    try:
        is_minor = portraits.compute_is_minor(
            birthdate=data.get("birthdate"),
            age=data.get("age"),
        )
    except portraits.ConsentError as exc:
        return jsonify({"error": str(exc), "code": exc.code}), exc.status

    guardian_consent = (data.get("guardian_consent") or "").strip() or None

    # Application gate (mirrors + precedes the DB floor) with a precise reason.
    if is_minor == 1 and not guardian_consent:
        return jsonify({
            "error": "guardian consent is required for an under-18 reference",
            "code": "guardian_consent_required",
            "is_minor": True,
        }), 422

    # Only now persist the image (avoid writing a file for a rejected request).
    rel_path, err = _save_reference_image(data)
    if err is not None:
        payload, status = err
        return jsonify(payload), status

    pin = portraits.PortraitInput(
        label=(data.get("label") or "").strip() or None,
        is_minor=is_minor,
        guardian_consent=guardian_consent,
        consenter_name=(data.get("consenter_name") or "").strip() or None,
        consent_date=(data.get("consent_date") or "").strip() or None,
        consent_notes=(data.get("consent_notes") or "").strip() or None,
        image_path=rel_path,
        thumb_path=None,
    )
    try:
        record = portraits.register_portrait(pin)
    except portraits.ConsentError as exc:
        # Roll back the just-written file so a rejected registration leaves no
        # orphan on disk (defence-in-depth; the app gate above usually catches
        # this first).
        portraits._safe_unlink_under_storage(rel_path)
        return jsonify({"error": str(exc), "code": exc.code}), exc.status

    return jsonify(record), 201
