"""LGR22 taxonomy — the backend-authoritative single source of truth (lb-582).

The canonical taxonomy ships as ``taxonomy.json`` next to this module, copied
**verbatim** from the design's ``design/data.js`` (``window.LB``). It is the one
place subject / area / image-type / video-type slugs are defined; every other
layer (DB validation, the library API, the frontend) references these slugs by
id, never by free text (risk R7 — slug drift).

The LoRA matrix that once lived here (``baseLora`` / ``loraMatrix`` /
``resolve_lora`` and the FLUX lane) was the diffusion render path's
backend-authoritative LoRA selection + SFW allowlist; it was removed with the
render path when Lärbild committed permanently to authored SVG content.

Frozen at import, so the loaded structures are never mutated at runtime:

  * :data:`SUBJECTS`      — 17 LGR22 åk 7–9 subjects, each with ``areas`` (102
    total) carrying the Skolverket central content (``central``).
  * :data:`IMAGE_TYPES`   — 8 image types (foto, illustration, …).
  * :data:`VIDEO_TYPES`   — 6 film types.
  * :data:`NARRATION_VOICES` — 2 Swedish narration voices.

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
    "TAXONOMY_PATH",
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
