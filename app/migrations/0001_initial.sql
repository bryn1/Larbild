-- lb-ah2 — v1 foundation schema. Idempotent: every statement is CREATE … IF
-- NOT EXISTS so re-running this migration (or booting against a DB the runner
-- has already advanced) is a no-op. The schema_version ledger row for this
-- migration is written by the runner (app/migrations/__init__.py), not here —
-- that resolves the bootstrap chicken-and-egg (the ledger table must exist
-- before any migration, including this one, is recorded).
--
-- Three tables in v1: jobs (render lifecycle), library (generated image/video
-- records, sorted by subject/area/imageType), settings (owner key/value).
--
-- subject / area / image_type / video_type are stored as STRING KEYS that the
-- application validates against the LGR22 taxonomy single source of truth
-- (lb-582, app.taxonomy) on the write path. They are intentionally NOT encoded
-- as DB CHECK enums: the taxonomy is large (17 subjects x 102 areas) and owned
-- by lb-582, so pinning it twice would invite slug drift (risk R7). The DB
-- keeps them NOT NULL + indexed for fast subject/area/imageType browsing.

-- ── jobs ───────────────────────────────────────────────────────────────────
-- One row per render request (image or film). Carries the lifecycle the P1
-- Redis queue + SSE progress (lb-3gi) drive, and the taxonomy keys chosen at
-- submission so a completed job can be mirrored into `library` already sorted.
-- On success the worker writes a `library` row and sets library_id here.
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('bild', 'film')),
    status          TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    -- LGR22 taxonomy keys (validated app-side against app.taxonomy / lb-582).
    subject         TEXT NOT NULL,
    area            TEXT NOT NULL,
    image_type      TEXT,                 -- imageTypes slug (kind='bild')
    video_type      TEXT,                 -- videoTypes slug (kind='film')
    title           TEXT,
    prompt          TEXT,
    -- Resolved render parameters (resolve_lora output, seed, GPU target, …).
    lora            TEXT,                 -- resolved LoRA name (resolve_lora)
    lora_strength   REAL,
    seed            INTEGER,
    backend         TEXT,                 -- e.g. 'vm100 · 3090', 'vm153 · B70'
    params_json     TEXT,                 -- full submitted parameter blob
    -- Progress + outcome (the SSE stream reads these).
    progress_step   INTEGER,
    progress_total  INTEGER,
    error           TEXT,
    library_id      TEXT,                 -- set on success: the library row produced
    queued_at       TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    finished_at     TEXT,
    FOREIGN KEY (library_id) REFERENCES library (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs (status, queued_at);
CREATE INDEX IF NOT EXISTS idx_jobs_subject     ON jobs (subject, area);
CREATE INDEX IF NOT EXISTS idx_jobs_library     ON jobs (library_id);

-- ── library ────────────────────────────────────────────────────────────────
-- The persisted generated image/film records, auto-sorted by subject / area /
-- image_type for the Bibliotek 3-level view (lb-5rc). One row per artefact on
-- disk under STORAGE_DIR. subject/area + (image_type|video_type) are the
-- taxonomy string keys; the GET /api/library listing groups + counts on them.
CREATE TABLE IF NOT EXISTS library (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('bild', 'film')),
    -- LGR22 taxonomy keys (validated app-side against app.taxonomy / lb-582).
    subject         TEXT NOT NULL,
    area            TEXT NOT NULL,
    image_type      TEXT,                 -- imageTypes slug (kind='bild')
    video_type      TEXT,                 -- videoTypes slug (kind='film')
    title           TEXT NOT NULL,
    -- Provenance + render metadata.
    job_id          TEXT,                 -- the jobs row that produced this (soft ref)
    file_path       TEXT NOT NULL,        -- relative to STORAGE_DIR
    thumb_path      TEXT,
    prompt          TEXT,
    lora            TEXT,
    lora_strength   REAL,
    seed            INTEGER,
    backend         TEXT,
    width           INTEGER,
    height          INTEGER,
    duration_s      INTEGER,              -- film only
    narration       TEXT,                 -- narrationVoices slug (film only)
    workflow_json   TEXT,
    deleted_at      TEXT,                 -- soft delete
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Browsing the library is always scoped subject -> area -> type, newest first;
-- this composite serves the 3-level Bibliotek view + per-bucket counts.
CREATE INDEX IF NOT EXISTS idx_library_sort
    ON library (subject, area, image_type, deleted_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_library_video
    ON library (subject, area, video_type, deleted_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_library_kind     ON library (kind, deleted_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_library_job      ON library (job_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_library_file_path ON library (file_path);

-- ── settings ───────────────────────────────────────────────────────────────
-- Owner-scoped key/value store (single-user LAN tool). Flat strings; callers
-- JSON-encode structured values into `value`.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
