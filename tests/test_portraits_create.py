"""Tests for the /api/portraits create gate (lb-ly1, P7.2).

Acceptance:
  * POST /api/portraits rejects an under-18 reference lacking guardian consent
    (4xx) and stores nothing;
  * it stores the reference once guardian consent is supplied;
  * ``is_minor`` (consent state) is computed SERVER-SIDE — a client cannot
    register a real minor by asserting adult / a fake consentOk;
  * the stored row satisfies the DB CHECK; an audit row is written.
"""
from __future__ import annotations

import base64

import pytest

from app import create_app
from app.db import connect


# A 1x1 PNG, base64 — a valid tiny reference image for the create path.
_PNG_1x1 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da6360000002000100ffff03000006000557bfabd400"
        "00000049454e44ae426082"
    )
).decode()


@pytest.fixture()
def app(tmp_path):
    application = create_app({
        "TESTING": True,
        "DB_PATH": tmp_path / "portraits.db",
        "STORAGE_DIR": tmp_path / "storage",
    })
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def _faces(app):
    with connect(app.config["DB_PATH"]) as conn:
        return conn.execute("SELECT * FROM faces").fetchall()


def _audit(app):
    with connect(app.config["DB_PATH"]) as conn:
        return conn.execute("SELECT * FROM face_usage_log").fetchall()


# ── adult: registers fine ────────────────────────────────────────────────────


def test_adult_registers_201(client, app):
    resp = client.post("/api/portraits", json={
        "label": "Teacher portrait",
        "age": 34,
        "consenter_name": "Self",
        "image_b64": _PNG_1x1,
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["is_minor"] == 0
    assert body["consent_ok"] is True
    assert len(_faces(app)) == 1


# ── minor WITHOUT guardian: rejected, nothing stored ─────────────────────────


def test_minor_without_guardian_rejected_and_stores_nothing(client, app):
    resp = client.post("/api/portraits", json={
        "label": "Pupil",
        "age": 14,
        "image_b64": _PNG_1x1,
    })
    assert resp.status_code == 422, resp.get_data(as_text=True)
    assert resp.get_json()["code"] == "guardian_consent_required"
    # The whole point: nothing persisted — not the row, not the file.
    assert _faces(app) == []
    storage = app.config["STORAGE_DIR"] / "portraits"
    assert not storage.exists() or not any(storage.iterdir())


# ── minor WITH guardian: registers, row satisfies the DB CHECK ───────────────


def test_minor_with_guardian_registers(client, app):
    resp = client.post("/api/portraits", json={
        "label": "Pupil (consented)",
        "age": 14,
        "guardian_consent": "Vårdnadshavare: B. Förälder, 2026-06-01",
        "consenter_name": "B. Förälder",
        "consent_date": "2026-06-01",
        "image_b64": _PNG_1x1,
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)
    rows = _faces(app)
    assert len(rows) == 1
    row = rows[0]
    assert row["is_minor"] == 1
    assert row["guardian_consent"]  # CHECK satisfied: minor has guardian consent


# ── consent is SERVER-COMPUTED: a client cannot fake adult/consentOk ─────────


def test_client_cannot_register_minor_by_claiming_adult(client, app):
    # Client lies: asserts is_minor=0 + consentOk=true, but the birthdate proves
    # a minor. The server ignores the asserted flags, computes is_minor from the
    # birthdate, and rejects (no guardian consent supplied).
    resp = client.post("/api/portraits", json={
        "label": "Sneaky",
        "birthdate": "2014-01-01",   # ~12 yrs → minor
        "is_minor": 0,               # client lie — ignored
        "consentOk": True,           # client lie — ignored
        "image_b64": _PNG_1x1,
    })
    assert resp.status_code == 422, resp.get_data(as_text=True)
    assert resp.get_json()["code"] == "guardian_consent_required"
    assert _faces(app) == []


def test_birthdate_adult_registers(client, app):
    resp = client.post("/api/portraits", json={
        "birthdate": "1990-05-05",
        "image_b64": _PNG_1x1,
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)
    assert resp.get_json()["is_minor"] == 0


# ── missing age input is rejected (can't skip the age check) ─────────────────


def test_missing_age_and_birthdate_rejected(client, app):
    resp = client.post("/api/portraits", json={"image_b64": _PNG_1x1})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "age_required"
    assert _faces(app) == []


# ── missing image is rejected ────────────────────────────────────────────────


def test_missing_image_rejected(client, app):
    resp = client.post("/api/portraits", json={"age": 30})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "image_required"


# ── audit row is written on registration ─────────────────────────────────────


def test_registration_writes_audit_row(client, app):
    resp = client.post("/api/portraits", json={"age": 40, "image_b64": _PNG_1x1})
    assert resp.status_code == 201
    audit = _audit(app)
    assert len(audit) == 1
    assert audit[0]["action"] == "use"
    assert audit[0]["consent_ok"] == 1


# ── the reference image is actually written to disk ──────────────────────────


def test_reference_image_written_to_disk(client, app):
    resp = client.post("/api/portraits", json={"age": 22, "image_b64": _PNG_1x1})
    assert resp.status_code == 201
    rel = resp.get_json()["image_path"]
    assert rel.startswith("portraits/")
    on_disk = app.config["STORAGE_DIR"] / rel
    assert on_disk.is_file()
    assert on_disk.read_bytes() == base64.b64decode(_PNG_1x1)
