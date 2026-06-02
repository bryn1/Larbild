"""Tests for migration 0006 — lesson/short kinds + library.level + lessons table.

Acceptance: a fresh DB widens the ``kind`` CHECK on library + jobs to admit
'lesson'/'short', adds the level-tag column to library, and creates the lessons
manuscript store — all idempotently (re-running the runner is a no-op), preserving
the columns earlier migrations added (batch_id / missing_since / review_ok /
parent_id), and a row with kind='lesson' + level='enkel' inserts cleanly while a
junk kind/level is rejected by the CHECK.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.db import connect, init_db, new_id


def _cols(conn, table) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ddl(conn, table) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]


def test_kind_check_widened_on_library_and_jobs(tmp_path):
    db = tmp_path / "k.db"
    init_db(db)
    with connect(db) as conn:
        for t in ("library", "jobs"):
            ddl = _ddl(conn, t)
            assert "'lesson'" in ddl, f"{t}.kind must admit 'lesson'"
            assert "'short'" in ddl, f"{t}.kind must admit 'short'"
            # the original bild/film are still admitted
            assert "'bild'" in ddl and "'film'" in ddl


def test_library_has_level_column(tmp_path):
    db = tmp_path / "lvl.db"
    init_db(db)
    with connect(db) as conn:
        assert "level" in _cols(conn, "library")


def test_lessons_table_created_with_expected_columns(tmp_path):
    db = tmp_path / "less.db"
    init_db(db)
    with connect(db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "lessons" in tables
        cols = _cols(conn, "lessons")
    for c in ("id", "kind", "subject", "area", "level", "title", "begrepp_count",
              "duration_target_s", "manuscript_json", "job_id", "library_id",
              "created_at"):
        assert c in cols, f"lessons.{c} missing"


def test_earlier_migration_columns_survive_the_recreate(tmp_path):
    # the kind-widen recreates library + jobs by reading PRAGMA table_info, so the
    # columns 0003/0004/0005 added MUST be carried verbatim.
    db = tmp_path / "carry.db"
    init_db(db)
    with connect(db) as conn:
        lib = _cols(conn, "library")
        jb = _cols(conn, "jobs")
    # 0004 (reconcile flag), 0005 (review/regen): library
    assert {"missing_since", "review_ok", "review_comment", "reviewed_at",
            "parent_id"} <= lib
    # 0003 (batch), 0005 (parent_id): jobs
    assert {"batch_id", "parent_id"} <= jb


def test_lesson_row_inserts_and_bad_kind_rejected(tmp_path):
    db = tmp_path / "ins.db"
    init_db(db)
    with connect(db) as conn:
        # a level-tagged lesson video row inserts cleanly
        conn.execute(
            "INSERT INTO library (id, kind, subject, area, level, title, file_path) "
            "VALUES (?, 'lesson', 'fysik', 'ljud', 'enkel', 'Ljud — översikt', ?)",
            (new_id(), "output/fysik/ljud/lesson/x.mp4"),
        )
        # a short with level NULL inserts cleanly too
        conn.execute(
            "INSERT INTO library (id, kind, subject, area, level, title, file_path) "
            "VALUES (?, 'short', 'fysik', 'ljus', NULL, 'Speglar', ?)",
            (new_id(), "output/fysik/ljus/short/y.mp4"),
        )
        # a junk kind is rejected by the widened CHECK
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO library (id, kind, subject, area, title, file_path) "
                "VALUES (?, 'documentary', 'fysik', 'ljud', 't', ?)",
                (new_id(), "output/z.mp4"),
            )
        # a junk level is rejected by the level CHECK
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO library (id, kind, subject, area, level, title, file_path) "
                "VALUES (?, 'lesson', 'fysik', 'ljud', 'expert', 't', ?)",
                (new_id(), "output/zz.mp4"),
            )


def test_lessons_manuscript_row_roundtrips(tmp_path):
    db = tmp_path / "man.db"
    init_db(db)
    rid = new_id()
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO lessons (id, kind, subject, area, level, begrepp_count, "
            "manuscript_json) VALUES (?, 'lesson', 'fysik', 'ljud', 'enkel', 3, ?)",
            (rid, '{"beats": []}'),
        )
        row = conn.execute("SELECT * FROM lessons WHERE id=?", (rid,)).fetchone()
    assert row["kind"] == "lesson"
    assert row["begrepp_count"] == 3
    assert row["created_at"]  # default populated


def test_migration_idempotent_rerun_is_noop(tmp_path):
    from app import migrations
    db = tmp_path / "idem.db"
    init_db(db)
    with connect(db) as conn:
        applied = migrations.run_migrations(conn)  # second run
    assert applied == []  # nothing new applied
    # 0006 is recorded exactly once
    with connect(db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 6"
        ).fetchone()[0]
    assert n == 1
