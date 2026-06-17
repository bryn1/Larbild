"""Media serving — serve a generated artefact's bytes from STORAGE_DIR (lb-gallery).

The Bibliotek gallery renders each generated image with an ``<img>`` whose ``src``
is ``/media/<file_path>``, where ``file_path`` is the STORAGE_DIR-relative path a
``library`` row carries (``output/<subject>/<area>/<imageType>/<file>``). This
blueprint is the route that turns that URL back into the PNG (or any artefact)
on disk.

Without it, ``/api/library`` reports counts and the gallery knows *which* files
exist, but every ``<img>`` 404s — exactly the "numbers update but no images
show" symptom. This serves the bytes with the correct ``Content-Type``.

Security — this is the one route that maps a client-supplied path onto the
filesystem, so it is **path-traversal-safe by construction**:

  * The requested path is resolved against STORAGE_DIR and the *real* resolved
    path (symlinks included) must stay inside STORAGE_DIR — a ``..`` escape, an
    absolute path, or a symlink pointing outside all 404 (never 500, never leak).
  * Only regular files under STORAGE_DIR are served; a directory or a missing
    path is a 404.

Owner-only LAN tool: the app has no auth layer (single teacher on the LAN), so
this route is open on the LAN exactly like the rest of the app. If/when an auth
layer is added to the app, gate this route the same way (it is the only one that
streams arbitrary stored bytes). SFW-only: serves the SFW library's artefacts
and nothing else — it can only ever reach files under the configured STORAGE_DIR.
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from flask import Blueprint, abort, current_app, send_file

media_bp = Blueprint("media", __name__)

# Public read-only mode (bilder.sibbamala.com): same switch as
# Config.PUBLIC_READONLY, read here at import time. When set, /media only serves
# the bytes of files referenced by an APPROVED (review_ok = 1), non-deleted
# library row — so a guessed path to an unreviewed/rejected image (the file_path
# scheme is semantic, e.g. .../diagram/<concept>__authored-svg.png) cannot leak
# unapproved content the gallery already hides.
_PUBLIC_READONLY = os.environ.get("LARBILD_PUBLIC_READONLY", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _is_visible_media(relpath: str) -> bool:
    """True if ``relpath`` is the file/thumb/svg of a PUBLICLY-VISIBLE library row.

    The public instance serves only the bytes of a row the gallery itself would
    show. A library row references its bytes via ``file_path`` (the render),
    ``thumb_path`` (the thumbnail), or ``svg_source_path`` (the authored-SVG
    baseline). The requested STORAGE_DIR-relative path must match one of those on
    a non-soft-deleted row that passes the public visibility predicate
    (visible_predicate_sql) — i.e. approved everywhere, plus un-reviewed for the
    crowdsource subjects (fysik/matematik/teknik), and NEVER rejected. This is
    the SAME predicate the listing + count queries use, so a tile the gallery
    renders always has servable bytes and a rejected/other-subject-pending path
    404s identically to a missing one (no existence oracle).
    """
    from ..db import connect
    from ..public_visibility import visible_predicate_sql

    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT 1 FROM library
            WHERE deleted_at IS NULL AND {visible_predicate_sql(_PUBLIC_READONLY)}
              AND (file_path = ? OR thumb_path = ? OR svg_source_path = ?)
            LIMIT 1
            """,
            (relpath, relpath, relpath),
        ).fetchone()
    return row is not None


def _storage_root() -> Path:
    """The configured STORAGE_DIR as an absolute, symlink-resolved path.

    Resolved once per request against the live Flask config so a test pointing
    STORAGE_DIR at a temp dir is honoured. ``strict=False`` so a not-yet-created
    storage dir doesn't crash the resolve (the containment check below still
    holds: a non-existent candidate simply won't be under it / won't exist).
    """
    return Path(current_app.config["STORAGE_DIR"]).resolve(strict=False)


def _safe_resolve(relpath: str) -> Path:
    """Resolve ``relpath`` under STORAGE_DIR or abort 404 — traversal-safe.

    The single chokepoint that maps a client path onto disk. Steps:

      1. Reject an absolute path outright (``/etc/passwd`` must not be served).
      2. Join under STORAGE_DIR and resolve symlinks (``strict=False`` so a
         missing file resolves to a path we can containment-check, then 404 on
         the existence test rather than raising).
      3. Containment: the resolved real path must be STORAGE_DIR itself or a
         descendant. A ``..`` escape or an out-of-tree symlink fails here → 404.

    Any failure is a 404 (not a 403/500): we never confirm whether an
    out-of-bounds path exists, and a malformed path can't crash the handler.
    """
    # An absolute client path (or a Windows drive) can never be in-bounds.
    candidate = Path(relpath)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        abort(404)

    root = _storage_root()
    try:
        resolved = (root / candidate).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # e.g. a path with a NUL byte, or a symlink loop — treat as not found.
        abort(404)

    # Containment: resolved must be root or strictly under it.
    if resolved != root and root not in resolved.parents:
        abort(404)
    return resolved


@media_bp.get("/media/<path:relpath>")
def serve_media(relpath: str):
    """Serve a stored artefact at ``/media/<file_path>`` with the right MIME.

    ``relpath`` is the STORAGE_DIR-relative ``library.file_path`` (e.g.
    ``output/fysik/rorelse/diagram/larbild_00001_.png``). Path-traversal-safe
    (see :func:`_safe_resolve`); a directory, a missing file, or any escape
    attempt returns 404. The ``Content-Type`` is guessed from the extension
    (``image/png``, ``image/jpeg``, ``video/mp4`` for the film artefacts, …),
    defaulting to ``application/octet-stream`` for an unknown type.

    Read-only: streams the bytes off disk, never writes.
    """
    # PUBLIC_READONLY: only an approved row's bytes may be served. Checked before
    # touching disk so a guessed unapproved path 404s identically to a missing
    # one (never confirms an unapproved file exists). The private instance serves
    # any in-bounds file as before.
    if _PUBLIC_READONLY and not _is_visible_media(relpath):
        abort(404)

    resolved = _safe_resolve(relpath)
    if not resolved.is_file():
        # Missing, or a directory — nothing to serve.
        abort(404)

    mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    # conditional=True → honour If-Modified-Since/Range so the browser caches
    # thumbnails and can range-request the (larger) film artefacts.
    return send_file(str(resolved), mimetype=mime, conditional=True)
