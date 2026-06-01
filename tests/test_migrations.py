"""Tests for the SQLite schema + migrations runner (lb-ah2).

Acceptance: the runner applies all migrations idempotently on boot (re-running
is a no-op); a fresh DB has jobs/library/settings; ``schema_version`` rows track
applied migrations. Also exercises the runner mechanics the design calls out:
ledger bootstrap, ``.py`` migration discovery + ``up(conn)``, and the
concurrent-apply PRIMARY KEY race.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import create_app
from app.db import connect, init_db
from app import migrations


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


# ── fresh DB: the v1 foundation tables exist ───────────────────────────────


def test_fresh_db_has_v1_tables(tmp_path):
    db = tmp_path / "fresh.db"
    init_db(db)
    with connect(db) as conn:
        tables = _tables(conn)
    assert {"jobs", "library", "settings"} <= tables


def test_schema_version_ledger_records_initial_migration(tmp_path):
    db = tmp_path / "ledger.db"
    init_db(db)
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT version, name, applied_at FROM schema_version ORDER BY version"
        ).fetchall()
    assert rows, "schema_version must record applied migrations"
    versions = [r["version"] for r in rows]
    assert 1 in versions
    first = rows[0]
    assert first["name"] == "0001_initial"
    assert first["applied_at"]  # datetime('now') default populated


# ── idempotency: re-running applies nothing ────────────────────────────────


def test_init_db_is_idempotent(tmp_path):
    db = tmp_path / "idem.db"
    init_db(db)
    with connect(db) as conn:
        before = conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()

    # Second boot: must be a pure no-op.
    init_db(db)
    with connect(db) as conn:
        after = conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        applied = migrations.run_migrations(conn)

    assert [r[0] for r in before] == [r[0] for r in after]
    assert applied == [], "re-running migrations must apply nothing"


def test_run_migrations_returns_applied_then_empty(tmp_path):
    db = tmp_path / "applied.db"
    with connect(db) as conn:
        first = migrations.run_migrations(conn)
        second = migrations.run_migrations(conn)
    assert 1 in first  # first call applied the initial migration
    assert second == []  # second call applied nothing


# ── runner mechanics: discovery, ledger bootstrap, .py up(conn), concurrency ─


def test_discover_initial_migration():
    found = migrations.discover_migrations()
    by_version = {m.version: m for m in found}
    assert 1 in by_version
    assert by_version[1].name == "0001_initial"
    assert by_version[1].ext == "sql"
    # discovery is sorted ascending by version
    assert [m.version for m in found] == sorted(m.version for m in found)


def test_ledger_created_before_any_migration(tmp_path):
    # applied_versions() on an empty DB must create the ledger and return empty.
    db = tmp_path / "bootstrap.db"
    with connect(db) as conn:
        versions = migrations.applied_versions(conn)
        assert versions == set()
        assert "schema_version" in _tables(conn)


def test_duplicate_version_rejected(tmp_path):
    # Two files claiming the same version is an authoring error → ValueError.
    d = tmp_path / "migs"
    d.mkdir()
    (d / "0001_a.sql").write_text("CREATE TABLE IF NOT EXISTS a (x);", encoding="utf-8")
    (d / "0001_b.sql").write_text("CREATE TABLE IF NOT EXISTS b (x);", encoding="utf-8")
    with pytest.raises(ValueError):
        migrations.discover_migrations(d)


def test_py_migration_up_is_invoked(tmp_path, monkeypatch):
    # A .py migration must be discovered and its up(conn) executed. Point the
    # runner at a temp dir holding one .py migration.
    d = tmp_path / "migs"
    d.mkdir()
    (d / "0001_pytable.py").write_text(
        "def up(conn):\n"
        "    conn.execute('CREATE TABLE IF NOT EXISTS pytable (id TEXT PRIMARY KEY)')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(migrations, "_MIGRATIONS_DIR", d)
    db = tmp_path / "py.db"
    with connect(db) as conn:
        applied = migrations.run_migrations(conn)
        tables = _tables(conn)
    assert applied == [1]
    assert "pytable" in tables


def test_py_migration_without_up_raises(tmp_path, monkeypatch):
    d = tmp_path / "migs"
    d.mkdir()
    (d / "0001_noup.py").write_text("# no up() here\nX = 1\n", encoding="utf-8")
    monkeypatch.setattr(migrations, "_MIGRATIONS_DIR", d)
    db = tmp_path / "noup.db"
    with connect(db) as conn:
        with pytest.raises(AttributeError):
            migrations.run_migrations(conn)


def test_concurrent_apply_race_is_no_op(tmp_path):
    # Simulate two workers: worker A records version 1, then worker B's INSERT
    # for the same version hits the PRIMARY KEY and is swallowed (returns False).
    db = tmp_path / "race.db"
    with connect(db) as conn_a, connect(db) as conn_b:
        migrations._ensure_ledger(conn_a)
        mig = migrations.discover_migrations()[0]
        assert migrations._apply_one(conn_a, mig) is True
        # conn_b tries to apply/record the same version → IntegrityError → False.
        assert migrations._apply_one(conn_b, mig) is False
        rows = conn_a.execute(
            "SELECT COUNT(*) AS n FROM schema_version WHERE version = 1"
        ).fetchone()
    assert rows["n"] == 1, "ledger must hold exactly one row per version"


# ── connection pragmas (imagegen parity) ───────────────────────────────────


def test_connection_has_wal_and_foreign_keys(tmp_path):
    db = tmp_path / "pragma.db"
    init_db(db)
    with connect(db) as conn:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert fk == 1
    assert journal.lower() == "wal"


# ── factory wiring: create_app boots the DB ────────────────────────────────


def test_create_app_initialises_db(tmp_path):
    db = tmp_path / "factory.db"
    app = create_app({"TESTING": True, "DB_PATH": db})
    assert db.exists(), "create_app must create the DB on boot"
    with connect(db) as conn:
        assert {"jobs", "library", "settings"} <= _tables(conn)


def test_create_app_can_skip_db_init(tmp_path):
    db = tmp_path / "skipped.db"
    create_app({"TESTING": True, "DB_PATH": db, "INIT_DB": False})
    assert not db.exists(), "INIT_DB=False must skip DB creation"
