"""Tests for the LGR22 taxonomy single source of truth (lb-582).

Locks the design's counts (acceptance: "a test asserts the counts"), the
verbatim port (slugs stable + referenced by id), the ``resolve_lora`` fallback
order, and the ``GET /api/taxonomy`` exposure. If a future edit drifts from
``design/data.js`` these break loudly — the whole point of a single SoT.
"""
from __future__ import annotations

from app import create_app
from app import taxonomy


# ── counts: the canonical LGR22 shape (acceptance criterion) ───────────────


def test_subject_count():
    assert len(taxonomy.SUBJECTS) == 17


def test_area_count():
    total = sum(len(s["areas"]) for s in taxonomy.SUBJECTS)
    assert total == 102


def test_image_type_count():
    assert len(taxonomy.IMAGE_TYPES) == 8


def test_video_type_count():
    assert len(taxonomy.VIDEO_TYPES) == 6


def test_narration_voice_count():
    assert len(taxonomy.NARRATION_VOICES) == 2


def test_base_lora_count():
    assert len(taxonomy.BASE_LORA) == 8


def test_lora_matrix_count():
    assert len(taxonomy.LORA_MATRIX) == 28


# ── slugs are stable + every reference is by id, not free text ─────────────


def test_subject_slugs_present_and_unique():
    slugs = [s["slug"] for s in taxonomy.SUBJECTS]
    assert len(slugs) == len(set(slugs)) == 17
    # A spot-check of the canonical slugs the rest of the system keys on.
    for expected in ("matematik", "fysik", "kemi", "biologi", "historia", "bild"):
        assert expected in slugs


def test_every_subject_has_name_short_hue_and_areas():
    for s in taxonomy.SUBJECTS:
        assert s["slug"] and s["name"] and s["short"]
        assert isinstance(s["hue"], (int, float))
        assert s["areas"], f"{s['slug']} has no areas"


def test_every_area_has_slug_name_and_central_content():
    for s in taxonomy.SUBJECTS:
        for a in s["areas"]:
            assert a["slug"], f"area in {s['slug']} missing slug"
            assert a["name"], f"area {a.get('slug')} in {s['slug']} missing name"
            # Skolverket central content is the reason areas exist here.
            assert a["central"], f"area {a['slug']} in {s['slug']} missing central"


def test_base_lora_keyed_by_every_image_type():
    image_type_slugs = {t["slug"] for t in taxonomy.IMAGE_TYPES}
    assert set(taxonomy.BASE_LORA.keys()) == image_type_slugs


def test_lora_matrix_keys_reference_valid_subject_and_image_type():
    subject_slugs = {s["slug"] for s in taxonomy.SUBJECTS}
    image_type_slugs = {t["slug"] for t in taxonomy.IMAGE_TYPES}
    for key in taxonomy.LORA_MATRIX:
        subj, _, itype = key.partition(":")
        assert subj in subject_slugs, f"matrix key {key!r} has unknown subject"
        assert itype in image_type_slugs, f"matrix key {key!r} has unknown image type"


# ── resolve_lora: verbatim fallback order ──────────────────────────────────


def test_resolve_lora_matrix_hit():
    # fysik:diagram is a specialised cell in the design.
    out = taxonomy.resolve_lora("fysik", "diagram")
    assert out["name"] == "physics-vector-v3"
    assert out["strength"] == 0.86
    assert out["source"] == "ämnesspecifik"
    assert out["base"] == "SDXL"  # cell.base or "SDXL"


def test_resolve_lora_falls_back_to_base():
    # matematik:foto has no matrix cell → base LoRA for 'foto'.
    out = taxonomy.resolve_lora("matematik", "foto")
    assert out["name"] == taxonomy.BASE_LORA["foto"]["name"]
    assert out["source"] == "bas"
    assert out["note"] == "Bas-LoRA för bildtypen."


def test_resolve_lora_every_image_type_resolves_via_base():
    # Every image type has a base LoRA, so any valid type resolves even for a
    # subject with no specialised cell.
    for itype in (t["slug"] for t in taxonomy.IMAGE_TYPES):
        out = taxonomy.resolve_lora("idrott", itype)
        assert out is not None and out["name"]


def test_resolve_lora_unknown_image_type_is_none():
    assert taxonomy.resolve_lora("fysik", "does-not-exist") is None


# ── validation helpers (back DB key checks + library API) ──────────────────


def test_validation_helpers():
    assert taxonomy.is_valid_subject("fysik")
    assert not taxonomy.is_valid_subject("nope")
    # areas are subject-scoped
    assert taxonomy.is_valid_area("fysik", "ljud")
    assert not taxonomy.is_valid_area("matematik", "ljud")  # ljud is a fysik area
    assert taxonomy.is_valid_image_type("diagram")
    assert not taxonomy.is_valid_image_type("nope")
    assert taxonomy.is_valid_video_type("lektionsfilm")
    assert not taxonomy.is_valid_video_type("nope")


def test_taxonomy_is_immutable():
    import pytest

    with pytest.raises((TypeError, AttributeError)):
        taxonomy.SUBJECTS[0]["name"] = "mutated"  # type: ignore[index]


# ── exposure: GET /api/taxonomy ────────────────────────────────────────────


def test_taxonomy_endpoint_returns_full_structure():
    app = create_app({"TESTING": True})
    client = app.test_client()
    resp = client.get("/api/taxonomy")
    assert resp.status_code == 200
    assert resp.is_json
    body = resp.get_json()
    assert len(body["subjects"]) == 17
    assert len(body["imageTypes"]) == 8
    assert len(body["videoTypes"]) == 6
    assert len(body["baseLora"]) == 8
    assert len(body["loraMatrix"]) == 28
