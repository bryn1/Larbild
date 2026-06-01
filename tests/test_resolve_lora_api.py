"""Tests for the resolveLora endpoint (lb-9tz).

Acceptance: resolveLora(subject, imageType) returns the loraMatrix cell when
present, else the baseLora fallback; the resolved LoRA is always within the
allowlist; tests cover a matrix hit, a fallback, and an invalid input
(rejected); frontend-supplied LoRA names are ignored/overridden.

The endpoint is a thin wrapper over app.taxonomy.resolve_lora (whose resolution
order is already unit-tested in test_taxonomy.py); these tests pin the HTTP
contract + the three guarantees the wrapper adds: validation, allowlist
enforcement, and "the backend decides, not the client".
"""
from __future__ import annotations

from app import create_app
from app import taxonomy


def _client():
    return create_app({"TESTING": True, "INIT_DB": False}).test_client()


def _allowlist() -> set[str]:
    names = {b["name"] for b in taxonomy.BASE_LORA.values()}
    names |= {c["name"] for c in taxonomy.LORA_MATRIX.values()}
    return names


# ── matrix hit ─────────────────────────────────────────────────────────────


def test_matrix_hit_returns_specialised_cell():
    resp = _client().post("/api/resolve-lora", json={"subject": "fysik", "imageType": "diagram"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["authoritative"] is True
    lora = body["lora"]
    assert lora["name"] == "physics-vector-v3"   # the fysik:diagram cell
    assert lora["strength"] == 0.86
    assert lora["source"] == "ämnesspecifik"
    assert body["subject"] == "fysik"
    assert body["imageType"] == "diagram"


# ── fallback to base ───────────────────────────────────────────────────────


def test_fallback_to_base_when_no_cell():
    # matematik:foto has no matrix cell → base LoRA for 'foto'.
    resp = _client().post("/api/resolve-lora", json={"subject": "matematik", "imageType": "foto"})
    assert resp.status_code == 200
    lora = resp.get_json()["lora"]
    assert lora["name"] == taxonomy.BASE_LORA["foto"]["name"]
    assert lora["source"] == "bas"
    assert lora["note"] == "Bas-LoRA för bildtypen."


def test_matches_taxonomy_resolve_lora_for_every_combo():
    # The endpoint must not diverge from the SoT function it wraps.
    client = _client()
    for s in taxonomy.subject_slugs():
        for it in taxonomy.image_type_slugs():
            resp = client.post("/api/resolve-lora", json={"subject": s, "imageType": it})
            assert resp.status_code == 200, f"{s}:{it} -> {resp.status_code}"
            assert resp.get_json()["lora"] == taxonomy.resolve_lora(s, it)


# ── allowlist: resolved LoRA is always declared by the taxonomy ────────────


def test_resolved_lora_always_in_allowlist():
    client = _client()
    allow = _allowlist()
    for s in taxonomy.subject_slugs():
        for it in taxonomy.image_type_slugs():
            body = client.post("/api/resolve-lora", json={"subject": s, "imageType": it}).get_json()
            assert body["lora"]["name"] in allow, f"{s}:{it} resolved off-allowlist"


# ── invalid input is rejected ──────────────────────────────────────────────


def test_unknown_image_type_rejected():
    resp = _client().post("/api/resolve-lora", json={"subject": "fysik", "imageType": "does-not-exist"})
    assert resp.status_code == 400
    assert "valid_image_types" in resp.get_json()


def test_unknown_subject_rejected():
    # A bad subject is rejected rather than silently falling back to base — a
    # typo must not hide behind graceful degrade.
    resp = _client().post("/api/resolve-lora", json={"subject": "nope", "imageType": "foto"})
    assert resp.status_code == 400
    assert "valid_subjects" in resp.get_json()


def test_missing_fields_rejected():
    resp = _client().post("/api/resolve-lora", json={"subject": "fysik"})
    assert resp.status_code == 400
    body = resp.get_json()
    assert "valid_subjects" in body and "valid_image_types" in body


# ── frontend-supplied LoRA is ignored / overridden ─────────────────────────


def test_client_supplied_lora_is_ignored():
    # The client tries to force an off-allowlist LoRA; the backend ignores it and
    # returns its own authoritative choice.
    resp = _client().post(
        "/api/resolve-lora",
        json={
            "subject": "fysik",
            "imageType": "diagram",
            "lora": "evil-injected-model",
            "loraName": "evil-injected-model",
        },
    )
    assert resp.status_code == 200
    lora = resp.get_json()["lora"]
    assert lora["name"] == "physics-vector-v3"           # backend's choice
    assert lora["name"] != "evil-injected-model"
    assert lora["name"] in _allowlist()


# ── GET form (preview/debug) ───────────────────────────────────────────────


def test_get_form_query_string():
    resp = _client().get("/api/resolve-lora?subject=biologi&imageType=illustration")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["subject"] == "biologi"
    assert body["lora"]["name"] in _allowlist()


def test_get_snake_case_alias_accepted():
    resp = _client().get("/api/resolve-lora?subject=kemi&image_type=foto")
    assert resp.status_code == 200
    assert resp.get_json()["imageType"] == "foto"
