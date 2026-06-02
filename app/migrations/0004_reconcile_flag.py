"""lb-qtr (P10.3) — reconcile flag: ``library.missing_since``.

The full reconcile-on-boot (DB ↔ disk ↔ library, :mod:`app.reconcile`) needs a
*non-destructive* way to record that a ``library`` row's backing file has gone
missing from disk. The build plan is explicit: reconcile, do NOT purge — a
dangling row is flagged for review, never silently deleted (the bytes might be
recoverable; the owner decides).

This adds one nullable column:

* ``library.missing_since`` — the ISO timestamp at which reconcile first noticed
  this row's ``file_path`` was absent under ``STORAGE_DIR``. NULL = the file is
  present (the healthy default). It is the soft mirror of the existing
  ``deleted_at`` soft-delete column: a flag, not a tombstone, so the row (and its
  metadata) survives and the UI can surface "needs review" instead of 404-ing.

Self-healing is the point: reconcile SETs ``missing_since`` when a file vanishes
and CLEARs it back to NULL if the file reappears, so a restart converges the DB
to match disk reality without an operator touching anything.

A Python migration (not ``.sql``) because ``ALTER TABLE … ADD COLUMN`` cannot be
guarded with ``IF NOT EXISTS``; pure-SQL would crash-loop the runner if a crash
landed between the ALTER and the ledger INSERT (next boot → "duplicate column").
Following the lb-y9c (0003) precedent, ``up(conn)`` checks ``PRAGMA table_info``
first so it is safe to *attempt* twice (the runner still commits it once).

SFW-only: schema plumbing for consistency bookkeeping; no restricted column,
content enum, or reference.
"""
from __future__ import annotations

import sqlite3


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # PRAGMA rows: (cid, name, type, notnull, dflt_value, pk); name is index 1.
    return any(r[1] == column for r in rows)


def up(conn: sqlite3.Connection) -> None:
    # Idempotent ADD COLUMN (lb-y9c precedent): guard with table_info so a second
    # attempt before the ledger row exists doesn't raise "duplicate column".
    if not _has_column(conn, "library", "missing_since"):
        conn.execute("ALTER TABLE library ADD COLUMN missing_since TEXT")

    # Reconcile sweeps "live" (not soft-deleted) rows and flips this flag; index
    # the flag so a "show me everything flagged missing" query is cheap.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_missing "
        "ON library (missing_since, deleted_at)"
    )
