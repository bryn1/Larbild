"""lb-svg-baseline — SVG-source provenance + a "preferred" flag on library rows.

The 123 Claude-authored fysik/teknik concept SVGs (skola rig) are imported into
the library as the DIAGRAM baseline for their concepts. They are exact, perfectly
Swedish-labelled vector diagrams — strictly better than diffusion/matplotlib for
these structured concepts — so an imported SVG is flagged the *preferred* version
for its concept, and the original vector source is kept on disk (not just the
rasterised PNG) so it can be served / edited as vector later.

This adds three nullable columns to ``library``:

* ``source``           — a provenance tag for *where this row came from*. NULL for
  an organically rendered asset (the GPU/procedural worker leaves it NULL, exactly
  as today); ``'skola-svg-baseline'`` for an SVG imported by this lane. A plain
  string (not a DB enum) — provenance tags are open-ended (a future import lane
  adds its own tag) and validated app-side, matching the lb-ah2 single-SoT,
  no-DB-enum decision for the taxonomy keys.
* ``svg_source_path``  — the STORAGE_DIR-relative path of the original ``.svg``
  vector kept alongside the rasterised PNG. NULL for a non-vector asset. Lets the
  Bibliotek / a later edit path serve or re-rasterise the true vector instead of
  the flattened PNG. (``file_path`` still points at the servable PNG so the
  existing gallery <img> path is unchanged — the SVG is the *extra* provenance.)
* ``preferred``        — a 0/1 flag: when several rows exist for the same concept
  bucket (e.g. a batch-rendered diffusion asset AND this exact SVG baseline), the
  ``preferred = 1`` one is the version the owner/UI should surface first. NULL/0 =
  not specially preferred (the default for every existing row); the SVG import
  sets it to 1. A nullable INTEGER tri-readable as "unset / not-preferred / 1 =
  preferred", with a CHECK pinning it to 0/1/NULL so a stray value can't land.

Self-describing + non-destructive: nothing existing is rewritten, the columns are
purely additive, and a row with all three NULL behaves exactly as a pre-0007 row.
The import lane (scripts/import_skola_svgs.py) writes distinct ``file_path``s, so
it never collides with the running render batch's UNIQUE(file_path) rows — both an
imported SVG baseline and a batch render for the same concept coexist, and
``preferred`` lets the review loop resolve which to show.

A Python migration (not ``.sql``) because every change is ``ALTER TABLE … ADD
COLUMN``, which SQLite cannot guard with ``IF NOT EXISTS``. A pure-SQL ADD COLUMN
would crash-loop the runner: a crash between the ALTER and the ledger INSERT means
the next boot re-runs ALTER → "duplicate column". This ``up(conn)`` follows the
migration-author idempotency contract (lb-y9c/0003 precedent): it checks
``PRAGMA table_info`` before adding each column (the runner still commits it
exactly once); indexes are ``CREATE INDEX IF NOT EXISTS`` so they are independently
idempotent. Re-running on an up-to-date DB does nothing.

SFW-only: provenance + preference plumbing over the existing SFW library table —
the imported SVGs are educational concept diagrams; no restricted column, content
enum, model, or reference.
"""
from __future__ import annotations

import sqlite3


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # PRAGMA rows: (cid, name, type, notnull, dflt_value, pk); name is index 1.
    return any(r[1] == column for r in rows)


def up(conn: sqlite3.Connection) -> None:
    # ── library.source: provenance tag (NULL = organically rendered) ───────
    if not _has_column(conn, "library", "source"):
        conn.execute("ALTER TABLE library ADD COLUMN source TEXT")

    # ── library.svg_source_path: the kept vector source (NULL = not a vector)
    if not _has_column(conn, "library", "svg_source_path"):
        conn.execute("ALTER TABLE library ADD COLUMN svg_source_path TEXT")

    # ── library.preferred: 0/1 "show this version first" flag ──────────────
    # CHECK keeps any stray value out (only 0/1/NULL). NULL/0 = not preferred
    # (every existing row's default); the SVG import sets it to 1.
    if not _has_column(conn, "library", "preferred"):
        conn.execute(
            "ALTER TABLE library ADD COLUMN preferred INTEGER "
            "CHECK (preferred IN (0, 1))"
        )

    # "Show me the preferred / SVG-baseline rows for a bucket" is the new hot
    # read (the review loop + the gallery's preferred-first ordering); index the
    # bucket + flag so it stays cheap alongside a large rendered library.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_preferred "
        "ON library (subject, area, image_type, preferred, deleted_at, created_at DESC)"
    )
    # Provenance scans ("everything from the SVG baseline lane") — cheap by tag.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_source ON library (source)"
    )
