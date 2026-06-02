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
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from .. import taxonomy
from ..db import connect

library_bp = Blueprint("library", __name__)

# The URL prefix the frontend builds an <img> src from. A library row's
# ``file_path`` is STORAGE_DIR-relative (e.g. ``output/fysik/.../x.png``); the
# media route (app.routes.media) serves it at ``/media/<file_path>``. Centralised
# here so the items payload and the serving route never drift on the prefix.
MEDIA_URL_PREFIX = "/media/"

# Columns surfaced per item — enough for the Bibliotek gallery tile + the
# full-size view's metadata, without dumping the whole row. ``file_path`` /
# ``thumb_path`` are turned into servable URLs; the rest are display fields.
_ITEM_COLUMNS = (
    "id",
    "kind",
    "subject",
    "area",
    "image_type",
    "video_type",
    "title",
    "prompt",
    "lora",
    "lora_strength",
    "seed",
    "backend",
    "width",
    "height",
    "duration_s",
    "file_path",
    "thumb_path",
    "created_at",
)


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


def _media_url(file_path: Optional[str]) -> Optional[str]:
    """Turn a STORAGE_DIR-relative ``file_path`` into the servable media URL.

    The media route (:mod:`app.routes.media`) serves any file under STORAGE_DIR
    at ``/media/<relpath>``; the stored ``file_path`` is exactly that relpath
    (``output/<subject>/<area>/<imageType>/<file>``), so the URL is the prefix +
    the path. Returns ``None`` for an empty/None path (the row carries no file —
    e.g. a thumb that was never generated), so the client can fall back. The path
    is URL-quoted (defensive: filenames are simple, but a space or unicode glyph
    in an area slug must not break the URL); ``/`` separators are preserved.
    """
    if not file_path:
        return None
    from urllib.parse import quote

    return MEDIA_URL_PREFIX + quote(file_path.lstrip("/"), safe="/")


def _row_to_item(row) -> dict[str, Any]:
    """Shape one ``library`` row into the Bibliotek gallery item contract.

    Carries the provenance/display fields the gallery + full-size view show, and
    crucially attaches ``url`` (and ``thumb_url`` when a thumb exists) — the
    servable image URLs the frontend renders an ``<img>`` from. ``area_name`` is
    resolved from the taxonomy SoT for the tile's caption (falling back to the
    slug if the taxonomy can't resolve it — never blank).
    """
    d = {k: row[k] for k in _ITEM_COLUMNS}
    d["url"] = _media_url(row["file_path"])
    d["thumb_url"] = _media_url(row["thumb_path"]) or d["url"]
    area_node = taxonomy.area(row["subject"], row["area"])
    d["area_name"] = (area_node["name"] if area_node else None) or row["area"]
    # Stable display alias the frontend already reads.
    d["generated_at"] = row["created_at"]
    return d


def _node_items(subject: str, area: str, image_type: Optional[str]) -> list[dict[str, Any]]:
    """Fetch the browsable ``library`` rows for one Bibliotek node, newest first.

    Scopes to ``subject`` + ``area`` (both required) and, when given,
    ``image_type`` (the imageTypes slug). Excludes rows that are NOT browsable:

      * soft-deleted (``deleted_at IS NOT NULL``) — removed from the library, and
      * dangling (``missing_since IS NOT NULL``) — the file vanished off disk
        (flagged by the boot reconcile, lb-qtr); serving its URL would 404, so it
        stays out of the gallery rather than render a broken tile.

    Ordered ``created_at DESC`` (newest first) to match the auto-sort index and
    the Bibliotek "latest at top" expectation. Read-only.
    """
    cols = ", ".join(_ITEM_COLUMNS)
    sql = (
        f"SELECT {cols} FROM library "
        "WHERE subject = ? AND area = ? "
        "AND deleted_at IS NULL AND missing_since IS NULL"
    )
    params: list[Any] = [subject, area]
    if image_type:
        # A film row has video_type, not image_type; match either column so an
        # imageType filter never silently hides a same-bucket film.
        sql += " AND (image_type = ? OR video_type = ?)"
        params.extend([image_type, image_type])
    sql += " ORDER BY created_at DESC, id DESC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_item(r) for r in rows]


@library_bp.get("/api/library/items")
def get_library_items():
    """Return the generated artefacts for one Bibliotek node (the gallery source).

    Query params:
      * ``subject`` (required) — subject slug (taxonomy SoT).
      * ``area`` (required) — area slug, scoped to the subject.
      * ``imageType`` (optional) — imageTypes slug; narrows to one type bucket.

    ``subject``/``area`` are validated against the taxonomy SoT (lb-582): an
    unknown pair returns ``[]`` with HTTP 200 (a valid-but-empty node looks the
    same to the gallery — "nothing here yet" — rather than erroring). A missing
    required param is a 400. Each item carries a servable ``url`` (+ ``thumb_url``)
    so the Bibliotek view can render a thumbnail and a full-size view directly.

    Shape::

        {
          "subject": "fysik", "area": "rorelse", "image_type": "diagram"|null,
          "items": [
            { "id", "kind", "title", "prompt", "lora", "seed", "backend",
              "width", "height", "area_name", "generated_at",
              "url", "thumb_url", … },
            …  # newest first
          ],
          "count": <int>,
          "generated_at": "<iso8601 UTC>"
        }

    Soft-deleted and dangling (file-missing) rows are excluded. Read-only.
    """
    subject = (request.args.get("subject") or "").strip()
    area = (request.args.get("area") or "").strip()
    image_type = (request.args.get("imageType") or "").strip() or None
    if not subject or not area:
        return jsonify(error="subject and area are required"), 400

    # Unknown taxonomy node → empty (not an error): the gallery shows the
    # "nothing here yet" state, same as a real-but-empty node.
    if not taxonomy.is_valid_area(subject, area):
        items: list[dict[str, Any]] = []
    else:
        items = _node_items(subject, area, image_type)

    return (
        jsonify(
            subject=subject,
            area=area,
            image_type=image_type,
            items=items,
            count=len(items),
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
        200,
    )
