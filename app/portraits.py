"""Porträtt consent machinery — data + logic layer (lb-ly1 / lb-wdu / lb-xlr).

This is the consent *engine* behind the /api/portraits routes. It owns the one
authoritative rule the whole feature turns on and keeps the HTTP layer thin
(mirroring app.jobs vs app.routes.jobs):

    consentOk is ALWAYS recomputed server-side from the stored consent facts.
    It is never read from, nor trusted from, a client. A registered portrait is
    usable only when consent_ok() returns True.

``consent_ok`` rule (single source of truth, used by create + use + list):

    usable  ⇔  not revoked
              AND (adult  OR  (minor AND guardian_consent present))

Because the DB floor ``CHECK (is_minor = 0 OR guardian_consent IS NOT NULL)``
guarantees a stored minor row always has guardian_consent, the minor branch is
belt-and-braces — but we recompute it anyway so the logic is explicit and so a
future column (e.g. expiry) has one place to extend.

Age is derived server-side from a birthdate or an explicit age integer; a
client-asserted ``is_minor`` / ``consentOk`` is ignored. ``is_minor`` is then
``age < ADULT_AGE`` (18).

SFW-only: consent records + an audit trail + reference-image storage. No
compositor / swap engine lives here — that is a later bead, and it must call
:func:`consent_ok` (never a client flag) before it touches a reference.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from flask import current_app

from .db import connect, new_id, now_iso

__all__ = [
    "ADULT_AGE",
    "ConsentError",
    "consent_ok",
    "compute_is_minor",
    "register_portrait",
    "get_portrait",
    "list_portraits",
    "record_usage",
    "log_event",
    "portraits_dir",
]

# The legal adult floor. Under this age a guardian consent is mandatory.
ADULT_AGE = 18


class ConsentError(ValueError):
    """A consent rule was violated at the application layer (maps to 4xx).

    Carries an HTTP ``status`` (default 422 Unprocessable — the request was
    well-formed JSON but failed the consent gate) and a stable ``code`` so the
    route can render a precise body without the engine importing Flask response
    helpers.
    """

    def __init__(self, message: str, *, status: int = 422, code: str = "consent_required"):
        super().__init__(message)
        self.status = status
        self.code = code


# ── storage ──────────────────────────────────────────────────────────────────


def portraits_dir() -> Path:
    """Absolute directory holding portrait reference images, under STORAGE_DIR.

    Isolated subdir so GDPR delete (lb-xlr) can reason about exactly which files
    belong to a portrait. Created on demand.
    """
    base = Path(current_app.config["STORAGE_DIR"]) / "portraits"
    base.mkdir(parents=True, exist_ok=True)
    return base


# ── the authoritative consent computation ────────────────────────────────────


def compute_is_minor(*, birthdate: Optional[str], age: Optional[int],
                     today: Optional[date] = None) -> int:
    """Derive ``is_minor`` (0/1) server-side from a birthdate or an age.

    Exactly one of ``birthdate`` (ISO ``YYYY-MM-DD``) or ``age`` (years) must be
    given; a client-asserted minor/adult flag is never consulted. Raises
    :class:`ConsentError` (400) if neither is usable, so a registration can't
    sneak past the age check by omitting it.
    """
    if age is not None:
        try:
            years = int(age)
        except (TypeError, ValueError):
            raise ConsentError("age must be an integer", status=400, code="bad_age")
        if years < 0 or years > 130:
            raise ConsentError("age out of range", status=400, code="bad_age")
        return 1 if years < ADULT_AGE else 0

    if birthdate:
        try:
            bd = date.fromisoformat(birthdate.strip())
        except (TypeError, ValueError):
            raise ConsentError("birthdate must be ISO YYYY-MM-DD", status=400,
                               code="bad_birthdate")
        ref = today or datetime.now(timezone.utc).date()
        years = ref.year - bd.year - ((ref.month, ref.day) < (bd.month, bd.day))
        return 1 if years < ADULT_AGE else 0

    raise ConsentError(
        "a birthdate or age is required to compute consent",
        status=400, code="age_required",
    )


def consent_ok(row: Any) -> bool:
    """Recompute whether a stored portrait row is usable. The single SoT.

    NEVER trust a client value — call this with the DB row on every read + every
    use. Usable ⇔ not revoked AND (adult OR guardian-consented minor).

    Accepts a sqlite3.Row, a mapping, or any object exposing the columns via
    item access; tolerant so callers don't have to normalise first.
    """
    def _get(key: str):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None

    if _get("revoked_at"):
        return False
    is_minor = int(_get("is_minor") or 0)
    if is_minor == 0:
        return True
    # Minor: needs guardian consent. (The DB floor guarantees this for stored
    # rows; we still check so the rule is explicit and self-contained.)
    return bool(_get("guardian_consent"))


# ── audit ────────────────────────────────────────────────────────────────────


def log_event(conn, face_id: str, *, action: str, consent_ok_value: bool,
              job_id: Optional[str] = None, detail: Optional[str] = None) -> str:
    """Append one row to ``face_usage_log``. Returns the new audit row id.

    ``action`` is one of use|denied|delete (the table CHECK enforces it).
    ``consent_ok_value`` is the server-recomputed boolean at this moment — so an
    audit can later see exactly what the gate decided and why.
    """
    audit_id = new_id()
    conn.execute(
        """
        INSERT INTO face_usage_log (id, face_id, action, consent_ok, job_id, detail, used_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (audit_id, face_id, action, 1 if consent_ok_value else 0, job_id, detail, now_iso()),
    )
    return audit_id


# ── reads ────────────────────────────────────────────────────────────────────


def _row_to_dict(row) -> dict:
    """Project a faces row to a JSON-safe dict with the recomputed consent_ok.

    The reference image path is exposed only as a relative path; the absolute
    on-disk location is never leaked to the client.
    """
    d = {k: row[k] for k in row.keys()}
    # consent_ok is derived, never stored — recompute on the way out.
    d["consent_ok"] = consent_ok(row)
    return d


def get_portrait(face_id: str) -> Optional[dict]:
    """Fetch one portrait as a dict (with recomputed consent_ok), or None."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM faces WHERE id = ?", (face_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_portraits(*, usable_only: bool = False) -> list[dict]:
    """List portraits newest-first, each with a recomputed consent_ok.

    ``usable_only`` filters to references the consent gate currently permits —
    the list the use path (lb-xlr UI) must surface. Filtering happens in Python
    against :func:`consent_ok` (not a stored column) so the rule stays singular.
    """
    with connect() as conn:
        rows = conn.execute("SELECT * FROM faces ORDER BY created_at DESC, id").fetchall()
    out = [_row_to_dict(r) for r in rows]
    if usable_only:
        out = [d for d in out if d["consent_ok"]]
    return out


# ── create (lb-ly1) ──────────────────────────────────────────────────────────


@dataclass
class PortraitInput:
    """A validated, server-normalised portrait registration request."""

    label: Optional[str]
    is_minor: int
    guardian_consent: Optional[str]
    consenter_name: Optional[str]
    consent_date: Optional[str]
    consent_notes: Optional[str]
    image_path: str  # relative to STORAGE_DIR
    thumb_path: Optional[str]


def register_portrait(data: PortraitInput) -> dict:
    """Insert a consented portrait through the P7.1 schema. Server-authoritative.

    The gate (also enforced at the DB CHECK): a minor (``is_minor == 1``) MUST
    carry guardian consent. We raise :class:`ConsentError` (422) *before* the
    INSERT so the API returns a precise reason — and the DB floor is the backstop
    if anything ever bypasses this. On success, writes the faces row and one
    ``face_usage_log`` audit row (action='use', detail='portrait registered')
    carrying the server-recomputed consent_ok, so registration leaves an audit
    footprint per the bead's acceptance ("an audit row is written").
    """
    # Application-layer gate — fail fast with a precise reason. The DB CHECK is
    # the floor that catches anything that ever slips past this.
    if data.is_minor == 1 and not (data.guardian_consent and data.guardian_consent.strip()):
        raise ConsentError(
            "guardian consent is required to register a portrait of a minor",
            status=422, code="guardian_consent_required",
        )

    face_id = new_id()
    with connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO faces
                    (id, label, is_minor, guardian_consent, consenter_name,
                     consent_date, consent_notes, image_path, thumb_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    face_id, data.label, data.is_minor,
                    data.guardian_consent, data.consenter_name, data.consent_date,
                    data.consent_notes, data.image_path, data.thumb_path,
                ),
            )
        except Exception as exc:  # DB floor or other integrity failure
            # Surface the floor as a consent error, not a 500: a minor without
            # guardian consent that somehow reached here is a consent violation.
            raise ConsentError(
                "portrait rejected by the consent floor (minor requires guardian consent)",
                status=422, code="db_consent_floor",
            ) from exc

        row = conn.execute("SELECT * FROM faces WHERE id = ?", (face_id,)).fetchone()
        ok = consent_ok(row)
        # Audit the registration with the server-recomputed consent state.
        log_event(conn, face_id, action="use", consent_ok_value=ok,
                  detail="portrait registered")
    return _row_to_dict(row)


# ── use (lb-wdu) ─────────────────────────────────────────────────────────────


def record_usage(face_id: str, *, job_id: Optional[str] = None) -> dict:
    """Gate + record a compositor USE of a portrait. Server-recomputes consent.

    Returns ``{"face": <dict>, "audit_id": <id>}`` on success. Raises
    :class:`ConsentError` (404 unknown / 403 not consented) otherwise. Every
    outcome — allowed or denied — writes a ``face_usage_log`` row so the audit is
    complete. The caller (a later compositor bead) must invoke this and only
    proceed on success; it must never read a client ``consentOk``.
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM faces WHERE id = ?", (face_id,)).fetchone()
        if row is None:
            raise ConsentError("unknown portrait", status=404, code="not_found")
        ok = consent_ok(row)  # RECOMPUTED from the DB, never a client flag
        if not ok:
            log_event(conn, face_id, action="denied", consent_ok_value=False,
                      job_id=job_id, detail="use blocked: consent not satisfied")
            raise ConsentError(
                "portrait is not usable: consent not satisfied (or revoked)",
                status=403, code="not_consented",
            )
        audit_id = log_event(conn, face_id, action="use", consent_ok_value=True,
                             job_id=job_id, detail="compositor use")
        out = _row_to_dict(row)
    return {"face": out, "audit_id": audit_id}


# ── delete (lb-xlr) ──────────────────────────────────────────────────────────


def _safe_unlink_under_storage(rel_path: Optional[str]) -> bool:
    """Unlink a STORAGE_DIR-relative file, refusing to escape STORAGE_DIR.

    Returns True if a file was removed. Path traversal (``..``) or any path that
    resolves outside STORAGE_DIR is refused (returns False) — a stored row can
    never trick the deleter into removing an arbitrary file.
    """
    if not rel_path:
        return False
    storage = Path(current_app.config["STORAGE_DIR"]).resolve()
    target = (storage / rel_path).resolve()
    try:
        target.relative_to(storage)
    except ValueError:
        return False  # escapes STORAGE_DIR — refuse
    if target.is_file():
        target.unlink()
        return True
    return False


def delete_portrait(face_id: str) -> dict:
    """GDPR delete: remove on-disk reference files + the faces row; audit it.

    Order: (1) read the row (404 if gone), (2) unlink image + thumb off disk,
    (3) delete the faces row, (4) write a ``'delete'`` audit row. The audit row
    survives because ``face_usage_log.face_id`` is a soft ref (no FK cascade) —
    deletion is recorded, not erased. Returns a summary of what was removed.
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM faces WHERE id = ?", (face_id,)).fetchone()
        if row is None:
            raise ConsentError("unknown portrait", status=404, code="not_found")

        removed = {
            "image": _safe_unlink_under_storage(row["image_path"]),
            "thumb": _safe_unlink_under_storage(row["thumb_path"]),
        }
        conn.execute("DELETE FROM faces WHERE id = ?", (face_id,))
        # Audit AFTER the delete; face_id is a soft ref so this row persists.
        log_event(conn, face_id, action="delete", consent_ok_value=False,
                  detail="GDPR delete: reference files + row removed")
    return {"deleted": True, "face_id": face_id, "files_removed": removed}
