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
from ..db import connect, now_iso
from ..jobs import QueueFull, enqueue_job

library_bp = Blueprint("library", __name__)

# Owner notes are plain text; cap so a runaway paste can't bloat the row (matches
# the atelier review cap). The same cap is applied to a regenerate's fresh note.
_COMMENT_MAX = 4000

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
    "level",
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
    # Review state (lb-review-regen): tri-state verdict + note + when. review_ok
    # is NULL (unreviewed) | 1 (OK) | 0 (Inte-OK).
    "review_ok",
    "review_comment",
    "reviewed_at",
    # The library row this one was regenerated FROM (NULL = an original); the
    # frontend walks it to show the version chain.
    "parent_id",
    # SVG-baseline provenance (migration 0007 / lb-svg-baseline): where the row
    # came from, the kept vector source, and whether it's the preferred version
    # for its bucket. ``source='skola-svg-baseline'`` rows are the hand-authored
    # concept diagrams imported from the skola rig; the UI reads ``preferred`` to
    # surface them first and ``svg_source_path`` to offer the true vector.
    "source",
    "svg_source_path",
    "preferred",
)

# Map a ?review= filter value to a SQL predicate over review_ok. "pending" =
# unreviewed (NULL), "ok" = kept (1), "notok" = rejected (0); anything else (or
# absent) applies no filter. Centralised so the items endpoint and any future
# count read agree on the verdict semantics.
_REVIEW_FILTER_SQL = {
    "pending": "review_ok IS NULL",
    "ok": "review_ok = 1",
    "notok": "review_ok = 0",
}


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
    # The true vector source (when this is an SVG-baseline row) — served via the
    # same /media route so the UI can offer "open/edit as vector"; None for a
    # raster-only asset. The PNG ``url`` stays the default <img> src so existing
    # tiles are unchanged; ``svg_url`` is the extra capability.
    d["svg_url"] = _media_url(row["svg_source_path"]) if "svg_source_path" in row.keys() else None
    area_node = taxonomy.area(row["subject"], row["area"])
    d["area_name"] = (area_node["name"] if area_node else None) or row["area"]
    # Stable display alias the frontend already reads.
    d["generated_at"] = row["created_at"]
    return d


def _node_items(
    subject: str,
    area: str,
    image_type: Optional[str],
    review: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch the browsable ``library`` rows for one Bibliotek node, newest first.

    Scopes to ``subject`` + ``area`` (both required) and, when given,
    ``image_type`` (the imageTypes slug). Excludes rows that are NOT browsable:

      * soft-deleted (``deleted_at IS NOT NULL``) — removed from the library, and
      * dangling (``missing_since IS NOT NULL``) — the file vanished off disk
        (flagged by the boot reconcile, lb-qtr); serving its URL would 404, so it
        stays out of the gallery rather than render a broken tile.

    ``review`` (lb-review-regen) narrows by verdict — ``"pending"`` (unreviewed),
    ``"ok"`` (kept), ``"notok"`` (rejected); any other/None value applies no
    verdict filter (the whole node). Used by the review UI's pending/not-ok
    filter.

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
    review_clause = _REVIEW_FILTER_SQL.get(review or "")
    if review_clause:
        sql += f" AND {review_clause}"
    # Preferred-first (lb-svg-baseline): a ``preferred=1`` row — the hand-authored
    # SVG concept-diagram baseline — sorts ahead of an organically rendered asset
    # in the same bucket, so the owner sees the exact vector diagram on top while
    # any batch render for the same concept stays available below. Within each
    # preference tier the existing newest-first order holds. ``COALESCE`` keeps a
    # NULL ``preferred`` (every pre-0007 / batch row) at the un-preferred tier.
    sql += " ORDER BY COALESCE(preferred, 0) DESC, created_at DESC, id DESC"
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
      * ``review`` (optional) — verdict filter for the review UI:
        ``pending`` (unreviewed) | ``ok`` (kept) | ``notok`` (Inte-OK). Any other
        value applies no verdict filter.

    ``subject``/``area`` are validated against the taxonomy SoT (lb-582): an
    unknown pair returns ``[]`` with HTTP 200 (a valid-but-empty node looks the
    same to the gallery — "nothing here yet" — rather than erroring). A missing
    required param is a 400. Each item carries a servable ``url`` (+ ``thumb_url``)
    so the Bibliotek view can render a thumbnail and a full-size view directly,
    plus the review state (``review_ok`` / ``review_comment`` / ``reviewed_at``)
    and ``parent_id`` (the row it was regenerated from) for the review + version
    UI.

    Shape::

        {
          "subject": "fysik", "area": "rorelse", "image_type": "diagram"|null,
          "review": "pending"|"ok"|"notok"|null,
          "items": [
            { "id", "kind", "title", "prompt", "lora", "seed", "backend",
              "width", "height", "area_name", "generated_at",
              "review_ok", "review_comment", "reviewed_at", "parent_id",
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
    review = (request.args.get("review") or "").strip().lower() or None
    # Normalise an unrecognised verdict filter to None so the response echoes
    # only a real filter (and applies none) — never surfaces a typo as state.
    if review not in _REVIEW_FILTER_SQL:
        review = None
    if not subject or not area:
        return jsonify(error="subject and area are required"), 400

    # Unknown taxonomy node → empty (not an error): the gallery shows the
    # "nothing here yet" state, same as a real-but-empty node.
    if not taxonomy.is_valid_area(subject, area):
        items: list[dict[str, Any]] = []
    else:
        items = _node_items(subject, area, image_type, review)

    return (
        jsonify(
            subject=subject,
            area=area,
            image_type=image_type,
            review=review,
            items=items,
            count=len(items),
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
        200,
    )


# ── review verdict + regenerate (lb-review-regen) ───────────────────────────
# The owner reviews each generated asset OK / Inte-OK with a note (mirroring the
# atelier /admin/review UX), then regenerates the rejects — each regen a NEW
# library row LINKED to the one it was made from (a version chain), so the same
# subject/area asset can be improved across several turns with full history kept.


def _get_library_row(lib_id: str) -> Optional[dict]:
    """The full ``library`` row as a dict, or None. Soft-deleted rows count as
    absent (they are not part of the browsable library, so reviewing or
    regenerating one is a 404 — never a silent write to a deleted asset)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM library WHERE id = ? AND deleted_at IS NULL", (lib_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def _review_state(lib_id: str) -> dict[str, Any]:
    """Just the review fields of a row, for the review endpoint's response.

    Returns the persisted verdict/note/timestamp so the UI can confirm what it
    wrote without re-fetching the whole gallery. Keys mirror the item contract.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id, review_ok, review_comment, reviewed_at "
            "FROM library WHERE id = ?",
            (lib_id,),
        ).fetchone()
    return {
        "id": row["id"],
        "review_ok": row["review_ok"],
        "review_comment": row["review_comment"],
        "reviewed_at": row["reviewed_at"],
    }


@library_bp.post("/api/library/<lib_id>/review")
def post_review(lib_id: str):
    """Set the owner's review verdict + note on a generated asset.

    Body (JSON or form): ``{review_ok?, comment?}`` — a PARTIAL update, so the
    keys *present* are written and an absent key is left untouched (toggling the
    verdict alone never wipes the comment, and vice-versa — matches the atelier).

      * ``review_ok`` — ``1``/``true`` (OK) | ``0``/``false`` (Inte-OK) |
        ``null`` (clear back to unreviewed/pending). Anything else → 400.
      * ``comment`` (alias ``review_comment``) — free text (capped); ``null``
        clears it.

    Whenever either field is written, ``reviewed_at`` is stamped (UTC ISO) so the
    UI can show when the verdict was last touched. 404 if the asset is unknown or
    soft-deleted. Returns the persisted review state.
    """
    if _get_library_row(lib_id) is None:
        return jsonify(error="library item not found", id=lib_id), 404

    data = _review_body()
    sets: list[str] = []
    params: list[Any] = []

    if "review_ok" in data:
        verdict = _coerce_verdict(data.get("review_ok"))
        if verdict is _INVALID:
            return jsonify(
                error="review_ok must be 1/true (OK), 0/false (Inte-OK), or null"
            ), 400
        sets.append("review_ok = ?")
        params.append(verdict)

    if "comment" in data or "review_comment" in data:
        raw = data.get("comment", data.get("review_comment"))
        if raw is None:
            comment: Optional[str] = None
        elif isinstance(raw, str):
            comment = raw.strip()[:_COMMENT_MAX] or None
        else:
            return jsonify(error="comment must be a string or null"), 400
        sets.append("review_comment = ?")
        params.append(comment)

    if not sets:
        return jsonify(
            error="nothing to update — send review_ok and/or comment"
        ), 400

    # Any verdict/note write stamps reviewed_at so the UI can show "reviewed when".
    sets.append("reviewed_at = ?")
    params.append(now_iso())
    params.append(lib_id)
    with connect() as conn:
        conn.execute(f"UPDATE library SET {', '.join(sets)} WHERE id = ?", params)

    return jsonify(_review_state(lib_id)), 200


# Sentinel distinguishing a rejected verdict value from a legitimate None
# (null = clear back to pending is a real, accepted value).
_INVALID = object()


def _coerce_verdict(value: Any) -> Any:
    """Map a request ``review_ok`` to the stored tri-state, or ``_INVALID``.

    Accepts the JSON/int/bool/string spellings a client might send and pins them
    to the DB shape: ``None`` (clear), ``1`` (OK), ``0`` (Inte-OK). 1/0, true/
    false, and the strings "1"/"0"/"true"/"false"/"ok"/"notok" all resolve;
    anything else is ``_INVALID`` so the caller answers 400 rather than writing a
    junk verdict.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool before int — bool is an int subclass
        return 1 if value else 0
    if isinstance(value, int):
        if value in (0, 1):
            return value
        return _INVALID
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "ok"):
            return 1
        if v in ("0", "false", "notok", "not_ok", "inte-ok", "inteok"):
            return 0
        if v in ("", "null", "none"):
            return None
    return _INVALID


def _review_body() -> dict:
    """The JSON/form body as a dict (empty if absent/non-object)."""
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
    if request.form:
        return dict(request.form)
    return {}


def _guided_prompt(base_prompt: Optional[str], comment: Optional[str]) -> str:
    """Fold the owner's correction note into the regenerate prompt.

    The regenerate re-uses the original prompt and INCORPORATES the review note as
    correction guidance so the next render addresses what was wrong. The note is
    appended as an explicit, labelled correction clause (kept short + plain) — a
    minimal, model-agnostic approach that needs no LLM round-trip: a clear
    "Correction:" suffix steers a txt2img prompt without rewriting the base. If
    there is no note (e.g. a plain re-roll), the original prompt is returned
    unchanged. SFW: the note is the owner's pedagogical correction, nothing else.
    """
    base = (base_prompt or "").strip()
    note = (comment or "").strip()
    if not note:
        return base
    suffix = f"Correction: {note}"
    return f"{base}. {suffix}" if base else suffix


@library_bp.post("/api/library/<lib_id>/regenerate")
def post_regenerate(lib_id: str):
    """Regenerate an asset as a NEW, linked version — the core review→fix loop.

    Enqueues a fresh render job for the SAME subject/area/imageType as the source
    row, re-using its prompt and INCORPORATING the owner's correction note so the
    next render addresses what was flagged. The produced ``library`` row is LINKED
    to the source via ``parent_id`` (a version chain), so the owner can regenerate
    across several turns — each a new version, full history kept.

    Body (JSON/form, all optional):
      * ``comment`` (alias ``review_comment``) — a FRESH correction note for this
        round. When given it both (a) guides this regenerate and (b) is persisted
        onto the source row's review (so the source records why it was redone).
        When omitted, the source row's existing ``review_comment`` (the verdict
        the owner already left) guides the regenerate.
      * ``seed`` — pin a seed; omitted → a fresh random seed each round (so a
        re-roll actually differs).

    Chain semantics: a regenerate always links to the row the request targeted
    (``parent_id = <lib_id>``). Regenerating the LATEST version again therefore
    extends the chain one deeper; regenerating an older version branches from it —
    both are kept, nothing is overwritten.

    404 if the source is unknown/soft-deleted. 400 if the source is a film (v1
    regenerates stills only — the render worker is bild-only; films are a later
    pass). 429 if the render queue is full. Returns
    ``202 {job_id, status:"queued", parent_id}``.
    """
    parent = _get_library_row(lib_id)
    if parent is None:
        return jsonify(error="library item not found", id=lib_id), 404
    if parent.get("kind") != "bild":
        # The render worker handles kind='bild' only (render_job); a film
        # regenerate would enqueue an un-renderable job. Fail loudly.
        return jsonify(
            error="regenerate supports images (kind='bild') only; "
                  "film regeneration is not available yet",
            id=lib_id, kind=parent.get("kind"),
        ), 400

    data = _review_body()

    # A fresh note (if any) guides this round AND is recorded on the source row's
    # review — so the source shows the correction that produced the next version.
    fresh = data.get("comment", data.get("review_comment"))
    fresh_comment: Optional[str] = None
    if isinstance(fresh, str):
        fresh_comment = fresh.strip()[:_COMMENT_MAX] or None
    # The guiding note: this round's fresh note if sent, else the verdict note the
    # owner already left on the source.
    guide_comment = fresh_comment if fresh_comment is not None else parent.get("review_comment")

    # seed: optional int; reject a non-int rather than silently coercing. Omitted
    # → None, so the worker rolls a fresh random seed (a real re-roll).
    seed = data.get("seed")
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            return jsonify(error="seed must be an integer"), 400

    prompt = _guided_prompt(parent.get("prompt"), guide_comment)

    try:
        job_id = enqueue_job(
            kind="bild",
            subject=parent["subject"],
            area=parent["area"],
            image_type=parent.get("image_type"),
            video_type=None,
            title=parent.get("title"),
            prompt=prompt,
            seed=seed,
            # The library row this render is made FROM — the worker copies it onto
            # the produced row so the version chain is preserved (lb-review-regen).
            parent_id=lib_id,
            # Provenance of the regenerate, for debugging the chain.
            params={
                "regenerate_of": lib_id,
                "guidance": guide_comment,
                "source_prompt": parent.get("prompt"),
            },
        )
    except QueueFull as e:
        return jsonify(
            error="render queue is full, try again shortly",
            depth=e.depth, limit=e.limit,
        ), 429

    # Persist the fresh correction note onto the source row's review so the chain
    # records why this version was made (best-effort — the job is already queued;
    # a note-write hiccup must not fail the regenerate the owner just triggered).
    if fresh_comment is not None:
        try:
            with connect() as conn:
                conn.execute(
                    "UPDATE library SET review_comment = ?, reviewed_at = ? WHERE id = ?",
                    (fresh_comment, now_iso(), lib_id),
                )
        except Exception:  # noqa: BLE001 — review-note mirror is best-effort
            pass

    return jsonify(job_id=job_id, status="queued", parent_id=lib_id), 202
