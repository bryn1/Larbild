"""resolveLora endpoint — backend-authoritative LoRA resolution (lb-9tz).

``POST /api/resolve-lora`` (and a convenience ``GET`` form) turns a
``(subject, imageType)`` selection into the LoRA the render must use. The
**backend decides** — the Skapa funnel (lb-e1p) only sends the *selection*; it
never sends, and can never override, the LoRA name. This closes the SFW
injection vector from the BUILD_PLAN's isolation plane: the resolved LoRA is
always one the taxonomy SoT declares, so a crafted request cannot smuggle an
off-allowlist model in.

This is a thin wrapper over :func:`app.taxonomy.resolve_lora`, which already
implements the resolution order verbatim (subject-specific ``loraMatrix`` cell →
``baseLora`` fallback) and is unit-tested in ``tests/test_taxonomy.py``. The
endpoint adds the *HTTP contract* the funnel needs and three guarantees the bare
function does not enforce on its own:

1. **Input validation** — ``imageType`` must be a real image type (``resolve_lora``
   assumes a valid one and returns ``None`` otherwise); an unknown ``subject`` is
   rejected too, so a typo can't silently fall back to base and hide a bug. Both
   yield ``400`` with the list of valid ids.
2. **Allowlist enforcement (defense in depth)** — the resolved ``name`` is
   re-checked against the allowlist (``baseLora`` ∪ ``loraMatrix`` names). It
   always passes, since ``resolve_lora`` only ever returns from those two
   sources; the explicit check makes the invariant verifiable and fails closed
   (``500``) rather than ever emitting an unlisted LoRA.
3. **Frontend LoRA is ignored** — only ``subject`` + ``imageType`` are read from
   the request; any ``lora``/``loraName`` the client sends is discarded. The
   response echoes the inputs and marks ``authoritative: true`` so the funnel can
   show *why* a LoRA was chosen without trusting its own copy.

SFW-only: pure taxonomy resolution; no restricted import, model, or route here.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from .. import taxonomy

lora_bp = Blueprint("lora", __name__)


@lru_cache(maxsize=1)
def _allowlist() -> frozenset[str]:
    """Every LoRA name the taxonomy SoT declares — base LoRAs ∪ matrix cells.

    This is the *only* set a render may use (BUILD_PLAN hard-isolation plane).
    Built once from the frozen taxonomy; an unlisted name must never leave this
    endpoint.
    """
    names = {b["name"] for b in taxonomy.BASE_LORA.values()}
    names |= {c["name"] for c in taxonomy.LORA_MATRIX.values()}
    return frozenset(names)


def _read_selection() -> tuple[Optional[str], Optional[str]]:
    """Pull ``(subject, image_type)`` from the request, ignoring everything else.

    Accepts JSON body, form, or query string. Reads only the two selection
    fields — a client-supplied LoRA name is never read, so it cannot influence
    the result (frontend is not authoritative). ``imageType`` and the snake_case
    ``image_type`` are both accepted for ergonomics.
    """
    data: dict[str, Any] = {}
    if request.is_json:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            data = body
    get = lambda *keys: next(  # noqa: E731 - small local helper
        (data.get(k) or request.values.get(k) for k in keys if (data.get(k) or request.values.get(k))),
        None,
    )
    subject = get("subject", "subjectSlug", "subject_slug")
    image_type = get("imageType", "image_type", "type", "typeSlug")
    return subject, image_type


def _resolve(subject: Optional[str], image_type: Optional[str]):
    """Validate inputs, resolve via the SoT, enforce the allowlist.

    Returns ``(payload, status)``. The happy path is ``200`` with the resolved
    LoRA plus provenance; bad inputs are ``400``; an off-allowlist result (which
    must not happen) fails closed at ``500``.
    """
    if not subject or not image_type:
        return (
            {
                "error": "subject and imageType are required",
                "valid_subjects": list(taxonomy.subject_slugs()),
                "valid_image_types": list(taxonomy.image_type_slugs()),
            },
            400,
        )
    if not taxonomy.is_valid_subject(subject):
        return (
            {"error": f"unknown subject: {subject!r}",
             "valid_subjects": list(taxonomy.subject_slugs())},
            400,
        )
    if not taxonomy.is_valid_image_type(image_type):
        return (
            {"error": f"unknown imageType: {image_type!r}",
             "valid_image_types": list(taxonomy.image_type_slugs())},
            400,
        )

    resolved = taxonomy.resolve_lora(subject, image_type)
    # Both inputs are validated above, so resolve_lora must return a LoRA; guard
    # anyway rather than ever returning a null result downstream.
    if resolved is None or "name" not in resolved:  # pragma: no cover - unreachable
        return ({"error": "resolution failed", "subject": subject,
                 "imageType": image_type}, 500)

    if resolved["name"] not in _allowlist():  # pragma: no cover - invariant
        # resolve_lora only returns base/matrix names, so this is unreachable;
        # fail closed if the invariant is ever violated rather than emit it.
        return (
            {"error": "resolved LoRA not in allowlist (refused)",
             "subject": subject, "imageType": image_type},
            500,
        )

    return (
        {
            "subject": subject,
            "imageType": image_type,
            "lora": resolved,            # {name, strength, source, base/note, …}
            "authoritative": True,       # backend decided; client cannot override
        },
        200,
    )


@lora_bp.post("/api/resolve-lora")
def resolve_lora_post():
    """Resolve the LoRA for a ``(subject, imageType)`` selection (JSON/form body).

    The funnel POSTs its selection; the backend returns the authoritative LoRA.
    Any LoRA the client includes in the body is ignored.
    """
    subject, image_type = _read_selection()
    payload, status = _resolve(subject, image_type)
    return jsonify(payload), status


@lora_bp.get("/api/resolve-lora")
def resolve_lora_get():
    """Query-string form (``?subject=…&imageType=…``) for easy preview/debug."""
    subject, image_type = _read_selection()
    payload, status = _resolve(subject, image_type)
    return jsonify(payload), status
