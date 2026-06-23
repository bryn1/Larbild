"""lb-zdp — asset-lessons: group existing library assets into a named lesson.

A *lesson* here is a TEACHER-CURATED, ORDERED COLLECTION of already-generated
library assets (images/films) under a human title — "the set of pictures I show
when I teach Ljud in åk 8". It is a SEPARATE concern from the lb-lesson
video-manuscript funnel (``app/lesson.py`` + ``app/routes/lessons.py``), which
RENDERS a new pedagogical *video* from an Opus manuscript and produces a single
``library`` row of kind ``film``. That funnel makes content; this groups content
the teacher already has. They share nothing but the word "lesson", so this uses
distinct table names (``asset_lessons`` / ``asset_lesson_items``) to avoid any
collision or duplicate-and-rewire of the video pipeline.

Two tables:

* ``asset_lessons``       — one row per named lesson:
    - ``id``         UUID primary key.
    - ``title``      the teacher's name for the lesson (required, length-capped
                     app-side).
    - ``subject`` /  optional taxonomy hints (the lesson's "home" area, for
      ``area``       grouping/sorting in the UI). Free of FK to taxonomy — the
                     taxonomy SoT is app-side strings, not a table — and nullable
                     because a lesson may span subjects.
    - ``created_at`` / ``updated_at``  UTC ISO timestamps.

* ``asset_lesson_items``  — the ordered membership (lesson ↔ library, many-many):
    - ``lesson_id``  → ``asset_lessons.id``  ON DELETE CASCADE (delete a lesson,
                     its membership rows go; the library assets are untouched).
    - ``library_id`` → ``library.id``        ON DELETE CASCADE (hard-deleting an
                     asset removes it from any lesson it was in).
    - ``position``   INTEGER ordering within the lesson (0-based; the UI reorders).
    - ``added_at``   UTC ISO.
    - PRIMARY KEY (``lesson_id``, ``library_id``) — an asset appears at most once
      per lesson (adding it again is a no-op upsert, not a duplicate).

Behaviour-additive: it adds tables only; the gallery, the review/regenerate loop,
and the video funnel are untouched. A ``.py`` migration (not ``.sql``) to follow
the established idempotent-on-every-boot contract.

SFW-only: a grouping table over the existing SFW library; no restricted column.
"""
from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS asset_lessons (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            subject     TEXT,
            area        TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS asset_lesson_items (
            lesson_id   TEXT NOT NULL
                            REFERENCES asset_lessons (id) ON DELETE CASCADE,
            library_id  TEXT NOT NULL
                            REFERENCES library (id) ON DELETE CASCADE,
            position    INTEGER NOT NULL DEFAULT 0,
            added_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (lesson_id, library_id)
        )
        """
    )
    # The lesson list is ordered newest-first; the per-lesson asset read scans by
    # lesson_id in position order.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_asset_lessons_recent "
        "ON asset_lessons (updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_asset_lesson_items_order "
        "ON asset_lesson_items (lesson_id, position)"
    )
    # Reverse lookup: which lessons is an asset in (and to CASCADE on asset delete).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_asset_lesson_items_library "
        "ON asset_lesson_items (library_id)"
    )
