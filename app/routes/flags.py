"""Visitor image flags — the ONE sanctioned public write + the LAN review API.

Two distinct surfaces over the ``flags`` table (migration 0008):

PUBLIC (bilder.sibbamala.com, :6869) — ``POST /api/flag``:
    The single write the internet-facing read-only instance accepts. A visitor
    browsing the crowdsource gallery (fysik/matematik/teknik publish-all) reports
    an image as wrong/broken. Hardened on every axis (see below). It is exempt
    from the global read-only 405 guard via ``request.environ`` marking — the
    factory wires the exemption — but nothing else public can write.

PRIVATE (LAN tool, :6868) — ``GET /api/flags`` + ``POST /api/flags/<id>/resolve``:
    The owner's review surface: list OPEN flags (joined to the flagged image's
    library row so the owner sees what was reported and can open/fix/approve/
    reject it), and move a flag to ``resolved``/``dismissed``. NEVER registered on
    the public instance.

Security posture of the public POST (the only attacker-reachable write):
  * Method + size: a JSON or form body only; the request body is already capped
    by ``MAX_CONTENT_LENGTH`` (32 MiB) app-wide, and we read only two small
    fields.
  * ``image_id`` MUST reference a row that is PUBLICLY VISIBLE (the same
    visibility predicate the gallery uses) — you cannot flag an image you cannot
    see (a rejected/other-subject-pending/unknown id is a 404, no existence
    oracle distinction).
  * ``reason`` is OPTIONAL, coerced to str, control-characters stripped, collapsed
    whitespace, HTML-escaped (so nothing scriptable is ever stored), and hard
    length-capped. Empty → NULL. No markup, no script, bounded size.
  * Per-source rate limit: a salted SHA-256 of the client IP (the raw IP is never
    stored) bounds how many flags one source may file per rolling window; over
    the cap → 429. A duplicate OPEN flag from the same source on the same image
    is collapsed (idempotent 200) rather than stacked, so refresh-spam can't
    inflate counts.
  * The response NEVER reveals flag state, counts, or other visitors' reports —
    it returns only ``{"ok": true}``. The public site shows a flag BUTTON, never
    flag status.

SFW-only: visitor feedback over the SFW library; no restricted column/content.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
from typing import Any, Optional

from flask import Blueprint, current_app, jsonify, request

from ..db import connect, connect_writer, new_id, now_iso
from ..public_visibility import (
    public_readonly_from_env,
    visible_predicate_sql,
)

# Capture the public/readonly mode ONCE at import (matching the other route
# modules), and pass it to visible_predicate_sql so the predicate can never
# diverge from this module's wiring.
_PUBLIC_READONLY = public_readonly_from_env()

public_flag_bp = Blueprint("public_flag", __name__)
flags_admin_bp = Blueprint("flags_admin", __name__)

# A visitor reason is a short note, not an essay. Cap hard so a row can't be
# bloated and a paste can't be used as a storage-abuse vector.
_REASON_MAX = 500

# Per-source rate limit: at most this many flags from one client_ip_hash within
# the rolling window (seconds). Generous enough for honest use across a session,
# tight enough that scripted spam hits 429 fast.
_RATE_MAX = 20
_RATE_WINDOW_S = 3600

# Strip ASCII control characters (except none are kept) before storing, so a
# reason can never carry NULs/escape sequences/newlines that break a log or UI.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


def _ip_salt() -> str:
    """Process secret used to salt the IP hash (raw IPs are never stored).

    Falls back to the Flask SECRET_KEY (always set — persistent on the private
    instance, ephemeral on the public one). A per-deploy salt is plenty: the hash
    only has to be stable within a process to rate-limit + dedup, and not
    reversible to the IP.
    """
    return (
        os.environ.get("LARBILD_FLAG_IP_SALT")
        or str(current_app.config.get("SECRET_KEY", "larbild-flag"))
    )


def _client_ip_hash() -> str:
    """Salted SHA-256 of the best-effort client IP (privacy: raw IP not stored).

    Honours a single ``X-Forwarded-For`` hop (cloudflared sets it) then falls
    back to the socket peer. Only the hash is persisted/compared.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    ip = (fwd.split(",")[0].strip() if fwd else "") or (request.remote_addr or "")
    return hashlib.sha256(f"{_ip_salt()}:{ip}".encode("utf-8")).hexdigest()


def _sanitise_reason(raw: Any) -> Optional[str]:
    """Coerce a submitted reason to safe, bounded plain text, or None.

    str-only; control chars removed; whitespace collapsed; HTML-escaped so no
    markup/script can ever be stored or later rendered; hard length-capped.
    Empty after cleaning → None (an omitted/blank reason is allowed).
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    cleaned = _CONTROL_RE.sub("", raw)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if not cleaned:
        return None
    cleaned = cleaned[:_REASON_MAX]
    # Escape AFTER capping so the cap is on visible characters, then escaping can
    # only shorten-by-expansion safely (still well under any column limit).
    return html.escape(cleaned, quote=True)


def _request_body() -> dict:
    """Parse a JSON or form body into a dict (never raise on a bad body)."""
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    if request.form:
        return dict(request.form)
    return {}


def _visible_row_exists(library_id: str) -> bool:
    """True if ``library_id`` is a publicly-VISIBLE, non-deleted library row.

    Uses the SAME visibility predicate as the gallery/media gates, so a visitor
    can only flag an image the public site actually shows. A rejected image, an
    other-subject pending image, a soft-deleted row, or an unknown id all read as
    not-visible → the endpoint 404s (no distinction = no existence oracle).
    """
    with connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM library "
            f"WHERE id = ? AND deleted_at IS NULL AND {visible_predicate_sql(_PUBLIC_READONLY)} "
            f"LIMIT 1",
            (library_id,),
        ).fetchone()
    return row is not None


@public_flag_bp.post("/api/flag")
def post_flag():
    """Record a visitor's "this image is wrong/broken" report (the one public write).

    Body (JSON or form): ``{"image_id": "<library id>", "reason"?: "<short text>"}``.

      * ``image_id`` is required and must be a publicly-visible image (else 404).
      * ``reason`` is optional, sanitised to bounded plain text (no markup).

    Rate-limited per source (429 over the cap) and idempotent for a duplicate open
    report from the same source on the same image. Returns only ``{"ok": true}`` —
    never any flag state, count, or other reports. 400 on a missing image_id.
    """
    data = _request_body()
    library_id = (data.get("image_id") or data.get("library_id") or "")
    if not isinstance(library_id, str):
        library_id = ""
    library_id = library_id.strip()
    if not library_id:
        return jsonify(error="image_id is required"), 400

    # You can only flag what the public gallery shows.
    if not _visible_row_exists(library_id):
        return jsonify(error="image not found"), 404

    reason = _sanitise_reason(data.get("reason"))
    iphash = _client_ip_hash()

    # The single write. Open a deliberately-writable connection (the public
    # instance's default connect() is read-only) — this is the one sanctioned
    # public write, and only this caller uses connect_writer.
    with connect_writer() as conn:
        # Rate limit: count this source's flags in the rolling window.
        cutoff = "datetime('now', ?)"
        recent = conn.execute(
            f"SELECT COUNT(*) FROM flags "
            f"WHERE client_ip_hash = ? AND created_at >= {cutoff}",
            (iphash, f"-{_RATE_WINDOW_S} seconds"),
        ).fetchone()[0]
        if recent >= _RATE_MAX:
            return jsonify(error="too many reports — try again later"), 429

        # Idempotent: collapse a duplicate OPEN flag from this source on this
        # image (refresh/double-click spam) into the existing one.
        dup = conn.execute(
            "SELECT 1 FROM flags "
            "WHERE library_id = ? AND client_ip_hash = ? AND status = 'open' "
            "LIMIT 1",
            (library_id, iphash),
        ).fetchone()
        if dup is None:
            conn.execute(
                "INSERT INTO flags (id, library_id, reason, status, "
                "client_ip_hash, created_at) VALUES (?, ?, ?, 'open', ?, ?)",
                (new_id(), library_id, reason, iphash, now_iso()),
            )

    # Deliberately minimal: no flag state ever leaks to the public.
    return jsonify(ok=True), 201


# ── LAN-only owner review surface (NEVER registered on the public instance) ──


@flags_admin_bp.get("/api/flags")
def list_flags():
    """List flagged images for the owner's review (private :6868 only).

    Query: ``status`` = ``open`` (default) | ``resolved`` | ``dismissed`` | ``all``.
    Groups by the flagged image so the owner sees ONE entry per image with its
    open-flag count, the most recent reason, when it was last reported, and the
    image's current review state + a /media URL — enough to open it, judge it,
    and fix/approve/reject it. Newest-reported first.
    """
    status = (request.args.get("status") or "open").strip().lower()
    where_status = "" if status == "all" else "WHERE f.status = ?"
    params: list[Any] = [] if status == "all" else [status]
    # One row per image: count of matching flags, latest reason + time.
    sql = f"""
        SELECT
            l.id                AS library_id,
            l.subject, l.area, l.image_type, l.title,
            l.file_path, l.thumb_path, l.svg_source_path,
            l.review_ok, l.review_comment,
            COUNT(f.id)         AS flag_count,
            MAX(f.created_at)   AS last_flagged_at
        FROM flags f
        JOIN library l ON l.id = f.library_id
        {where_status}
        GROUP BY l.id
        ORDER BY last_flagged_at DESC
    """
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Latest reason for this image among the matching-status flags.
            reason_params = [r["library_id"]]
            reason_sql = (
                "SELECT reason FROM flags WHERE library_id = ? "
                + ("" if status == "all" else "AND status = ? ")
                + "AND reason IS NOT NULL ORDER BY created_at DESC LIMIT 1"
            )
            if status != "all":
                reason_params.append(status)
            rr = conn.execute(reason_sql, reason_params).fetchone()
            d["last_reason"] = rr["reason"] if rr else None
            relp = r["thumb_path"] or r["file_path"]
            d["thumb_url"] = f"/media/{relp}" if relp else None
            d["url"] = f"/media/{r['file_path']}" if r["file_path"] else None
            out.append(d)
    return jsonify(flags=out, status=status, count=len(out)), 200


@flags_admin_bp.post("/api/flags/<flag_target>/resolve")
def resolve_flags(flag_target: str):
    """Move an image's flags out of ``open`` (private :6868 only).

    ``flag_target`` is a LIBRARY id (the review view acts per image): all OPEN
    flags on that image move to the requested status. Body: ``{"status": "resolved"
    | "dismissed"}`` (default ``resolved``). Returns the count updated.
    """
    data = _request_body()
    new_status = (data.get("status") or "resolved").strip().lower()
    if new_status not in ("resolved", "dismissed"):
        return jsonify(error="status must be 'resolved' or 'dismissed'"), 400
    with connect_writer() as conn:
        cur = conn.execute(
            "UPDATE flags SET status = ?, resolved_at = ? "
            "WHERE library_id = ? AND status = 'open'",
            (new_status, now_iso(), flag_target),
        )
        updated = cur.rowcount
    return jsonify(ok=True, updated=updated, status=new_status), 200
