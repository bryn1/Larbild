"""Tests for the Porträtt consent schema + DB CHECK floors (lb-fmh, P7.1).

Acceptance (the compliance floor, risk R3):

* migration 0002 creates ``faces`` + ``face_usage_log``;
* the floor ``CHECK (is_minor = 0 OR guardian_consent IS NOT NULL)`` FIRES — a
  minor row without guardian consent cannot be inserted *at the DB level*
  (proving UI/API checks aren't the only line of defence);
* a minor row WITH guardian consent inserts fine; an adult row without a
  guardian inserts fine;
* the audit table records a row per use;
* the migration is idempotent and the schema files stay SFW-clean.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

from app import migrations
from app.db import connect, init_db, new_id, now_iso


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _insert_face(conn, *, is_minor: int, guardian_consent, label="ref"):
    conn.execute(
        """
        INSERT INTO faces (id, label, is_minor, guardian_consent, image_path)
        VALUES (?, ?, ?, ?, ?)
        """,
        (new_id(), label, is_minor, guardian_consent, "portraits/ref.png"),
    )


# ── schema exists ───────────────────────────────────────────────────────────


def test_consent_tables_created(tmp_path):
    db = tmp_path / "consent.db"
    init_db(db)
    with connect(db) as conn:
        assert {"faces", "face_usage_log"} <= _tables(conn)


def test_consent_migration_recorded_in_ledger(tmp_path):
    db = tmp_path / "ledger.db"
    init_db(db)
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT name FROM schema_version ORDER BY version"
        ).fetchall()
    names = [r["name"] for r in rows]
    assert "0002_consent" in names


# ── THE FLOOR: a minor without guardian consent is rejected by the DB ────────


def test_minor_without_guardian_rejected_at_db_level(tmp_path):
    db = tmp_path / "floor.db"
    init_db(db)
    with connect(db) as conn:
        # This is the whole point of the bead: even a direct INSERT that bypasses
        # every API/UI check cannot persist a minor without guardian consent.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_face(conn, is_minor=1, guardian_consent=None)


def test_minor_with_guardian_consent_allowed(tmp_path):
    db = tmp_path / "minor_ok.db"
    init_db(db)
    with connect(db) as conn:
        _insert_face(conn, is_minor=1, guardian_consent="Guardian: A. Förälder, 2026-06-01")
        n = conn.execute("SELECT COUNT(*) AS n FROM faces").fetchone()["n"]
    assert n == 1


def test_adult_without_guardian_allowed(tmp_path):
    db = tmp_path / "adult_ok.db"
    init_db(db)
    with connect(db) as conn:
        _insert_face(conn, is_minor=0, guardian_consent=None)
        n = conn.execute("SELECT COUNT(*) AS n FROM faces").fetchone()["n"]
    assert n == 1


def test_floor_also_fires_on_update_to_minor(tmp_path):
    # The floor is a table CHECK, so it guards UPDATE too: you cannot flip an
    # adult row to is_minor=1 while leaving guardian_consent NULL.
    db = tmp_path / "update_floor.db"
    init_db(db)
    with connect(db) as conn:
        _insert_face(conn, is_minor=0, guardian_consent=None, label="adult")
        fid = conn.execute("SELECT id FROM faces").fetchone()["id"]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE faces SET is_minor = 1 WHERE id = ?", (fid,))


# ── audit table records a row per use ────────────────────────────────────────


def test_audit_table_records_a_use_row(tmp_path):
    db = tmp_path / "audit.db"
    init_db(db)
    with connect(db) as conn:
        _insert_face(conn, is_minor=0, guardian_consent=None)
        fid = conn.execute("SELECT id FROM faces").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO face_usage_log (id, face_id, action, consent_ok, used_at)
            VALUES (?, ?, 'use', 1, ?)
            """,
            (new_id(), fid, now_iso()),
        )
        row = conn.execute(
            "SELECT face_id, action, consent_ok FROM face_usage_log"
        ).fetchone()
    assert row["face_id"] == fid
    assert row["action"] == "use"
    assert row["consent_ok"] == 1


def test_audit_action_check_rejects_unknown_action(tmp_path):
    db = tmp_path / "audit_check.db"
    init_db(db)
    with connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO face_usage_log (id, face_id, action, consent_ok) "
                "VALUES (?, 'x', 'bogus', 1)",
                (new_id(),),
            )


# ── idempotency ──────────────────────────────────────────────────────────────


def test_consent_migration_idempotent(tmp_path):
    db = tmp_path / "idem.db"
    init_db(db)
    with connect(db) as conn:
        before = conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
    init_db(db)  # second boot
    with connect(db) as conn:
        after = conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        applied = migrations.run_migrations(conn)
    assert [r[0] for r in before] == [r[0] for r in after]
    assert applied == []


# ── SFW guard: the consent migration must not smuggle in banned markers ──────


def test_consent_migration_file_is_sfw_clean():
    """The consent module legitimately uses protective field names (is_minor,
    guardian_consent). It must still be free of the repo's NSFW markers — the
    consent system is the SFW *protection* layer, not NSFW content."""
    markers = (
        "nsfw", "deepfake", "face_swap", "faceswap", "wardrobe",
        "lustify", "bigasp", "wai-nsfw", "wai_nsfw", "pony",
        "reactor", "insightface", "cyberrealistic", "porn",
    )
    sql = (
        pathlib.Path(__file__).resolve().parent.parent
        / "app" / "migrations" / "0002_consent.sql"
    ).read_text(encoding="utf-8").lower()
    for marker in markers:
        assert marker not in sql, f"NSFW marker {marker!r} found in consent migration"
