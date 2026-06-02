"""Tests for GDPR delete + use-path listing (lb-xlr, P7.4).

Acceptance:
  * deleting a portrait removes its reference files from disk and cascades the DB
    rows;
  * the use path lists ONLY consent-OK references;
  * a deleted portrait is unusable and its files are gone;
  * deletion is audited (and the audit survives the row's deletion).
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
        "DB_PATH": tmp_path / "del.db",
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
    return resp.get_json()


def _faces(app):
    with connect(app.config["DB_PATH"]) as conn:
        return conn.execute("SELECT * FROM faces").fetchall()


def _audit(app, face_id):
    with connect(app.config["DB_PATH"]) as conn:
        return conn.execute(
            "SELECT * FROM face_usage_log WHERE face_id = ? ORDER BY used_at", (face_id,)
        ).fetchall()


def _revoke(app, face_id):
    with connect(app.config["DB_PATH"]) as conn:
        conn.execute(
            "UPDATE faces SET revoked_at = datetime('now') WHERE id = ?", (face_id,)
        )


# ── delete removes the on-disk file + the DB row ─────────────────────────────


def test_delete_cascades_file_and_row(client, app):
    rec = _register(client, age=30)
    fid = rec["id"]
    on_disk = app.config["STORAGE_DIR"] / rec["image_path"]
    assert on_disk.is_file()

    resp = client.delete(f"/api/portraits/{fid}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["deleted"] is True
    assert body["files_removed"]["image"] is True

    # Row gone, file gone.
    assert _faces(app) == []
    assert not on_disk.exists()


# ── after delete: unusable (404 on get + use) ────────────────────────────────


def test_deleted_portrait_is_unusable(client, app):
    fid = _register(client, age=30)["id"]
    client.delete(f"/api/portraits/{fid}")
    assert client.get(f"/api/portraits/{fid}").status_code == 404
    assert client.post(f"/api/portraits/{fid}/use", json={}).status_code == 404


# ── deletion is audited, and the audit survives the row deletion ─────────────


def test_delete_is_audited_and_audit_survives(client, app):
    fid = _register(client, age=30)["id"]
    client.delete(f"/api/portraits/{fid}")
    rows = _audit(app, fid)
    actions = [r["action"] for r in rows]
    # The registration 'use' row AND the 'delete' row both remain — the audit's
    # face_id is a soft ref (no FK cascade), so GDPR delete records, never erases.
    assert "delete" in actions
    delete_row = [r for r in rows if r["action"] == "delete"][-1]
    assert delete_row["consent_ok"] == 0


# ── delete unknown → 404 ─────────────────────────────────────────────────────


def test_delete_unknown_404(client, app):
    resp = client.delete("/api/portraits/nope")
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"


# ── the use path lists ONLY consent-OK references ────────────────────────────


def test_usable_list_hides_non_consented(client, app):
    keep = _register(client, age=25)["id"]
    revoked = _register(client, age=25)["id"]
    _revoke(app, revoked)

    # Full list: both present.
    full = client.get("/api/portraits").get_json()
    ids_full = {p["id"] for p in full["portraits"]}
    assert {keep, revoked} <= ids_full

    # Usable-only: the revoked one is hidden from the use path.
    usable = client.get("/api/portraits?usable=1").get_json()
    ids_usable = {p["id"] for p in usable["portraits"]}
    assert keep in ids_usable
    assert revoked not in ids_usable
    assert all(p["consent_ok"] for p in usable["portraits"])


# ── path-traversal safety: a stored path can't escape STORAGE_DIR ────────────


def test_unlink_refuses_to_escape_storage(app, tmp_path):
    # A malicious/buggy stored path must never let the deleter remove an
    # arbitrary file outside STORAGE_DIR.
    from app import portraits as p

    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete me")
    with app.app_context():
        removed = p._safe_unlink_under_storage("../outside.txt")
    assert removed is False
    assert outside.exists()
