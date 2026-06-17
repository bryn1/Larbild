"""lb-public-flags — visitor "this image is wrong/broken" reports.

The internet-facing read-only gallery (bilder.sibbamala.com) publishes
un-reviewed images for three crowdsource subjects (fysik/matematik/teknik) so
visitors can spot broken or wrong ones. This migration adds the ONE table those
reports land in. It is a SEPARATE signal from the owner's ``library.review_ok``
verdict: a flag is "a visitor thinks something is off", which the owner then
reviews on the private :6868 instance and acts on (fix / approve / reject the
image). Reusing ``review_comment`` would conflate the owner's note with public
input and can't hold many reports per image — so a dedicated table is the
correct one-impl, not a duplicate-and-rewire.

Table ``flags``:

* ``id``            — UUID primary key.
* ``library_id``    — the flagged ``library`` row (soft FK, ON DELETE CASCADE:
  if the image row is hard-deleted the flag goes with it; a soft-delete leaves
  the flag, which is fine — the owner still sees what was reported).
* ``reason``        — the visitor's optional short note. Sanitised + length-
  capped by the endpoint BEFORE insert (plain text only, no markup); NULL when
  omitted.
* ``status``        — owner triage state: ``open`` (new, default) | ``resolved``
  (acted on) | ``dismissed`` (not a real problem). A CHECK pins the set.
* ``client_ip_hash``— a salted SHA-256 of the reporter's IP, NEVER the raw IP
  (privacy). Used only to rate-limit abuse and to collapse duplicate spam from
  one source; not shown in any UI.
* ``created_at``    — when the report arrived (UTC ISO).
* ``resolved_at``   — when the owner moved it out of ``open`` (UTC ISO; NULL
  while open).

A Python migration (not ``.sql``) to follow the established idempotency contract
(``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS``), safe to
attempt on every worker boot.

SFW-only: a visitor-feedback table over the existing SFW library; no restricted
column or content.
"""
from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flags (
            id              TEXT PRIMARY KEY,
            library_id      TEXT NOT NULL
                                REFERENCES library (id) ON DELETE CASCADE,
            reason          TEXT,
            status          TEXT NOT NULL DEFAULT 'open'
                                CHECK (status IN ('open', 'resolved', 'dismissed')),
            client_ip_hash  TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at     TEXT
        )
        """
    )
    # The LAN review view lists OPEN flags newest-first and counts per image, so
    # status + created_at and the library_id grouping are the hot reads.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flags_status "
        "ON flags (status, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flags_library ON flags (library_id)"
    )
    # Rate-limit lookups scan recent rows by source within a time window.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flags_ip_time "
        "ON flags (client_ip_hash, created_at DESC)"
    )
