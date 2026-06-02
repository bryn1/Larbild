"""Tests for migration 0005 — review verdicts + the regenerate version chain.

Acceptance (lb-review-regen):
  * a fresh DB carries the new columns: library.review_ok / review_comment /
    reviewed_at / parent_id, and jobs.parent_id;
  * review_ok is a tri-state INTEGER constrained to NULL / 0 / 1 (a CHECK rejects
    anything else);
  * the version-chain + pending-review indexes exist;
  * the migration is idempotent (a second init_db applies nothing new) and the
    runner records version 5 exactly once.

These run the real runner via init_db (reconcile-on-boot) — the same path every
gunicorn worker takes — so the test proves the column adds survive a re-boot
without the "duplicate column" crash a naive SQL ADD COLUMN would cause.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

from app.db import connect, init_db
from app import migrations


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


# ── columns added ───────────────────────────────────────────────────────────


def test_library_gains_review_and_parent_columns(tmp_path):
    db = tmp_path / "m.db"
    init_db(db)
    with connect(db) as conn:
        cols = _cols(conn, "library")
    assert {"review_ok", "review_comment", "reviewed_at", "parent_id"} <= cols


def test_jobs_gains_parent_id(tmp_path):
    db = tmp_path / "m.db"
    init_db(db)
    with connect(db) as conn:
        assert "parent_id" in _cols(conn, "jobs")


def test_review_and_parent_indexes_exist(tmp_path):
    db = tmp_path / "m.db"
    init_db(db)
    with connect(db) as conn:
        lib_idx = _indexes(conn, "library")
        job_idx = _indexes(conn, "jobs")
    assert "idx_library_review" in lib_idx
    assert "idx_library_parent" in lib_idx
    assert "idx_jobs_parent" in job_idx


# ── review_ok CHECK constraint (NULL | 0 | 1) ───────────────────────────────


def _insert_lib(conn, review_ok):
    rid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO library (id, kind, subject, area, image_type, title,
                             file_path, review_ok, created_at)
        VALUES (?, 'bild', 'fysik', 'ljud', 'diagram', 't', ?, ?, datetime('now'))
        """,
        (rid, f"output/fysik/ljud/diagram/{rid}.png", review_ok),
    )
    return rid


def test_review_ok_accepts_null_zero_one(tmp_path):
    db = tmp_path / "m.db"
    init_db(db)
    with connect(db) as conn:
        for v in (None, 0, 1):
            _insert_lib(conn, v)  # must not raise


def test_review_ok_rejects_other_values(tmp_path):
    db = tmp_path / "m.db"
    init_db(db)
    with connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_lib(conn, 2)


# ── parent_id self-reference + ON DELETE SET NULL ───────────────────────────


def test_parent_id_links_and_nulls_on_parent_delete(tmp_path):
    db = tmp_path / "m.db"
    init_db(db)
    with connect(db) as conn:
        parent = _insert_lib(conn, None)
        child = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO library (id, kind, subject, area, image_type, title,
                                 file_path, parent_id, created_at)
            VALUES (?, 'bild', 'fysik', 'ljud', 'diagram', 'c', ?, ?, datetime('now'))
            """,
            (child, f"output/fysik/ljud/diagram/{child}.png", parent),
        )
        # link is set
        got = conn.execute("SELECT parent_id FROM library WHERE id = ?", (child,)).fetchone()
        assert got["parent_id"] == parent
        # deleting the parent nulls the child link (ON DELETE SET NULL), never
        # orphan-fails — FKs are ON in connect().
        conn.execute("DELETE FROM library WHERE id = ?", (parent,))
        got = conn.execute("SELECT parent_id FROM library WHERE id = ?", (child,)).fetchone()
        assert got["parent_id"] is None


# ── idempotency / ledger ────────────────────────────────────────────────────


def test_migration_0005_is_idempotent(tmp_path):
    db = tmp_path / "m.db"
    init_db(db)  # applies 0005
    # A second boot must apply nothing and must NOT raise duplicate-column.
    init_db(db)
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT version FROM schema_version WHERE version = 5"
        ).fetchall()
    assert len(rows) == 1, "version 5 recorded exactly once"


def test_0005_discovered_by_runner(tmp_path):
    # The runner picks up the .py migration file as version 5.
    found = {m.version: m.name for m in migrations.discover_migrations()}
    assert found.get(5) == "0005_review_regen"
