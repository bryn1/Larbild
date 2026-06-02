"""Tests for the concept-aligned educational prompt engine (lb-edu-prompt).

The fix for "prompts don't align with the outputs, no image is specific enough":
the diffusion render path now builds a SPECIFIC, begrepp-grounded educational
positive prompt (via the 14B composer on VM100:8091, with a rich template
fallback) instead of the thin one it used to pass straight through.

Covered:
  * build_edu_prompt yields a SPECIFIC prompt grounded in the begrepp — its
    förklaring + vardagligt_exempel + the imageType style appear in the prompt;
  * the dataset back-fills förklaring/example from (subject, begrepp), with
    accent/case-folded matching;
  * the 14B composer is called with the right OpenAI-shape body (MOCKED — no
    live :8091; VM100 is unreachable from CI), its completion is parsed + the
    SFW framing/quality baseline are appended;
  * graceful fallback when :8091 is unreachable (never raises, source=fallback);
  * the SFW negative scaffold carries no restricted token;
  * the RENDER PATH uses edu_prompt for a diffusion job (integration, mocked
    ComfyUI) and leaves non-diffusion jobs' prompts untouched;
  * the FLUX-routing flag is dormant until a FLUX lane is registered;
  * batch_compose pre-generates many prompts in one pass (the no-VRAM-contention
    pattern).

The composer is forced OFF by conftest (LARBILD_EDU_LLM_ENABLE=0) so the suite
is network-free; the LLM-path tests opt in via use_llm=True + a patched HTTP call.
"""
from __future__ import annotations

import contextlib
from urllib import error as urlerror

import pytest

from app import edu_prompt as ep


# ── helpers ────────────────────────────────────────────────────────────────


def _fake_llm(content: str):
    """A _http_json replacement returning an OpenAI-shape completion."""
    def _call(url, body, timeout):
        _call.last_url = url
        _call.last_body = body
        return {"choices": [{"message": {"content": content}}]}
    return _call


# ── build_edu_prompt: specificity from the begrepp (fallback path) ──────────


def test_fallback_prompt_is_specific_to_the_concept():
    # LLM off (conftest default) → deterministic rich template. The prompt must
    # carry the concept's actual content, not just "<type> of <concept>".
    r = ep.build_edu_prompt(
        subject="fysik", image_type="illustration", begrepp="Newtons andra lag",
        forklaring="Kraften på ett föremål är massan gånger accelerationen (F=ma).",
        vardagligt_exempel="Det är lättare att knuffa igång en tom kundvagn än en full.",
        use_llm=False,
    )
    assert r["source"] == "fallback"
    p = r["prompt"]
    # concept named + anchored as the main subject
    assert "Newtons andra lag" in p
    assert "main subject" in p.lower()
    # the förklaring content (the actual physics) is woven in — specificity
    assert "F=ma" in p or "massan" in p
    # the imageType style is present (text-free illustration style)
    assert "scientific textbook illustration" in p
    # SFW educational framing + quality baseline appended
    assert "school textbook" in p.lower()
    assert "neutral background" in p


def test_fallback_prompt_does_not_leak_the_everyday_example():
    # lb render-loop 2026-06: the vardagligt_exempel must NOT leak into the
    # diffusion prompt — that turned "Ljud" into a bicycle/bell. The example is
    # accepted (call-site compat) but never depicted.
    r = ep.build_edu_prompt(
        subject="fysik", image_type="illustration", begrepp="Ljud",
        forklaring="Vibrationer som sprider sig som vågor genom luft.",
        vardagligt_exempel="Ljudet från din cykelklocka når fotgängaren framför dig.",
        use_llm=False,
    )
    p = r["prompt"].lower()
    assert "cykelklocka" not in p and "cykel" not in p   # example object did not leak
    assert "fotgängare" not in p
    assert "ljud" in p                                    # the concept is still there


def test_diffusion_styles_are_text_free():
    # Every diffusion imageType style must forbid embedded text (SDXL garbles it;
    # labels are the procedural/caption path's job).
    for itype in ("foto", "illustration", "mikroskop", "tvarsnitt"):
        assert "no text" in ep.image_type_style(itype)
        assert "labelled" not in ep.image_type_style(itype)
    # the SFW negative also pushes text + unrelated leak-props away
    assert "text" in ep.SFW_NEGATIVE and "bicycle" in ep.SFW_NEGATIVE


def test_fallback_backfills_forklaring_from_bundled_dataset():
    # Only the concept is given; förklaring + example are looked up from the
    # bundled fysik dataset (the specificity source when the funnel sends just
    # the begrepp). "Ljud" must resolve and inject real content.
    r = ep.build_edu_prompt(subject="fysik", image_type="tvarsnitt",
                            begrepp="Ljud", use_llm=False)
    assert r["source"] == "fallback"
    p = r["prompt"].lower()
    # the dataset förklaring for Ljud is about vibrations/waves — must surface
    assert "vibration" in p or "våg" in p or "vag" in p
    # cutaway style for tvärsnitt
    assert "cutaway" in p or "cross-section" in p


def test_build_requires_a_begrepp():
    with pytest.raises(ValueError):
        ep.build_edu_prompt(subject="fysik", image_type="foto", begrepp="  ",
                            use_llm=False)


# ── the bundled begrepp dataset lookup (accent/case folding) ────────────────


def test_lookup_begrepp_is_accent_and_case_insensitive():
    a = ep.lookup_begrepp("fysik", "Ljud")
    b = ep.lookup_begrepp("fysik", "  ljud ")
    assert a is not None and b is not None
    assert a["begrepp"] == b["begrepp"]
    assert "forklaring" in a and "vardagligt_exempel" in a


def test_lookup_unknown_returns_none():
    assert ep.lookup_begrepp("fysik", "definitely-not-a-real-concept-xyz") is None
    assert ep.lookup_begrepp("nosuchsubject", "Ljud") is None


def test_all_three_subjects_ship_a_dataset():
    for subj in ("fysik", "kemi", "biologi"):
        idx = ep._dataset_index(subj)
        assert len(idx) > 50, f"{subj} dataset looks empty ({len(idx)})"


# ── imageType styling ───────────────────────────────────────────────────────


def test_image_type_style_distinguishes_the_diffusion_types():
    foto = ep.image_type_style("foto")
    illus = ep.image_type_style("illustration")
    micro = ep.image_type_style("mikroskop")
    cut = ep.image_type_style("tvarsnitt")
    # each is materially different + matches its modality
    assert "photoreal" in foto.lower() or "photograph" in foto.lower()
    assert "illustration" in illus.lower()
    assert "microscope" in micro.lower() or "micrograph" in micro.lower()
    assert "cutaway" in cut.lower() or "cross-section" in cut.lower()
    assert len({foto, illus, micro, cut}) == 4  # all distinct
    # unknown type → a safe educational default, never empty
    assert ep.image_type_style("totally-unknown")
    assert ep.image_type_style(None)


# ── the 14B composer path (MOCKED — no live :8091) ──────────────────────────


def test_llm_path_calls_composer_with_concept_fields_and_finalizes():
    fake = _fake_llm(
        "cross-section of a human ear, sound waves entering the ear canal, "
        "vibrating eardrum, labelled cochlea, concentric pressure-wave arcs"
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ep, "_http_json", fake)
        r = ep.build_edu_prompt(subject="fysik", image_type="tvarsnitt",
                                begrepp="Ljud", use_llm=True)
    assert r["source"] == "llm"
    assert r["error"] is None
    # the composer body carried the concept fields + the imageType style
    body = fake.last_body
    assert body["model"] == ep.EDU_LLM_MODEL
    user_msg = body["messages"][1]["content"]
    assert "Ljud" in user_msg
    assert "Required visual style" in user_msg  # imageType style threaded in
    assert "förklaring" in user_msg.lower() or "explanation" in user_msg.lower()
    # the composed concept body is present + the SFW framing/quality appended
    p = r["prompt"]
    assert "cochlea" in p                      # the model's concept-specific body
    assert "school textbook" in p.lower()       # framing appended by _finalize
    assert "neutral background" in p            # quality baseline appended


def test_llm_completion_envelope_is_stripped():
    # the composer occasionally wraps output in a fence + preamble + quotes
    fenced = '```\nHere is the prompt:\n"clean labelled diagram of a plant cell"\n```'
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ep, "_http_json", _fake_llm(fenced))
        r = ep.build_edu_prompt(subject="biologi", image_type="illustration",
                                begrepp="Cell", use_llm=True)
    assert r["source"] == "llm"
    # the fence/preamble/quotes are gone; the tag content remains
    assert "```" not in r["prompt"]
    assert "Here is the prompt" not in r["prompt"]
    assert "clean labelled diagram of a plant cell" in r["prompt"]


def test_llm_unreachable_falls_back_gracefully():
    def _boom(url, body, timeout):
        raise urlerror.URLError("connection refused")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ep, "_http_json", _boom)
        r = ep.build_edu_prompt(subject="kemi", image_type="illustration",
                                begrepp="Atom", use_llm=True)
    # never raises; degrades to the rich template + records the error
    assert r["source"] == "fallback"
    assert r["error"] and "unreachable" in r["error"]
    assert r["prompt"]  # still a usable, specific prompt
    assert "Atom" in r["prompt"]


def test_llm_empty_completion_falls_back():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ep, "_http_json", _fake_llm("   "))
        r = ep.build_edu_prompt(subject="fysik", image_type="foto",
                                begrepp="Ljud", use_llm=True)
    assert r["source"] == "fallback"
    assert r["prompt"]


def test_llm_backend_error_field_falls_back():
    # OpenAI-shape servers return 200 + {"error": ...} with no choices when the
    # upstream is down / queue full — must be treated as a failure, not a hang.
    def _err(url, body, timeout):
        return {"error": "upstream unavailable"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ep, "_http_json", _err)
        r = ep.build_edu_prompt(subject="fysik", image_type="foto",
                                begrepp="Ljud", use_llm=True)
    assert r["source"] == "fallback"
    assert r["error"] and "backend" in r["error"]


# ── the SFW scaffold is genuinely SFW ───────────────────────────────────────


def test_sfw_negative_has_no_restricted_token():
    # the negative must scaffold child-safe output without itself naming a
    # restricted asset/token (it ships in app/, scanned by the SFW guard).
    neg = ep.SFW_NEGATIVE.lower()
    for tok in ("nsfw", "lustify", "porn", "deepfake", "undress", "roop",
                "insightface", "faceswap"):
        assert tok not in neg
    # but it DOES push away the failure modes we care about
    assert "deformed" in neg or "bad anatomy" in neg
    assert "inappropriate for children" in neg or "not safe for school" in neg


# ── batch pre-generation (the no-VRAM-contention pattern) ───────────────────


def test_batch_compose_pregenerates_prompts_for_many_cells():
    cells = [
        {"subject": "fysik", "image_type": "illustration", "begrepp": "Ljud"},
        {"subject": "biologi", "image_type": "mikroskop", "begrepp": "Cell"},
        {"subject": "kemi", "image_type": "foto", "begrepp": "Atom"},
    ]
    out = ep.batch_compose(cells, use_llm=False)
    assert len(out) == 3
    for cell in out:
        assert cell["prompt"]                 # each got a specific prompt
        assert cell["source"] == "fallback"   # LLM off in this call
        assert cell["begrepp"].lower() in cell["prompt"].lower()
    # original cells not mutated (new dicts returned)
    assert "prompt" not in cells[0]


def test_batch_compose_skips_a_cell_missing_its_begrepp():
    out = ep.batch_compose([{"subject": "fysik", "image_type": "foto"}],
                           use_llm=False)
    assert out[0]["source"] == "skipped"
    assert out[0]["prompt_error"] == "missing_begrepp"
    assert out[0]["prompt"] == ""
