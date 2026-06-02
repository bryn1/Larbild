-- lb-fmh (P7.1) — Porträtt consent schema + DB CHECK floors.
--
-- The compliance floor for face usage (risk R3): the database physically cannot
-- persist a portrait of an under-18 subject without a recorded guardian
-- consent. UI / API checks are necessary but NOT sufficient — this CHECK is the
-- belt-and-braces floor that holds even if a buggy or hostile caller reaches
-- the write path. Modelled on imagegen's faces / face_usage_log CHECK-constraint
-- pattern, but rebuilt SFW-clean: no compositor-engine columns and no
-- adult-content enums, only the consent facts + an audit trail.
--
-- Idempotent: every statement is CREATE … IF NOT EXISTS, so re-running this
-- migration (or booting against a DB the runner has already advanced) is a
-- no-op. The schema_version ledger row is written by the runner, not here.
--
-- consentOk is intentionally NOT stored as a column. The application
-- RE-COMPUTES it from the raw consent facts on every read + every use (lb-wdu),
-- so a stale or client-forged flag can never authorise a usage. The columns
-- below hold only the underlying facts; consentOk is always derived.

-- ── faces ───────────────────────────────────────────────────────────────────
-- One row per registered portrait reference (a consented face the educational
-- compositor may later draw on). Stores the consent facts + the on-disk paths
-- of the reference image(s); GDPR delete (lb-xlr) removes both the row and the
-- files. owner_id scopes rows to the single LAN owner (kept for parity + future
-- multi-user; defaults to 'owner').
--
-- THE FLOOR: CHECK (is_minor = 0 OR guardian_consent IS NOT NULL).
-- An under-18 row (is_minor = 1) cannot be inserted or updated unless
-- guardian_consent carries the guardian's recorded consent text. This is the
-- single most important line in the consent system.
CREATE TABLE IF NOT EXISTS faces (
    id               TEXT PRIMARY KEY,
    owner_id         TEXT NOT NULL DEFAULT 'owner',
    label            TEXT,                 -- owner-facing name for the reference
    -- Consent facts (the application recomputes consentOk from these).
    is_minor         INTEGER NOT NULL DEFAULT 0
                         CHECK (is_minor IN (0, 1)),
    -- Guardian consent text/record. REQUIRED for a minor (the floor below).
    -- For an adult it is NULL (their own consent is consenter_name/consent_date).
    guardian_consent TEXT,
    consenter_name   TEXT,                 -- who gave consent (adult self or guardian)
    consent_date     TEXT,                 -- ISO-8601 date the consent was given
    consent_notes    TEXT,
    -- Revocation: a non-NULL revoked_at withdraws consent. The row is kept (so
    -- the audit trail stays intact) but consentOk recomputes to false. GDPR
    -- delete (lb-xlr) is the hard removal; this is the soft "stop using" switch.
    revoked_at       TEXT,
    -- On-disk reference paths (relative to STORAGE_DIR). GDPR delete unlinks
    -- both, then deletes this row.
    image_path       TEXT NOT NULL,
    thumb_path       TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    -- ── THE COMPLIANCE FLOOR ────────────────────────────────────────────────
    -- A minor's portrait physically cannot exist without guardian consent.
    CHECK (is_minor = 0 OR guardian_consent IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_faces_owner  ON faces (owner_id, is_minor, revoked_at);
-- The use path (lb-wdu / lb-xlr) filters to usable references; this index serves
-- "consented, not revoked" scans without a stored consentOk column.
CREATE INDEX IF NOT EXISTS idx_faces_usable ON faces (revoked_at, is_minor);

-- ── face_usage_log ──────────────────────────────────────────────────────────
-- Append-only audit trail: one row per consent-relevant event against a face —
-- a USE (the gate at lb-wdu approved a compositor use), a denied use, or a
-- DELETE (GDPR removal at lb-xlr). It records what was used, when, the action,
-- and the consentOk value the server computed at that moment, so an audit can
-- reconstruct why a use was (dis)allowed.
--
-- NOTE: this log row references face_id WITHOUT a hard cascade so the audit
-- survives the face's GDPR deletion (audit retention per policy). face_id is a
-- plain column, not a FK ON DELETE CASCADE — deleting a face must NOT erase its
-- usage history. The delete handler writes a final 'delete' row here.
CREATE TABLE IF NOT EXISTS face_usage_log (
    id           TEXT PRIMARY KEY,
    face_id      TEXT NOT NULL,            -- soft ref (no FK): audit outlives the face
    action       TEXT NOT NULL DEFAULT 'use'
                     CHECK (action IN ('use', 'denied', 'delete')),
    consent_ok   INTEGER NOT NULL         -- the server-recomputed value at this event
                     CHECK (consent_ok IN (0, 1)),
    job_id       TEXT,                     -- the render/use that consumed it (if any)
    detail       TEXT,                     -- free-text reason (e.g. denial cause)
    used_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_face_usage_face   ON face_usage_log (face_id, used_at);
CREATE INDEX IF NOT EXISTS idx_face_usage_action ON face_usage_log (action, used_at);
