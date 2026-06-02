"""lb-y9c (P5.2) — batch group tracking: ``batches`` table + ``jobs.batch_id``.

A Python migration (not ``.sql``) because the ``jobs.batch_id`` link is added via
``ALTER TABLE … ADD COLUMN``, which SQLite cannot guard with ``IF NOT EXISTS``.
Pure-SQL ``ADD COLUMN`` would crash-loop the runner: a crash between the ALTER
and the ledger INSERT means the next boot re-runs ALTER → "duplicate column".
This ``up(conn)`` follows the migration-author idempotency contract instead — it
is safe to *attempt* twice by checking ``PRAGMA table_info`` before adding the
column (the runner still commits it exactly once).

What it creates:

* ``batches`` — one row per submitted batch (a group id the fan-out hands back).
  Holds the batch's own metadata: how many rows parsed, how many were valid vs
  rejected, the rejected rows (``errors_json``) so a later GET can report them,
  and the source format. The per-job rows live in ``jobs``; this table is the
  group record, not a copy of them.
* ``jobs.batch_id`` — a nullable FK back to ``batches.id`` (ON DELETE SET NULL,
  matching the existing ``jobs.library_id`` soft-link style). A single-submit job
  leaves it NULL; a fan-out job carries its batch's id. Aggregate batch progress
  is then a plain ``SELECT status, COUNT(*) FROM jobs WHERE batch_id = ? GROUP BY
  status`` over the existing jobs lifecycle — reusing the jobs table / job-state
  reads, not a second source of truth.

SFW-only: schema plumbing for grouping render jobs; no restricted column,
content enum, or reference.
"""
from __future__ import annotations

import sqlite3


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # PRAGMA rows: (cid, name, type, notnull, dflt_value, pk); name is index 1.
    return any(r[1] == column for r in rows)


def up(conn: sqlite3.Connection) -> None:
    # ── batches: the group record ──────────────────────────────────────────
    # source = 'csv' | 'txt' | 'json' (how the spec arrived). errors_json is the
    # JSON list of rejected rows ({line, error, raw}) captured at submit time so
    # GET /api/jobs/batch/<id> can report them without re-parsing the upload.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batches (
            id              TEXT PRIMARY KEY,
            source          TEXT,                 -- 'csv' | 'txt' | 'json'
            title           TEXT,                 -- optional human label
            total_rows      INTEGER NOT NULL DEFAULT 0,   -- valid + rejected
            valid_count     INTEGER NOT NULL DEFAULT 0,   -- jobs actually enqueued
            rejected_count  INTEGER NOT NULL DEFAULT 0,
            errors_json     TEXT,                 -- JSON list of rejected rows
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    # ── jobs.batch_id: link a fan-out job to its batch (idempotent add) ─────
    if not _has_column(conn, "jobs", "batch_id"):
        conn.execute("ALTER TABLE jobs ADD COLUMN batch_id TEXT REFERENCES batches (id)")

    # Aggregate-progress query reads (batch_id, status); index the pair so the
    # GROUP BY count is cheap even with a large jobs table.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs (batch_id, status)")
