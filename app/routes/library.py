"""Library listing endpoint — the Bibliotek 3-level browse source (lb-5rc).

``GET /api/library`` returns the full LGR22 taxonomy as a 3-level tree —
subjects → areas → central content — with a count of generated assets at each
node, so the Bibliotek view (lb-eee) can render the auto-sorted library and the
Centralt-innehåll panel from one backend call.

Two sources, cleanly separated (single-SoT, risk R7):

* **Structure** comes from the taxonomy SoT (:mod:`app.taxonomy`, lb-582). The
  subject/area/central slugs and text are read from there, never hardcoded here.
* **Counts** come from the ``library`` table (lb-ah2): one row per generated,
  non-deleted artefact, grouped by ``subject`` / ``area``. The grouped counts
  are left-joined onto the taxonomy tree so *every* node appears — with
  ``asset_count == 0`` when nothing has been generated for it yet.

Read-only in this phase: no writes, and the response is derived fresh on each
request (the library is small — one teacher, one LAN tool).

Central-content derivation (resolves the contract mismatch the bead flags):
the taxonomy stores each area's central content as one Skolverket sentence —
a comma-separated list of topics ending in a period (e.g.
``"Bråk, decimaltal, procent, potenser, grundpotensform."``). The Bibliotek
panel wants discrete entries, so :func:`_central_entries` splits that sentence
into ``{id, text, asset_count}`` entries with **stable, deterministic ids**
(``<subject>:<area>:c<n>``) derived from position, so an id always denotes the
same topic across requests. Per-entry ``asset_count`` is always 0 in this
phase: the ``library`` schema links an asset to its subject + area, not to an
individual central-content topic, so there is no column to count on. Subject
and area counts are real; entry counts are a forward-looking shape kept at 0
until a future schema carries the link (documented, not fabricated).

SFW-only: this reads the taxonomy SoT and the SFW library DB and nothing else —
no restricted import, model, or route belongs here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify

from .. import taxonomy
from ..db import connect

library_bp = Blueprint("library", __name__)


def _central_entries(subject_slug: str, area_slug: str, central: str) -> list[dict[str, Any]]:
    """Split an area's Skolverket central-content sentence into entries.

    ``central`` is one sentence: a comma-separated topic list ending in a period
    (the taxonomy SoT shape). Each topic becomes one entry with a stable id
    ``"<subject>:<area>:c<n>"`` (1-based position) so the same topic keeps the
    same id across requests — downstream code (lb-eee) can reference an entry by
    id, never by free text (risk R7).

    ``asset_count`` is 0 for every entry: the library schema has no
    central-content link to count on (see module docstring). A central string
    with no splittable topic (defensive — the SoT guarantees one) yields a
    single entry holding the whole text.
    """
    body = central.strip().rstrip(".").strip()
    parts = [p.strip() for p in body.split(",") if p.strip()] if body else []
    if not parts:
        # Defensive: never drop an area's central content even if unsplittable.
        parts = [central.strip()] if central.strip() else []
    return [
        {
            "id": f"{subject_slug}:{area_slug}:c{i}",
            "text": text,
            "asset_count": 0,  # no schema link from asset → central topic yet
        }
        for i, text in enumerate(parts, start=1)
    ]


def _asset_counts() -> dict[tuple[str, str], int]:
    """Count non-deleted ``library`` rows grouped by ``(subject, area)``.

    Returns ``{(subject_slug, area_slug): count}``; absent keys mean zero. Soft-
    deleted rows (``deleted_at IS NOT NULL``) are excluded — they are not in the
    browsable library. Keys are whatever string keys the DB holds; the caller
    only reads counts for taxonomy-valid nodes, so a stray/legacy key simply
    goes unread (it never inflates a valid node).
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT subject, area, COUNT(*) AS n
            FROM library
            WHERE deleted_at IS NULL
            GROUP BY subject, area
            """
        ).fetchall()
    return {(r["subject"], r["area"]): int(r["n"]) for r in rows}


def _build_tree(counts: dict[tuple[str, str], int]) -> list[dict[str, Any]]:
    """Merge the live ``counts`` onto the taxonomy tree, in canonical order.

    Walks the taxonomy SoT (the authoritative structure + order), attaching each
    area's count from ``counts`` (default 0) and rolling areas up into a subject
    total. Every taxonomy node is present regardless of whether anything has been
    generated for it — the left-join-onto-structure contract.
    """
    subjects_out: list[dict[str, Any]] = []
    for s in taxonomy.SUBJECTS:
        s_slug = s["slug"]
        areas_out: list[dict[str, Any]] = []
        subject_total = 0
        for a in s["areas"]:
            a_slug = a["slug"]
            area_count = counts.get((s_slug, a_slug), 0)
            subject_total += area_count
            areas_out.append(
                {
                    "id": a_slug,
                    "name": a["name"],
                    "glyph": a.get("glyph"),
                    "asset_count": area_count,
                    "central_content": _central_entries(s_slug, a_slug, a["central"]),
                }
            )
        subjects_out.append(
            {
                "id": s_slug,
                "name": s["name"],
                "short": s.get("short"),
                "hue": s.get("hue"),
                "glyph": s.get("glyph"),
                "blurb": s.get("blurb"),
                "asset_count": subject_total,
                "areas": areas_out,
            }
        )
    return subjects_out


@library_bp.get("/api/library")
def get_library():
    """Return the 3-level taxonomy tree with per-node generated-asset counts.

    Shape (the contract lb-eee + lb-b1r build on)::

        {
          "subjects": [
            { "id", "name", "short", "hue", "glyph", "blurb",
              "asset_count",                 # sum over its areas
              "areas": [
                { "id", "name", "glyph", "asset_count",
                  "central_content": [ { "id", "text", "asset_count" }, … ] }
              ] }
          ],
          "generated_at": "<iso8601 UTC>"
        }

    Counts reflect the current non-deleted ``library`` contents; structure is the
    taxonomy SoT. Read-only.
    """
    subjects = _build_tree(_asset_counts())
    return (
        jsonify(
            subjects=subjects,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
        200,
    )
