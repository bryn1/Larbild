"""Versioned, idempotent migrations runner (lb-ah2).

Reconcile-on-boot: :func:`run_migrations` discovers every ``NNNN_<slug>.sql`` /
``NNNN_<slug>.py`` file in this package, applies the *pending* ones in version
order, and records each in the ``schema_version`` ledger so a re-run (every
gunicorn worker boot, every restart) applies nothing new — a no-op.

Design (P0.2, dementus):

* Migration files are named ``NNNN_<slug>.{sql,py}`` where ``NNNN`` is a
  zero-padded version. ``.sql`` files are executed via ``executescript`` (so
  one file may hold many statements). ``.py`` files must define ``up(conn)``
  (imagegen's ``_migrate_vN`` style) for data migrations SQL can't express.
* The ``schema_version`` table is **runner infrastructure**, created by the
  runner before any migration runs — this resolves the bootstrap chicken-egg
  where migration 0001 itself needs to be recorded. It is a ledger: one row per
  applied migration (``version`` PK, ``name``, ``applied_at``).
* Concurrency: several gunicorn workers may call :func:`run_migrations` at
  once. Migrations are written with idempotent DDL (``CREATE … IF NOT EXISTS``),
  and the ledger ``INSERT`` is guarded — a worker that loses the race to record
  a version catches the ``IntegrityError`` on the ``version`` PRIMARY KEY and
  treats it as already-applied. Each migration runs inside an explicit
  transaction so a partial failure rolls back rather than half-applying.

Idempotency contract for migration authors: ``.sql`` migrations must use
``IF NOT EXISTS`` guards; ``.py`` ``up(conn)`` must be safe to *attempt* twice
(it is only ever committed once, but a crash mid-apply means the next boot
re-runs it before the ledger row exists).
"""
from __future__ import annotations

import re
import sqlite3
from importlib import util as _import_util
from pathlib import Path
from typing import Callable, List, NamedTuple

__all__ = ["run_migrations", "applied_versions", "Migration"]

_MIGRATIONS_DIR = Path(__file__).resolve().parent

# NNNN_<slug>.{sql,py}: a 1+ digit version, an underscore, a slug, the suffix.
_FILE_RE = re.compile(r"^(?P<version>\d+)_(?P<slug>[a-z0-9_]+)\.(?P<ext>sql|py)$")


class Migration(NamedTuple):
    """One discovered migration file."""

    version: int
    name: str  # the filename stem, e.g. "0001_initial"
    path: Path
    ext: str  # "sql" | "py"


def _ensure_ledger(conn: sqlite3.Connection) -> None:
    """Create the ``schema_version`` ledger if absent (runner infrastructure).

    Created before any migration so migration 0001 can itself be recorded.
    Idempotent ``CREATE TABLE IF NOT EXISTS``.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Versions already recorded in the ledger. Empty set if the ledger is new."""
    _ensure_ledger(conn)
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    # Row factory may or may not be set; index 0 works for both tuple and Row.
    return {int(r[0]) for r in rows}


def discover_migrations(directory: Path | None = None) -> List[Migration]:
    """All migration files in *directory*, sorted ascending by version.

    *directory* defaults to this package's directory, resolved at call time
    (not bound at import) so it stays overridable — handy for tests and for any
    future split of migrations across packages. Raises ``ValueError`` if two
    files share a version (an authoring mistake that makes apply order
    ambiguous).
    """
    if directory is None:
        directory = _MIGRATIONS_DIR
    found: List[Migration] = []
    seen: dict[int, str] = {}
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        m = _FILE_RE.match(entry.name)
        if not m:
            continue
        version = int(m.group("version"))
        if version in seen:
            raise ValueError(
                f"duplicate migration version {version}: "
                f"{seen[version]} and {entry.name}"
            )
        seen[version] = entry.name
        found.append(
            Migration(version=version, name=entry.stem, path=entry, ext=m.group("ext"))
        )
    found.sort(key=lambda mig: mig.version)
    return found


def _load_py_up(path: Path) -> Callable[[sqlite3.Connection], None]:
    """Import a ``.py`` migration and return its ``up(conn)`` callable."""
    spec = _import_util.spec_from_file_location(f"_lb_migration_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load migration module {path}")
    module = _import_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    up = getattr(module, "up", None)
    if not callable(up):
        raise AttributeError(f"migration {path.name} must define up(conn)")
    return up


def _apply_one(conn: sqlite3.Connection, migration: Migration) -> bool:
    """Apply a single migration inside its own transaction and record it.

    Returns True if this call applied + recorded the migration, False if a
    concurrent worker had already recorded it (PRIMARY KEY race → no-op).
    """
    try:
        if migration.ext == "sql":
            sql = migration.path.read_text(encoding="utf-8")
            conn.executescript(sql)
        else:  # "py"
            up = _load_py_up(migration.path)
            up(conn)
        # Record in the ledger. The PRIMARY KEY on version makes this the
        # serialization point under concurrency: exactly one worker's INSERT
        # wins; the loser hits IntegrityError below and treats it as applied.
        conn.execute(
            "INSERT INTO schema_version (version, name) VALUES (?, ?)",
            (migration.version, migration.name),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Another worker recorded this version between our applied_versions()
        # read and this INSERT. The DDL is idempotent (IF NOT EXISTS), so the
        # double-apply is harmless; just roll back our duplicate ledger insert.
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise


def run_migrations(conn: sqlite3.Connection) -> List[int]:
    """Apply all pending migrations on *conn*; return versions applied this call.

    Idempotent: discovers migrations, skips those already in the ledger, applies
    the rest in version order. Re-running on an up-to-date DB applies nothing
    and returns ``[]``.
    """
    _ensure_ledger(conn)
    done = applied_versions(conn)
    applied_now: List[int] = []
    for migration in discover_migrations():
        if migration.version in done:
            continue
        if _apply_one(conn, migration):
            applied_now.append(migration.version)
        # Whether we applied it or lost the race, it is now recorded.
        done.add(migration.version)
    return applied_now
