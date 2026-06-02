"""The diffusion render path uses the concept-aligned prompt engine (lb-edu-prompt).

Proves the wiring half of the fix: render_job replaces a diffusion job's thin
prompt with a SPECIFIC, begrepp-grounded educational prompt (from edu_prompt)
before the ComfyUI graph is assembled — keeping the SFW LoRA allowlist + a SFW
negative — while leaving non-diffusion jobs untouched. Also covers the dormant
FLUX-routing flag.

End-to-end with a MOCKED ComfyUI + STUBBED gpu_lock/vram_pause (no live GPU,
no network). The 14B composer is OFF (conftest LARBILD_EDU_LLM_ENABLE=0) so the
render path uses the deterministic fallback; an LLM-on integration is asserted by
patching edu_prompt._http_json.
"""
from __future__ import annotations

import contextlib

import pytest

from app import create_app, jobs
from app import edu_prompt as ep
from app import render_worker as rw
from app.db import connect
from tests._fakeredis import FakeRedis


# ── reuse the render-worker test doubles ────────────────────────────────────


class MockComfyClient:
    def __init__(self):
        self.submitted: list[dict] = []
        self.polls = 0
        self.viewed: list[tuple] = []

    def queue_prompt(self, workflow):
        self.submitted.append(workflow)
        return "mock-prompt-1"

    def history(self, prompt_id):
        return {"outputs": {"9": {"images": [
            {"filename": "larbild_00001_.png", "subfolder": "", "type": "output"}
        ]}}}

    def view(self, filename, subfolder, type_):
        self.viewed.append((filename, subfolder, type_))
        return b"\x89PNG mock"


@contextlib.contextmanager
def _ok_lock(reason="", **kw):
    yield True


@contextlib.contextmanager
def _ok_pause(reason="", **kw):
    yield 1 << 20


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DB_PATH": tmp_path / "edu.db",
                       "STORAGE_DIR": tmp_path / "storage"})


@pytest.fixture
def fake_redis():
    r = FakeRedis()
    jobs.set_redis(r)
    yield r
    jobs.set_redis(None)


def _render(job_id, client):
    return rw.render_job(job_id, comfy_factory=lambda: client,
                         gpu_lock_cm=_ok_lock, vram_pause_cm=_ok_pause, poll_s=0.0)


def _submitted_positive(client) -> str:
    """The positive prompt text the (mock) ComfyUI graph was given."""
    wf = client.submitted[0]
    return wf["pos"]["inputs"]["text"]


def wf_neg(client) -> str:
    """The negative prompt text the (mock) ComfyUI graph was given."""
    return client.submitted[0]["neg"]["inputs"]["text"]


# ── a diffusion job gets the SPECIFIC prompt (not the thin one) ─────────────


def test_diffusion_job_renders_with_concept_specific_prompt(app, fake_redis):
    with app.app_context():
        # the operator submits just the begrepp as the "prompt" (the thin input)
        job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                                  image_type="illustration", prompt="Ljud", seed=7)
        client = MockComfyClient()
        lib_id = _render(job_id, client)

        positive = _submitted_positive(client)
        # NOT the bare input — it was rewritten into a specific educational prompt
        assert positive != "Ljud"
        assert "Ljud" in positive                       # concept still named
        assert "scientific illustration" in positive    # imageType style
        # the förklaring content (from the bundled dataset) was woven in
        low = positive.lower()
        assert "vibration" in low or "våg" in low or "vag" in low
        assert "school textbook" in low                 # SFW framing

        # the SFW negative scaffold was applied to the graph
        assert "inappropriate for children" in wf_neg(client).lower() \
            or "not safe for school" in wf_neg(client).lower()

        # the render still completed + auto-sorted correctly (LoRA path intact)
        with connect() as conn:
            row = conn.execute("SELECT * FROM library WHERE id=?", (lib_id,)).fetchone()
        assert row["subject"] == "fysik" and row["image_type"] == "illustration"
        assert row["lora"] in rw._workflow.allowlist()   # SFW allowlist held


def test_render_path_uses_llm_when_enabled(app, fake_redis):
    # With the composer reachable (patched), the render path uses ITS output as
    # the concept body — proving the :8091 integration is wired into the render.
    def fake_http(url, body, timeout):
        return {"choices": [{"message": {"content":
                "labelled cutaway of a human ear showing cochlea and eardrum"}}]}
    with app.app_context():
        job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                                  image_type="tvarsnitt", prompt="Ljud", seed=1)
        client = MockComfyClient()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ep, "_http_json", fake_http)
            # force the composer on for this render (conftest defaults it off)
            mp.setenv("LARBILD_EDU_LLM_ENABLE", "1")
            _render(job_id, client)
        positive = _submitted_positive(client)
        assert "cochlea" in positive                 # the composer's body
        assert "school textbook" in positive.lower()  # finalized framing


# ── a non-diffusion job keeps its original prompt (passthrough) ─────────────


def test_non_diffusion_job_routes_procedural_not_diffusion(app, fake_redis):
    # diagram is a STRUCTURED type: once the procedural engine (lb-procedural-
    # diagrams) is merged alongside the edu-prompt engine, render_job routes a
    # diagram job to the CPU procedural path FIRST and it never reaches the
    # diffusion/ComfyUI lane — so the edu-prompt rewrite can't touch it and the
    # mock ComfyUI client is never called. (Pre-merge, with no procedural module,
    # this job fell through to diffusion and the test asserted the prompt was
    # left unchanged; the composed design makes that even stronger — diagram
    # bypasses diffusion entirely.)
    with app.app_context():
        thin = "pedagogiskt diagram av en ljudvåg, vektorstil"
        job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                                  image_type="diagram", prompt=thin, seed=3)
        client = MockComfyClient()
        _render(job_id, client)
        # routed to procedural → diffusion graph was never assembled/submitted
        assert client.submitted == []


# ── build_specific_prompt unit behaviour ────────────────────────────────────


def test_build_specific_prompt_marks_source_and_concept(app):
    with app.app_context():
        job = {"id": "j", "subject": "biologi", "area": "celler",
               "image_type": "mikroskop", "prompt": "Cell", "title": None,
               "params_json": None, "seed": 1}
        pos, neg, meta = rw.build_specific_prompt(job)
        assert meta["edu_source"] in ("fallback", "llm")
        assert meta["concept"] == "Cell"
        assert meta["image_type"] == "mikroskop"
        assert neg == ep.SFW_NEGATIVE
        assert "microscope" in pos.lower() or "micrograph" in pos.lower()


def test_concept_prefers_explicit_begrepp_param(app):
    # a funnel can send the concept in params (distinct from a free-form prompt)
    with app.app_context():
        import json as _json
        job = {"id": "j", "subject": "fysik", "area": "ljud",
               "image_type": "foto", "prompt": "make me a nice picture please",
               "title": None, "params_json": _json.dumps({"begrepp": "Ljud"}),
               "seed": 1}
        _, _, meta = rw.build_specific_prompt(job)
        assert meta["concept"] == "Ljud"       # the param won over the prose


# ── the FLUX-routing flag is dormant until a FLUX lane exists ───────────────


def test_flux_route_is_dormant_by_default():
    # flag off (default) → no reroute for any type
    assert rw.flux_route_for("illustration") is None
    assert rw.flux_route_for("mikroskop") is None
    assert rw.flux_route_for("foto") is None


def test_flux_route_stays_safe_when_flag_on_but_no_lane(monkeypatch):
    # even with the flag ON, if no FLUX target is registered the route is None
    # (we never pretend a lane exists) — illustration stays on SDXL.
    monkeypatch.setattr(rw, "FLUX_ROUTE_ENABLE", True)
    # ensure the configured FLUX target key is absent from the registry
    assert rw.FLUX_ROUTE_TARGET not in rw.RENDER_TARGETS
    assert rw.flux_route_for("illustration") is None


def test_flux_route_activates_for_eligible_type_when_lane_registered(monkeypatch):
    # simulate a future FLUX lane: register the target + flip the flag → eligible
    # types route to it, ineligible (foto) stay on SDXL.
    monkeypatch.setattr(rw, "FLUX_ROUTE_ENABLE", True)
    fake_targets = dict(rw.RENDER_TARGETS)
    fake_targets[rw.FLUX_ROUTE_TARGET] = {"label": "flux-lane", "http": "x"}
    monkeypatch.setattr(rw, "RENDER_TARGETS", fake_targets)
    assert rw.flux_route_for("illustration") == rw.FLUX_ROUTE_TARGET
    assert rw.flux_route_for("mikroskop") == rw.FLUX_ROUTE_TARGET
    assert rw.flux_route_for("foto") is None   # foto not FLUX-eligible
