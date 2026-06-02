"""Tests for the face-use gate (lb-wdu, P7.3).

Acceptance:
  * a use request for a non-consented (or revoked) reference returns 403;
  * ``consentOk`` is recomputed SERVER-SIDE from the DB on every use — a client
    flag cannot authorise a use;
  * only consented references are usable;
  * every successful use (and every denial) appends a ``face_usage_log`` row.
"""
from __future__ import annotations

import base64

import pytest

from app import create_app
from app.db import connect


_PNG_1x1 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da6360000002000100ffff03000006000557bfabd400"
        "00000049454e44ae426082"
    )
).decode()


@pytest.fixture()
def app(tmp_path):
    return create_app({
        "TESTING": True,
        "DB_PATH": tmp_path / "use.db",
        "STORAGE_DIR": tmp_path / "storage",
    })


@pytest.fixture()
def client(app):
    return app.test_client()


def _register(client, **fields):
    body = {"image_b64": _PNG_1x1}
    body.update(fields)
    resp = client.post("/api/portraits", json=body)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()["id"]


def _audit_rows(app, face_id=None):
    with connect(app.config["DB_PATH"]) as conn:
        if face_id is None:
            return conn.execute("SELECT * FROM face_usage_log ORDER BY used_at").fetchall()
        return conn.execute(
            "SELECT * FROM face_usage_log WHERE face_id = ? ORDER BY used_at", (face_id,)
        ).fetchall()


def _revoke(app, face_id):
    with connect(app.config["DB_PATH"]) as conn:
        conn.execute(
            "UPDATE faces SET revoked_at = datetime('now') WHERE id = ?", (face_id,)
        )


# ── consented adult: use is allowed + audited ────────────────────────────────


def test_use_of_consented_ref_allowed_and_audited(client, app):
    fid = _register(client, age=30)
    resp = client.post(f"/api/portraits/{fid}/use", json={"job_id": "job-1"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["consent_ok"] is True
    # one 'use' row attributed to this job
    uses = [r for r in _audit_rows(app, fid) if r["action"] == "use"]
    assert any(r["job_id"] == "job-1" for r in uses)


# ── unknown ref → 404 ────────────────────────────────────────────────────────


def test_use_of_unknown_ref_404(client, app):
    resp = client.post("/api/portraits/does-not-exist/use", json={})
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"


# ── revoked ref → 403, even if the client asserts consentOk ──────────────────


def test_use_after_revocation_is_403_and_client_flag_ignored(client, app):
    # Register a consented minor (usable), then revoke. A use must 403 even
    # though the client lies with consentOk=true — the server recomputes from
    # the DB, not the request.
    fid = _register(
        client, age=15,
        guardian_consent="Vårdnadshavare: C. Förälder, 2026-06-01",
    )
    _revoke(app, fid)
    resp = client.post(f"/api/portraits/{fid}/use", json={"consentOk": True})
    assert resp.status_code == 403, resp.get_data(as_text=True)
    assert resp.get_json()["code"] == "not_consented"
    # A denial is audited with consent_ok=0.
    denied = [r for r in _audit_rows(app, fid) if r["action"] == "denied"]
    assert denied and denied[-1]["consent_ok"] == 0


# ── recompute is live: revoke flips a previously-usable ref to unusable ───────


def test_consent_recomputed_each_use(client, app):
    fid = _register(client, age=40)
    # First use: allowed.
    assert client.post(f"/api/portraits/{fid}/use", json={}).status_code == 200
    # Revoke, then the very next use is blocked — consent is recomputed, not
    # cached from the first call.
    _revoke(app, fid)
    assert client.post(f"/api/portraits/{fid}/use", json={}).status_code == 403


# ── every successful use writes its own audit row ────────────────────────────


def test_each_use_appends_an_audit_row(client, app):
    fid = _register(client, age=30)
    client.post(f"/api/portraits/{fid}/use", json={"job_id": "a"})
    client.post(f"/api/portraits/{fid}/use", json={"job_id": "b"})
    uses = [r for r in _audit_rows(app, fid) if r["action"] == "use" and r["job_id"] in ("a", "b")]
    assert len(uses) == 2


# ── only consented refs are usable (direct unusable-state proof) ─────────────


def test_only_consented_refs_usable(client, app):
    good = _register(client, age=25)
    bad = _register(client, age=25)
    _revoke(app, bad)
    assert client.post(f"/api/portraits/{good}/use", json={}).status_code == 200
    assert client.post(f"/api/portraits/{bad}/use", json={}).status_code == 403
