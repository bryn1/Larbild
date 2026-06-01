"""SQLite connection + boot-time initialisation for Lärbild (lb-ah2).

A thin reimplementation of imagegen's ``sibbis-app/db.py`` connection
conventions (WAL, ``foreign_keys ON``, ``Row`` factory, a ``busy_timeout`` so
concurrent gunicorn workers wait on the writer lock instead of erroring) — but
SFW-only and schema-free here: the schema lives in versioned migration files
under :mod:`app.migrations`, applied by the runner.

Two entry points:

* :func:`connect` — a context manager yielding a configured connection. Pass an
  explicit ``db_path``, or omit it to use the path stashed by :func:`init_db`.
* :func:`init_db` — called once from ``create_app`` on boot. mkdirs the DB's
  parent directory and runs all pending migrations (reconcile-on-boot). Reads
  ``DB_PATH`` from Flask config.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Union

from .migrations import run_migrations

__all__ = ["connect", "init_db", "new_id", "now_iso"]

# Process-wide default set by init_db(), so helper modules can call connect()
# without threading the path through every function. init_db() must run first
# (it does — create_app calls it on boot).
_DB_PATH: Optional[Path] = None

# Wait up to 5s for the writer lock before raising "database is locked" — gives
# parallel gunicorn workers room to serialize writes (matches imagegen).
_BUSY_TIMEOUT_MS = 5000


def _resolve_path(db_path: Optional[Union[str, Path]]) -> Path:
    if db_path is not None:
        return Path(db_path)
    if _DB_PATH is not None:
        return _DB_PATH
    raise RuntimeError(
        "db path not configured: call init_db(db_path) first, or pass db_path "
        "to connect()"
    )


@contextmanager
def connect(db_path: Optional[Union[str, Path]] = None) -> Iterator[sqlite3.Connection]:
    """Yield a configured SQLite connection (autocommit; WAL; FKs on; Row rows).

    ``isolation_level=None`` puts the connection in autocommit mode so callers
    manage transactions explicitly when they need atomicity. The connection is
    always closed on exit.
    """
    path = _resolve_path(db_path)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Union[str, Path]) -> Path:
    """Create the DB (and parents) and run all pending migrations. Idempotent.

    Called once from ``create_app`` on boot. Safe to call repeatedly and from
    multiple workers concurrently — the migrations runner records each migration
    once and a second call is a no-op. Returns the resolved DB path.
    """
    global _DB_PATH
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _DB_PATH = path
    with connect(path) as conn:
        run_migrations(conn)
    return path


def new_id() -> str:
    """A fresh UUID4 string for a primary key."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """Current UTC time as a second-precision ISO-8601 string.

    Servers run UTC (timezone policy); render Europe/Stockholm in the UI.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
