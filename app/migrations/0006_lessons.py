"""lb-lesson — pedagogical lesson/short VIDEO kinds + the manuscript store.

The Lärbild *Lärbild* feature (the manuscript-driven lesson/overview video
system) extends the P6 film layer with a richer artefact: a per-*delområde*
OVERVIEW video that walks ALL of an area's *begrepp* at one of three school
levels (``enkel`` / ``utvecklad`` / ``välutvecklad``), multimodal — a per-concept
image + a level-scaled *bildtext* (caption) + a *berättarröst* (narration) that
ties the concepts together step by step — plus per-*begrepp* and family-of-begrepp
SHORTS. Each produced video is auto-sorted into the existing ``library`` exactly
like a film, so it inherits the Bibliotek browse + the review/regenerate loop
(lb-review-regen) for free.

This migration adds the two things the lesson layer needs on top of the film
schema:

1. **Two new artefact KINDS — ``'lesson'`` and ``'short'``** — on ``library``
   (and on ``jobs``, so a lesson render job is durable like a film/bild job). The
   v1 schema (lb-ah2) pinned ``kind`` to a ``CHECK (kind IN ('bild','film'))``;
   SQLite cannot ALTER a CHECK constraint, so the only faithful way to widen it
   to ``('bild','film','lesson','short')`` is the documented *table-recreate*
   dance (rename → CREATE new → copy → drop → recreate indexes). To make that
   recreate impossible to drift as later migrations add columns, the new table is
   built by reading the LIVE ``PRAGMA table_info`` and reproducing every column
   verbatim (only the ``kind`` CHECK is rewritten) — so columns added by 0003
   (``batch_id``), 0004 (``missing_since``), 0005 (review_ok/comment/reviewed_at/
   parent_id) are all carried, and a future column is too. Foreign keys, the
   ``status`` CHECK, defaults, and NOT NULLs are preserved from the stored DDL.

2. **A ``lessons`` manuscript table** — the durable record of the structured
   manuscript (the Opus-generated pedagogical script) a lesson/short was
   assembled from: its subject/area (*delområde*), level, video kind, the full
   beat-by-beat manuscript JSON, and a soft link to the produced ``library``
   video. Keeping the manuscript persisted (not just inlined in the job's
   ``params_json``) lets a lesson be re-assembled / regenerated and lets the UI
   show "this video was made from N begrepp at level X".

Idempotency contract (a ``.py`` migration, per the runner): every step checks
state before acting — the table-recreate is skipped once ``kind`` already admits
``'lesson'`` (so a re-run, or a crash between the recreate and the ledger INSERT,
is a no-op), each ADD/CREATE is guarded, and the whole thing runs inside the
runner's single transaction. Re-running on an up-to-date DB does nothing.

SFW-only: this is curriculum video plumbing over the existing SFW library/jobs
tables — two new artefact kinds + a manuscript record; no restricted column,
content enum, model, or reference.
"""
from __future__ import annotations

import sqlite3

# The widened set of artefact kinds. 'bild'/'film' are the v1 kinds (lb-ah2);
# 'lesson'/'short' are the Lärbild pedagogical-video kinds added here.
_KINDS = ("bild", "film", "lesson", "short")
_KIND_CHECK = "CHECK (kind IN (" + ", ".join(f"'{k}'" for k in _KINDS) + "))"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _kind_admits_lesson(conn: sqlite3.Connection, table: str) -> bool:
    """True iff ``table.kind`` already accepts 'lesson' (the recreate is done).

    Probed off the stored ``CREATE TABLE`` text rather than by attempting an
    insert (which would dirty the table / need a rollback). The recreate writes
    the literal ``'lesson'`` into the CHECK, so its presence is the marker.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None or row[0] is None:
        return True  # table absent → nothing to widen (defensive; won't happen)
    return "'lesson'" in row[0]


def _table_ddl(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row and row[0] else ""


def _index_ddls(conn: sqlite3.Connection, table: str) -> list[str]:
    """Every user (non-autoindex) index DDL for ``table``, in catalog order.

    Excludes ``sqlite_autoindex_*`` (PK/UNIQUE-constraint indexes SQLite recreates
    with the table) and any NULL-sql entries. These are replayed verbatim after
    the table is recreated so all of lb-ah2/0003/0004/0005's indexes survive.
    """
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name=? ORDER BY rowid",
        (table,),
    ).fetchall()
    return [r[1] for r in rows if r[1] and not r[0].startswith("sqlite_autoindex_")]


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _widen_kind_check(conn: sqlite3.Connection, table: str) -> None:
    """Recreate ``table`` with the widened ``kind`` CHECK, preserving everything.

    The SQLite-documented safe column-alter procedure (https://sqlite.org/lang_
    altertable.html, "making other kinds of table schema changes"): rename the
    old table aside, CREATE the new one from the OLD DDL with only the ``kind``
    CHECK rewritten, copy every row across by the live column list, drop the old
    table, and replay the user indexes. Done only when the CHECK is still the
    narrow v1 one (guarded by the caller), so it runs exactly once.

    ``foreign_keys`` is toggled OFF for the duration: dropping the renamed table
    and the self-referential ``parent_id`` FK (and ``jobs``→``library``) must not
    trip FK enforcement mid-recreate. It is restored afterwards. (The runner
    holds one connection/transaction; PRAGMA foreign_keys is a no-op inside an
    open transaction on some SQLite builds, so we also guard the copy by column
    list to avoid touching FK-checked rows out of order.)
    """
    old_ddl = _table_ddl(conn, table)
    if not old_ddl:
        return

    # Rewrite ONLY the kind CHECK. The v1 DDL contains exactly this fragment
    # (lb-ah2): `kind  TEXT NOT NULL CHECK (kind IN ('bild', 'film'))`. Replace
    # the narrow CHECK with the widened one; everything else (status CHECK,
    # columns added by later ALTERs, FKs, defaults) is left byte-for-byte.
    narrow = "CHECK (kind IN ('bild', 'film'))"
    if narrow not in old_ddl:
        # Defensive: an unexpected DDL shape. Don't silently produce a table
        # without the widened CHECK — fail loudly so the migration is fixed.
        raise RuntimeError(
            f"0006: cannot widen kind CHECK on {table!r}; expected fragment "
            f"{narrow!r} not found in stored DDL"
        )
    new_ddl = old_ddl.replace(narrow, _KIND_CHECK, 1)
    # The new CREATE must target a temp name, then we swap. Build it by renaming
    # the table token in the DDL header.
    tmp = f"{table}__lb0006_new"
    new_ddl = new_ddl.replace(f"CREATE TABLE {table} ", f"CREATE TABLE {tmp} ", 1)
    if f"CREATE TABLE {tmp} " not in new_ddl:
        # Header form was `CREATE TABLE "table"` or similar — handle quoted name.
        new_ddl = new_ddl.replace(f'CREATE TABLE "{table}" ',
                                  f"CREATE TABLE {tmp} ", 1)
    if f"CREATE TABLE {tmp} " not in new_ddl:
        raise RuntimeError(f"0006: could not retarget CREATE TABLE for {table!r}")

    cols = _column_names(conn, table)
    collist = ", ".join(cols)
    indexes = _index_ddls(conn, table)

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(new_ddl)
        conn.execute(
            f"INSERT INTO {tmp} ({collist}) SELECT {collist} FROM {table}"
        )
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
        for idx_sql in indexes:
            conn.execute(idx_sql)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def up(conn: sqlite3.Connection) -> None:
    # ── 1) widen the `kind` CHECK on library + jobs to admit lesson/short ──
    # library first: jobs.library_id and jobs.parent_id FK it, so recreating
    # library while jobs points at it is why _widen_kind_check toggles FKs off.
    if not _kind_admits_lesson(conn, "library"):
        _widen_kind_check(conn, "library")
    if not _kind_admits_lesson(conn, "jobs"):
        _widen_kind_check(conn, "jobs")

    # ── 1b) library.level: tag a lesson/short video with its school level ──
    # The owner asked for the produced video to be "level-tagged" in the library
    # (so the Bibliotek can show / filter the enkel|utvecklad|valutvecklad
    # version of an area's overview). A nullable TEXT column: NULL for a bild/
    # film (no level) and for a family short that compares concepts rather than
    # scaling one across levels; otherwise the level slug. ALTER ADD COLUMN can't
    # be IF-NOT-EXISTS-guarded in SQLite, so it is gated by PRAGMA table_info
    # (the migration-author idempotency contract — re-running is a no-op). The
    # CHECK accepts only the three slugs or NULL so a stray value can't land.
    if not _has_column(conn, "library", "level"):
        conn.execute(
            "ALTER TABLE library ADD COLUMN level TEXT "
            "CHECK (level IN ('enkel', 'utvecklad', 'valutvecklad'))"
        )
    # Browse the level-versions of one area's lesson newest-first.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_library_level "
        "ON library (subject, area, kind, level, deleted_at, created_at DESC)"
    )

    # ── 2) the lessons manuscript store ───────────────────────────────────
    # One row per manuscript a lesson/short was (or will be) assembled from. The
    # full beat-by-beat script lives in manuscript_json (the documented schema in
    # app.lesson); the columns are the queryable facets (subject/area/level/kind)
    # + the soft link to the produced library video. subject/area are taxonomy
    # string keys (validated app-side, like everywhere — NOT a DB enum, per the
    # lb-ah2 single-SoT decision). `kind` here is 'lesson' | 'short'. `level` is
    # the school level slug ('enkel'|'utvecklad'|'valutvecklad') for the OVERVIEW
    # /short; a family short may leave level NULL (it compares concepts rather
    # than scaling one explanation across levels).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lessons (
            id              TEXT PRIMARY KEY,
            kind            TEXT NOT NULL CHECK (kind IN ('lesson', 'short')),
            subject         TEXT NOT NULL,
            area            TEXT NOT NULL,
            level           TEXT,        -- enkel|utvecklad|valutvecklad (NULL for a family short)
            title           TEXT,
            begrepp_count   INTEGER,     -- how many begrepp/beats the manuscript covers
            duration_target_s INTEGER,   -- the manuscript's target length (advisory)
            manuscript_json TEXT NOT NULL,   -- the full structured manuscript (app.lesson schema)
            narration       TEXT,        -- narrationVoices slug used
            job_id          TEXT,        -- the jobs row that assembled it (soft ref)
            library_id      TEXT,        -- the produced library VIDEO row (soft ref)
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # Browse manuscripts by area + level (the three level-versions of one area)
    # and reverse-lookup the manuscript behind a library video.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lessons_area "
        "ON lessons (subject, area, level, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lessons_library ON lessons (library_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lessons_job ON lessons (job_id)"
    )
