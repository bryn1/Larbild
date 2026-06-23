"""Asset-lessons API — group existing library assets into a named lesson (lb-zdp).

A *lesson* here is a teacher-curated, ordered COLLECTION of already-generated
library assets (the migration 0009 ``asset_lessons`` / ``asset_lesson_items``
tables). It is a SEPARATE concern from the lb-lesson video-manuscript funnel
(``app/routes/lessons.py``), which RENDERS a new pedagogical video — that funnel
makes content; this groups content the teacher already has. The two share nothing
but the word "lesson", so this blueprint owns distinct routes (``/api/asset-
lessons*``) and distinct tables; it does not touch the video pipeline, the
gallery, or the review/regenerate loop.

Endpoints (all owner-only — this blueprint is registered only on the private LAN
instance, never on the public read-only bilder.sibbamala.com process):

  * ``GET    /api/asset-lessons``                  — list lessons, newest first,
        each with its asset count + a few thumbnail URLs for a card preview.
  * ``POST   /api/asset-lessons``                  — create a lesson {title,
        subject?, area?} → 201 the new lesson.
  * ``GET    /api/asset-lessons/<id>``             — one lesson + its ordered
        assets (each shaped by the library item contract, so the frontend renders
        them with the same tile component).
  * ``PATCH  /api/asset-lessons/<id>``             — rename / retag {title?,
        subject?, area?}.
  * ``DELETE /api/asset-lessons/<id>``             — delete the lesson (its
        membership rows cascade; the library assets are untouched).
  * ``POST   /api/asset-lessons/<id>/items``       — add an asset {library_id} to
        the end (idempotent: re-adding is a no-op, never a duplicate).
  * ``DELETE /api/asset-lessons/<id>/items/<lib>`` — remove an asset.
  * ``PUT    /api/asset-lessons/<id>/order``       — reorder {order:[library_id…]}.

Reuses ``app.routes.library._row_to_item`` for asset serialization (ONE item
contract — no second shaping of a library row). SFW-only: a grouping layer over
the existing SFW library; no restricted import, model, or content.
"""
from __future__ import annotations

import os
from typing import Any

from flask import Blueprint, jsonify, request

from ..db import connect, new_id, now_iso

asset_lessons_bp = Blueprint("asset_lessons", __name__)

# Owner-only: this blueprint is only registered in the private factory, but guard
# at import time too so an accidental public registration is still inert. Same
# switch as Config.PUBLIC_READONLY / the library blueprint's _PUBLIC_READONLY.
_PUBLIC_READONLY = os.environ.get("LARBILD_PUBLIC_READONLY", "").strip().lower() in {
    "1", "true", "yes", "on",
}

# Teacher-entered title; cap so a runaway paste can't bloat the row (matches the
# library comment cap's intent).
_TITLE_MAX = 300
# Per-card thumbnail preview count (the lesson list shows a few cover thumbs).
_PREVIEW_THUMBS = 4


def _clean_title(raw: Any) -> str | None:
    """Validate + normalise a lesson title. Returns None if empty/too long."""
    if not isinstance(raw, str):
        return None
    title = raw.strip()
    if not title or len(title) > _TITLE_MAX:
        return None
    return title


def _opt_str(raw: Any) -> str | None:
    """A nullable optional string field (subject/area hint): trimmed or None."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    return s or None


def _lesson_row(conn, lesson_id: str):
    return conn.execute(
        "SELECT id, title, subject, area, created_at, updated_at "
        "FROM asset_lessons WHERE id = ?",
        (lesson_id,),
    ).fetchone()


def _item_rows_for_lesson(conn, lesson_id: str) -> list[dict[str, Any]]:
    """The lesson's assets in position order, shaped by the library item contract.

    Joins membership → ``library`` and reuses ``library._row_to_item`` so a lesson
    asset is the SAME shape (url/thumb_url/review state/…) the gallery renders —
    no second serialization of a library row. Soft-deleted / dangling assets are
    excluded so the lesson never shows a tile whose file 404s.
    """
    from .library import _row_to_item  # late import: avoid an import cycle at boot

    rows = conn.execute(
        """
        SELECT lib.*, li.position AS _lesson_position
          FROM asset_lesson_items li
          JOIN library lib ON lib.id = li.library_id
         WHERE li.lesson_id = ?
           AND lib.deleted_at IS NULL
           AND lib.missing_since IS NULL
         ORDER BY li.position ASC, li.added_at ASC
        """,
        (lesson_id,),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def _lesson_summary(conn, row) -> dict[str, Any]:
    """A lesson list-card: the lesson + its asset count + a few cover thumbs."""
    count = conn.execute(
        """
        SELECT COUNT(*) FROM asset_lesson_items li
          JOIN library lib ON lib.id = li.library_id
         WHERE li.lesson_id = ?
           AND lib.deleted_at IS NULL AND lib.missing_since IS NULL
        """,
        (row["id"],),
    ).fetchone()[0]
    thumbs = conn.execute(
        """
        SELECT COALESCE(lib.thumb_path, lib.file_path) AS p
          FROM asset_lesson_items li
          JOIN library lib ON lib.id = li.library_id
         WHERE li.lesson_id = ?
           AND lib.deleted_at IS NULL AND lib.missing_since IS NULL
         ORDER BY li.position ASC, li.added_at ASC
         LIMIT ?
        """,
        (row["id"], _PREVIEW_THUMBS),
    ).fetchall()
    from .library import _media_url  # late import (cycle-safe)

    return {
        "id": row["id"],
        "title": row["title"],
        "subject": row["subject"],
        "area": row["area"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "asset_count": count,
        "thumb_urls": [_media_url(t["p"]) for t in thumbs if t["p"]],
    }


def _touch(conn, lesson_id: str) -> None:
    """Bump a lesson's updated_at so the list re-sorts it to the top."""
    conn.execute(
        "UPDATE asset_lessons SET updated_at = ? WHERE id = ?",
        (now_iso(), lesson_id),
    )


@asset_lessons_bp.get("/api/asset-lessons")
def list_lessons():
    """List all lessons, newest-updated first, each as a preview card."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, subject, area, created_at, updated_at "
            "FROM asset_lessons ORDER BY updated_at DESC"
        ).fetchall()
        out = [_lesson_summary(conn, r) for r in rows]
    return jsonify(lessons=out, count=len(out)), 200


@asset_lessons_bp.post("/api/asset-lessons")
def create_lesson():
    """Create a named lesson. Body: {title (required), subject?, area?}."""
    data = request.get_json(silent=True) or {}
    title = _clean_title(data.get("title"))
    if not title:
        return jsonify(error="title is required (1–%d chars)" % _TITLE_MAX), 400
    lesson_id = new_id()
    ts = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO asset_lessons (id, title, subject, area, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (lesson_id, title, _opt_str(data.get("subject")), _opt_str(data.get("area")), ts, ts),
        )
        row = _lesson_row(conn, lesson_id)
        summary = _lesson_summary(conn, row)
    return jsonify(lesson=summary), 201


@asset_lessons_bp.get("/api/asset-lessons/<lesson_id>")
def get_lesson(lesson_id: str):
    """One lesson + its ordered assets (library item shape)."""
    with connect() as conn:
        row = _lesson_row(conn, lesson_id)
        if not row:
            return jsonify(error="lesson not found"), 404
        items = _item_rows_for_lesson(conn, lesson_id)
        lesson = {
            "id": row["id"], "title": row["title"], "subject": row["subject"],
            "area": row["area"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "asset_count": len(items),
        }
    return jsonify(lesson=lesson, items=items), 200


@asset_lessons_bp.patch("/api/asset-lessons/<lesson_id>")
def update_lesson(lesson_id: str):
    """Rename / retag a lesson. Body: any of {title, subject, area}."""
    data = request.get_json(silent=True) or {}
    sets, params = [], []
    if "title" in data:
        title = _clean_title(data.get("title"))
        if not title:
            return jsonify(error="title must be 1–%d chars" % _TITLE_MAX), 400
        sets.append("title = ?"); params.append(title)
    if "subject" in data:
        sets.append("subject = ?"); params.append(_opt_str(data.get("subject")))
    if "area" in data:
        sets.append("area = ?"); params.append(_opt_str(data.get("area")))
    if not sets:
        return jsonify(error="nothing to update"), 400
    sets.append("updated_at = ?"); params.append(now_iso())
    params.append(lesson_id)
    with connect() as conn:
        if not _lesson_row(conn, lesson_id):
            return jsonify(error="lesson not found"), 404
        conn.execute(f"UPDATE asset_lessons SET {', '.join(sets)} WHERE id = ?", params)
        summary = _lesson_summary(conn, _lesson_row(conn, lesson_id))
    return jsonify(lesson=summary), 200


@asset_lessons_bp.delete("/api/asset-lessons/<lesson_id>")
def delete_lesson(lesson_id: str):
    """Delete a lesson (membership cascades; library assets are untouched)."""
    with connect() as conn:
        if not _lesson_row(conn, lesson_id):
            return jsonify(error="lesson not found"), 404
        conn.execute("DELETE FROM asset_lessons WHERE id = ?", (lesson_id,))
    return jsonify(ok=True), 200


@asset_lessons_bp.post("/api/asset-lessons/<lesson_id>/items")
def add_item(lesson_id: str):
    """Add a library asset to the lesson's end. Body: {library_id}.

    Idempotent: re-adding an asset already in the lesson is a no-op (the PK on
    (lesson_id, library_id) prevents a duplicate), returning 200 not an error.
    """
    data = request.get_json(silent=True) or {}
    library_id = _opt_str(data.get("library_id"))
    if not library_id:
        return jsonify(error="library_id is required"), 400
    with connect() as conn:
        if not _lesson_row(conn, lesson_id):
            return jsonify(error="lesson not found"), 404
        asset = conn.execute(
            "SELECT id FROM library WHERE id = ? AND deleted_at IS NULL",
            (library_id,),
        ).fetchone()
        if not asset:
            return jsonify(error="asset not found"), 404
        # Append at the end: next position = current max + 1 (0 for an empty set).
        nextpos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM asset_lesson_items WHERE lesson_id = ?",
            (lesson_id,),
        ).fetchone()[0]
        # INSERT OR IGNORE makes the re-add a no-op rather than an IntegrityError.
        conn.execute(
            "INSERT OR IGNORE INTO asset_lesson_items "
            "(lesson_id, library_id, position, added_at) VALUES (?, ?, ?, ?)",
            (lesson_id, library_id, nextpos, now_iso()),
        )
        _touch(conn, lesson_id)
        items = _item_rows_for_lesson(conn, lesson_id)
    return jsonify(ok=True, asset_count=len(items)), 200


@asset_lessons_bp.delete("/api/asset-lessons/<lesson_id>/items/<library_id>")
def remove_item(lesson_id: str, library_id: str):
    """Remove an asset from the lesson (the asset itself is untouched)."""
    with connect() as conn:
        if not _lesson_row(conn, lesson_id):
            return jsonify(error="lesson not found"), 404
        conn.execute(
            "DELETE FROM asset_lesson_items WHERE lesson_id = ? AND library_id = ?",
            (lesson_id, library_id),
        )
        _touch(conn, lesson_id)
        items = _item_rows_for_lesson(conn, lesson_id)
    return jsonify(ok=True, asset_count=len(items)), 200


@asset_lessons_bp.put("/api/asset-lessons/<lesson_id>/order")
def reorder_items(lesson_id: str):
    """Reorder the lesson's assets. Body: {order: [library_id, …]}.

    Only ids already in the lesson are repositioned (0-based, in the given order);
    any id not currently a member is ignored, and members absent from ``order``
    keep their relative order after the listed ones (defensive against a partial
    list). Atomic under the connection's autocommit-with-explicit-txn pattern.
    """
    data = request.get_json(silent=True) or {}
    order = data.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        return jsonify(error="order must be a list of library_id strings"), 400
    with connect() as conn:
        if not _lesson_row(conn, lesson_id):
            return jsonify(error="lesson not found"), 404
        current = {
            r["library_id"]: r["position"]
            for r in conn.execute(
                "SELECT library_id, position FROM asset_lesson_items WHERE lesson_id = ?",
                (lesson_id,),
            ).fetchall()
        }
        # Listed members first (in the requested order); then any unlisted members
        # appended in their existing order, so a partial list never drops a row.
        listed = [lib for lib in order if lib in current]
        rest = sorted((lib for lib in current if lib not in set(listed)),
                      key=lambda lib: current[lib])
        for pos, lib in enumerate([*listed, *rest]):
            conn.execute(
                "UPDATE asset_lesson_items SET position = ? WHERE lesson_id = ? AND library_id = ?",
                (pos, lesson_id, lib),
            )
        _touch(conn, lesson_id)
        items = _item_rows_for_lesson(conn, lesson_id)
    return jsonify(ok=True, items=items), 200
