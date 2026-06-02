"""Tests for the 6 film templates + filmtype→imageType LoRA map (lb-1bh, P6.2).

ACCEPTANCE: all 6 film templates are available and map to the correct
imageType→LoRA via resolve_lora; the mapping is backend-authoritative and only
yields allowlisted LoRAs; covered by a test asserting each filmtype resolves.
"""
from __future__ import annotations

import pytest

from app import film_templates as ft
from app import taxonomy, workflow


# ── all 6 videoTypes have a template, and only those ────────────────────────


def test_every_videotype_has_a_template():
    tax_video_types = set(taxonomy.video_type_slugs())
    assert set(ft.FILM_TEMPLATES.keys()) == tax_video_types
    assert len(ft.FILM_TEMPLATES) == 6


def test_template_image_types_are_valid_taxonomy_image_types():
    for vt, tmpl in ft.FILM_TEMPLATES.items():
        assert taxonomy.is_valid_image_type(tmpl.image_type), (vt, tmpl.image_type)


def test_template_voices_are_valid_narration_voices():
    valid = {v["slug"] for v in taxonomy.NARRATION_VOICES}
    for tmpl in ft.FILM_TEMPLATES.values():
        assert tmpl.narration_voice in valid


def test_every_template_has_at_least_two_shots():
    for vt, tmpl in ft.FILM_TEMPLATES.items():
        assert tmpl.shot_count >= 2, vt
        # shots are 0-indexed and contiguous
        assert [s.index for s in tmpl.shots] == list(range(tmpl.shot_count))


def test_get_template_unknown_raises():
    with pytest.raises(ft.FilmTemplateError):
        ft.get_template("not-a-film")
    with pytest.raises(ft.FilmTemplateError):
        ft.get_template("")


# ── the ACCEPTANCE test: each filmtype resolves an allowlisted LoRA ─────────


def test_each_filmtype_resolves_allowlisted_lora():
    # For a representative subject, every videoType resolves a LoRA that is in
    # the SFW allowlist (baseLora ∪ loraMatrix) — the backend-authoritative,
    # SFW-guarded mapping the bead requires.
    allow = workflow.allowlist()
    for vt in taxonomy.video_type_slugs():
        lora = ft.resolve_film_lora(vt, subject="fysik")
        assert "name" in lora
        assert lora["name"] in allow, (vt, lora["name"])
        # a ComfyUI lora_name file was derived (so the worker can inject it)
        assert lora["file"].endswith((".safetensors", ".ckpt", ".pt", ".bin"))


def test_filmtype_lora_matches_its_image_types_resolution():
    # resolve_film_lora must be exactly resolve_lora(subject, template.image_type)
    # — i.e. it routes through the SAME resolver as the stills path, not a
    # parallel/forked mapping.
    for vt, tmpl in ft.FILM_TEMPLATES.items():
        via_film = ft.resolve_film_lora(vt, "biologi")
        via_stills = workflow.resolve_and_validate_lora("biologi", tmpl.image_type)
        assert via_film == via_stills


def test_resolution_is_allowlisted_across_all_subjects():
    # Stronger: across EVERY subject × every videoType, the resolved LoRA is
    # allowlisted (no subject/film combination can yield an off-list name).
    allow = workflow.allowlist()
    for subject in taxonomy.subject_slugs():
        for vt in taxonomy.video_type_slugs():
            lora = ft.resolve_film_lora(vt, subject)
            assert lora["name"] in allow, (subject, vt, lora["name"])


def test_subject_specific_cell_is_used_when_present():
    # A film whose (subject, imageType) has a matrix cell uses the ämnesspecifik
    # LoRA, not the bas fallback — proving the film map honours the matrix.
    # Find a videoType whose image_type has at least one subject matrix cell.
    found = False
    for vt, tmpl in ft.FILM_TEMPLATES.items():
        for key, cell in taxonomy.LORA_MATRIX.items():
            subj, itype = key.split(":", 1)
            if itype == tmpl.image_type:
                lora = ft.resolve_film_lora(vt, subj)
                assert lora["source"] == "ämnesspecifik"
                assert lora["name"] == cell["name"]
                found = True
                break
        if found:
            break
    assert found, "expected at least one film image_type to have a matrix cell"


def test_subject_without_matrix_cell_falls_back_to_base_lora():
    # A (subject, image_type) with no matrix cell → the image type's base LoRA.
    # Still allowlisted, never None (graceful degrade, BUILD_PLAN open-dec #2).
    # ken-burns → foto; only bild/fysik/historia/religion have a foto cell, so
    # biologi:foto has none → bas source.
    assert "biologi:foto" not in taxonomy.LORA_MATRIX  # guard the premise
    lora = ft.resolve_film_lora("ken-burns", subject="biologi")
    base = taxonomy.BASE_LORA["foto"]
    assert lora["source"] == "bas"
    assert lora["name"] == base["name"]


# ── build_storyboard: the render-ready skeleton ─────────────────────────────


def test_build_storyboard_shape():
    sb = ft.build_storyboard("lektionsfilm", "fysik", "ljud")
    assert sb["video_type"] == "lektionsfilm"
    assert sb["subject"] == "fysik" and sb["area"] == "ljud"
    assert sb["image_type"] == "illustration"
    assert sb["narration_voice"] == "sv-standard"
    assert sb["lora"]["name"] in workflow.allowlist()
    # the keyframes carry the template image type + Ken-Burns motion
    assert len(sb["shots"]) == ft.FILM_TEMPLATES["lektionsfilm"].shot_count
    for shot in sb["shots"]:
        assert shot["image_type"] == "illustration"
        assert "ken_burns" in shot and "pan" in shot["ken_burns"]
        assert shot["duration_s"] > 0


def test_build_storyboard_voice_override():
    sb = ft.build_storyboard("lektionsfilm", "fysik", "ljud",
                             narration_voice="sv-lattlast")
    assert sb["narration_voice"] == "sv-lattlast"


def test_build_storyboard_bad_area_raises():
    with pytest.raises(ft.FilmTemplateError):
        ft.build_storyboard("lektionsfilm", "fysik", "not-an-area")


def test_build_storyboard_bad_subject_raises():
    with pytest.raises(ft.FilmTemplateError):
        ft.build_storyboard("lektionsfilm", "not-a-subject", "ljud")


def test_build_storyboard_bad_voice_raises():
    with pytest.raises(ft.FilmTemplateError):
        ft.build_storyboard("lektionsfilm", "fysik", "ljud", narration_voice="en-US")


def test_build_storyboard_unknown_videotype_raises():
    with pytest.raises(ft.FilmTemplateError):
        ft.build_storyboard("not-a-film", "fysik", "ljud")


# ── templates are backend-authoritative (frozen, not client-supplied) ───────


def test_templates_are_immutable_dataclasses():
    tmpl = ft.FILM_TEMPLATES["lektionsfilm"]
    with pytest.raises(Exception):
        tmpl.image_type = "foto"  # frozen dataclass
    with pytest.raises(Exception):
        tmpl.shots[0].duration_s = 99  # frozen keyframe


def test_as_dict_is_json_serialisable():
    import json
    for tmpl in ft.FILM_TEMPLATES.values():
        json.dumps(tmpl.as_dict())  # must not raise
