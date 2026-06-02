"""lb-review-regen — review verdicts + a regenerate version chain.

The owner reviews each generated image/film OK / Inte-OK with a note (mirroring
the atelier ``/admin/review`` UX the owner already knows), then iteratively
regenerates the rejects. Each regenerate is a NEW ``library`` row LINKED to the
one it was made from, so a subject/area asset becomes a *version chain* the
owner can extend across several turns — full history kept.

This migration adds the columns that carry both:

* ``library.review_ok``      — the verdict: ``NULL`` (unreviewed) | ``1`` (OK) |
  ``0`` (Inte-OK). Stored as a nullable INTEGER (not the atelier's TEXT enum) per
  the larbild spec: null/1/0 reads cleanly as "pending / kept / rejected".
* ``library.review_comment`` — the owner's free-text note (what to fix / why).
* ``library.reviewed_at``    — when the verdict was last set (UTC ISO string).
* ``library.parent_id``      — the ``library`` row this one was regenerated FROM
  (NULL for an original). A self-referential soft link forming the version chain;
  ON DELETE SET NULL so soft/hard-removing a parent never orphans a child write.
* ``jobs.parent_id``         — carried from a regenerate request through to the
  render worker, which copies it onto the new ``library`` row so the chain link
  survives the async render (the worker builds the library row from the job row,
  lb-ocx).

A Python migration (not ``.sql``) because every change is ``ALTER TABLE … ADD
COLUMN``, which SQLite cannot guard with ``IF NOT EXISTS``. A pure-SQL ADD COLUMN
would crash-loop the runner: a crash between the ALTER and the ledger INSERT means
the next boot re-runs ALTER → "duplicate column". This ``up(conn)`` follows the
migration-author idempotency contract instead — it checks ``PRAGMA table_info``
before adding each column (the runner still commits it exactly once). Indexes are
``CREATE INDEX IF NOT EXISTS`` so they are independently idempotent.

SFW-only: review-state + version-chain plumbing over the existing SFW library /
jobs tables; no restricted column, content enum, or reference.
"""
from __future__ import annotations

import sqlite3


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # PRAGMA rows: (cid, name, type, notnull, dflt_value, pk); name is index 1.
    return any(r[1] == column for r in rows)


def up(conn: sqlite3.Connection) -> None:
    # ── library: review verdict + note + timestamp ─────────────────────────
    # review_ok is a nullable INTEGER tri-state: NULL = unreviewed (pending),
    # 1 = OK, 0 = Inte-OK. A CHECK keeps any stray value out (only 0/1/NULL).
    if not _has_column(conn, "library", "review_ok"):
        conn.execute(
            "ALTER TABLE library ADD COLUMN review_ok INTEGER "
            "CHECK (review_ok IN (0, 1))"
        )
    if not _has_column(conn, "library", "review_comment"):
        conn.execute("ALTER TABLE library ADD COLUMN review_comment TEXT")
    if not _has_column(conn, "library", "reviewed_at"):
        conn.execute("ALTER TABLE library ADD COLUMN reviewed_at TEXT")

    # ── library.parent_id: the regenerate version chain ────────────────────
    # Self-referential soft link to the library row this one was regenerated
    # from. NULL = an original (chain root). ON DELETE SET NULL: removing a
    # parent must never block or orphan a child insert.
    if not _has_column(conn, "library", "parent_id"):
        conn.execute(
            "ALTER TABLE library ADD COLUMN parent_id TEXT "
            "REFERENCES library (id) ON DELETE SET NULL"
        )

    # ── jobs.parent_id: carry the chain link through the async render ──────
    # A regenerate enqueues a normal render job tagged with the parent's id; the
    # worker copies it onto the produced library row so the chain survives the
    # queue→worker hop (the worker builds the library row from the job row).
    if not _has_column(conn, "jobs", "parent_id"):
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN parent_id TEXT "
            "REFERENCES library (id) ON DELETE SET NULL"
        )

    # Pending-review browse + version-chain walk are the two new hot reads:
    #   * filter the gallery to review_ok IS NULL / = 0 (pending / rejected),
    #   * fetch a row's children (parent_id = ?) to render the version chain.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_review "
        "ON library (subject, area, review_ok, deleted_at, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_parent ON library (parent_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs (parent_id)"
    )
