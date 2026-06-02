"""LGR22 taxonomy — the backend-authoritative single source of truth (lb-582).

The canonical taxonomy ships as ``taxonomy.json`` next to this module, copied
**verbatim** from the design's ``design/data.js`` (``window.LB``). It is the one
place subject / area / image-type / video-type slugs and the LoRA matrix are
defined; every other layer (DB validation, the library API, the frontend)
references these slugs by id, never by free text (risk R7 — slug drift).

Frozen at import, so the loaded structures are never mutated at runtime:

  * :data:`SUBJECTS`      — 17 LGR22 åk 7–9 subjects, each with ``areas`` (102
    total) carrying the Skolverket central content (``central``).
  * :data:`IMAGE_TYPES`   — 8 image types (foto, illustration, …).
  * :data:`VIDEO_TYPES`   — 6 film types.
  * :data:`NARRATION_VOICES` — 2 Swedish narration voices.
  * :data:`BASE_LORA`     — base (style) LoRA per image type (8).
  * :data:`LORA_MATRIX`   — 28 specialised ``"<subject>:<imageType>"`` cells.

:func:`resolve_lora` reproduces the design's resolution order verbatim:
subject-specific matrix cell → base LoRA for the image type.

Lookup helpers (:func:`subject`, :func:`area`, :func:`image_type`,
:func:`is_valid_subject`, …) back both the DB key validation (lb-ah2) and the
``/api/library`` endpoint (lb-5rc). :func:`as_dict` returns the whole taxonomy
for the frontend bootstrap.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "SUBJECTS",
    "IMAGE_TYPES",
    "VIDEO_TYPES",
    "NARRATION_VOICES",
    "BASE_LORA",
    "LORA_MATRIX",
    "FLUX_LORA",
    "TAXONOMY_PATH",
    "resolve_lora",
    "resolve_flux_lora",
    "flux_lora_names",
    "subject",
    "area",
    "image_type",
    "video_type",
    "is_valid_subject",
    "is_valid_area",
    "is_valid_image_type",
    "is_valid_video_type",
    "subject_slugs",
    "area_slugs",
    "image_type_slugs",
    "video_type_slugs",
    "as_dict",
]

# The canonical taxonomy JSON — verbatim from design/data.js (window.LB).
TAXONOMY_PATH = Path(__file__).resolve().parent / "taxonomy.json"


def _freeze(value: Any) -> Any:
    """Recursively make a JSON value read-only.

    dict → MappingProxyType, list → tuple, scalars unchanged. Keeps the loaded
    taxonomy immutable so no caller can mutate the single source of truth.
    """
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _load() -> Mapping[str, Any]:
    """Load + freeze the taxonomy JSON. Module-import time; fail loud if absent."""
    with TAXONOMY_PATH.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return _freeze(raw)


_DATA = _load()

# Public, immutable views of every canonical structure.
SUBJECTS: Sequence[Mapping[str, Any]] = _DATA["subjects"]
IMAGE_TYPES: Sequence[Mapping[str, Any]] = _DATA["imageTypes"]
VIDEO_TYPES: Sequence[Mapping[str, Any]] = _DATA["videoTypes"]
NARRATION_VOICES: Sequence[Mapping[str, Any]] = _DATA["narrationVoices"]
BASE_LORA: Mapping[str, Mapping[str, Any]] = _DATA["baseLora"]
LORA_MATRIX: Mapping[str, Mapping[str, Any]] = _DATA["loraMatrix"]


# ── FLUX render-lane LoRAs (SFW educational only — lb FLUX lane) ───────────
#
# The FLUX dev-fp8 lane (app.workflow.assemble_flux_txt2img) uses a SMALL set of
# Lärbild-allowlisted FLUX LoRAs — distinct on-disk files from the SDXL LoRA set
# above. Declared HERE (in code) rather than in taxonomy.json on purpose: the
# JSON is copied verbatim from the design's window.LB (the LGR22 design SoT) and
# must not drift; the FLUX LoRA roster is a Lärbild deployment detail (which
# physical FLUX adapters live on VM100), so it lives in the module that owns the
# taxonomy SoT without mutating the frozen JSON.
#
# STRICTLY SFW: these are the ONLY FLUX LoRAs the FLUX path may inject. The
# imagegen / NSFW FLUX LoRAs that also live on the box (loras/bondage/*,
# loras/faces/*, …) are NEVER listed here — :func:`resolve_flux_lora` only ever
# returns a name from this map, and app.workflow enforces a FLUX allowlist built
# from it, so an off-list FLUX LoRA can never reach a render (the same fail-closed
# guard the SDXL path has). FLUX follows prompts well, so most imageTypes render
# LoRA-FREE on the base model (a missing key → no LoRA, NOT a fallback to SDXL's).
#
# Routing (per the lb FLUX-lane design):
#   * mikroskop → photomicrography-flux  (cellular / micrograph realism)
#   * diagram / tvarsnitt → schematic-machinery-flux (schematic + cutaway/teknik)
#   * everything else → base FLUX, no LoRA.
# The on-disk files sit under ComfyUI loras/larbild/ ; the ComfyUI lora_name (the
# subpath) is derived by app.workflow.lora_filename, so the value here is the
# logical stem (no dir, no suffix) — kept consistent with BASE_LORA/LORA_MATRIX.
FLUX_LORA: Mapping[str, Mapping[str, Any]] = MappingProxyType({
    "mikroskop": MappingProxyType(
        {"name": "larbild/photomicrography-flux", "strength": 0.9,
         "note": "FLUX mikrofotografi — cellulär/mikroskopisk realism."}),
    "diagram": MappingProxyType(
        {"name": "larbild/schematic-machinery-flux", "strength": 0.85,
         "note": "FLUX schematik — teknik/maskindiagram."}),
    "tvarsnitt": MappingProxyType(
        {"name": "larbild/schematic-machinery-flux", "strength": 0.85,
         "note": "FLUX schematik — genomskärning/teknik."}),
})


# ── lookup indexes (built once) ───────────────────────────────────────────


@lru_cache(maxsize=1)
def _subject_index() -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType({s["slug"]: s for s in SUBJECTS})


@lru_cache(maxsize=1)
def _area_index() -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
    """{subject_slug: {area_slug: area}} — areas are subject-scoped."""
    return MappingProxyType(
        {s["slug"]: MappingProxyType({a["slug"]: a for a in s["areas"]}) for s in SUBJECTS}
    )


@lru_cache(maxsize=1)
def _image_type_index() -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType({t["slug"]: t for t in IMAGE_TYPES})


@lru_cache(maxsize=1)
def _video_type_index() -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType({t["slug"]: t for t in VIDEO_TYPES})


# ── LoRA resolution (verbatim port of design resolveLora) ──────────────────


def resolve_lora(subject_slug: str, type_slug: str) -> Optional[dict]:
    """Recommended LoRA for ``(subject, imageType)``, backend-authoritative.

    Verbatim port of the design's ``resolveLora`` (data.js): resolution order
    is the subject-specific matrix cell, then the base LoRA for the image type.

      * matrix hit → ``{**cell, "source": "ämnesspecifik", "base": cell.base or "SDXL"}``
      * else       → ``{**base_lora[type], "note": "Bas-LoRA för bildtypen.",
                        "source": "bas"}``

    Every image type has a base LoRA, so any valid ``type_slug`` resolves. An
    unknown ``type_slug`` (no matrix cell *and* no base entry) returns ``None``
    — the design assumes a valid image type; callers should validate first via
    :func:`is_valid_image_type`. ``subject_slug`` is not required to be valid:
    an unknown subject simply has no matrix cells and falls back to base.
    """
    cell = LORA_MATRIX.get(f"{subject_slug}:{type_slug}")
    if cell is not None:
        out = dict(cell)
        out["source"] = "ämnesspecifik"
        out["base"] = cell.get("base", "SDXL")
        return out
    base = BASE_LORA.get(type_slug)
    if base is None:
        return None
    out = dict(base)
    out["note"] = "Bas-LoRA för bildtypen."
    out["source"] = "bas"
    return out


def resolve_flux_lora(type_slug: str) -> Optional[dict]:
    """The FLUX-lane LoRA for an imageType, or ``None`` to render LoRA-free.

    The FLUX path's analogue of :func:`resolve_lora`, but FLUX-specific and
    OPTIONAL: only a handful of imageTypes map to a FLUX LoRA (:data:`FLUX_LORA`);
    every other type renders on the base FLUX model with no LoRA (FLUX follows
    prompts well enough that a style LoRA is unnecessary, and there is no
    SDXL-style per-cell matrix on the FLUX lane). A hit returns
    ``{**cell, "source": "flux"}``; a miss returns ``None`` (NOT a fallback to the
    SDXL LoRA — the two lanes' adapters are not interchangeable). The returned
    name is always one of the SFW :data:`FLUX_LORA` names, so the FLUX allowlist
    in app.workflow always passes on the happy path.
    """
    cell = FLUX_LORA.get(type_slug)
    if cell is None:
        return None
    out = dict(cell)
    out["source"] = "flux"
    return out


def flux_lora_names() -> frozenset[str]:
    """Every SFW FLUX LoRA name the FLUX lane may inject (the FLUX allowlist set).

    The single set the FLUX render path is allowed to reference — the FLUX twin
    of ``baseLora ∪ loraMatrix``. app.workflow builds its FLUX allowlist from this
    so an off-list (e.g. NSFW) FLUX LoRA can never enter a render.
    """
    return frozenset(c["name"] for c in FLUX_LORA.values())


# ── slug lookups + validation (back DB key checks + the library API) ───────


def subject(slug: str) -> Optional[Mapping[str, Any]]:
    """The subject for ``slug``, or ``None``."""
    return _subject_index().get(slug)


def area(subject_slug: str, area_slug: str) -> Optional[Mapping[str, Any]]:
    """The area for ``(subject, area)``, or ``None``. Areas are subject-scoped."""
    return _area_index().get(subject_slug, {}).get(area_slug)


def image_type(slug: str) -> Optional[Mapping[str, Any]]:
    return _image_type_index().get(slug)


def video_type(slug: str) -> Optional[Mapping[str, Any]]:
    return _video_type_index().get(slug)


def is_valid_subject(slug: str) -> bool:
    return slug in _subject_index()


def is_valid_area(subject_slug: str, area_slug: str) -> bool:
    """True iff ``area_slug`` is an area of ``subject_slug`` (subject-scoped)."""
    return area_slug in _area_index().get(subject_slug, {})


def is_valid_image_type(slug: str) -> bool:
    return slug in _image_type_index()


def is_valid_video_type(slug: str) -> bool:
    return slug in _video_type_index()


def subject_slugs() -> tuple[str, ...]:
    return tuple(_subject_index().keys())


def area_slugs(subject_slug: str) -> tuple[str, ...]:
    """Area slugs for one subject (empty tuple for an unknown subject)."""
    return tuple(_area_index().get(subject_slug, {}).keys())


def image_type_slugs() -> tuple[str, ...]:
    return tuple(_image_type_index().keys())


def video_type_slugs() -> tuple[str, ...]:
    return tuple(_video_type_index().keys())


def as_dict() -> dict:
    """The whole taxonomy as plain (JSON-serialisable) data.

    Deep-copied out of the frozen views so the caller (e.g. the frontend
    bootstrap endpoint) can serialise / mutate freely without touching the SoT.
    """
    return json.loads(json.dumps(_DATA, default=_proxy_default))


def _proxy_default(obj: Any) -> Any:
    """json.dumps hook: render MappingProxyType as a dict."""
    if isinstance(obj, MappingProxyType):
        return dict(obj)
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")
