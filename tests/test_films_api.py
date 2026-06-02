"""Route tests for the film API (lb-4vw, P6.3).

POST /api/films + GET /api/films/templates + GET /api/films/<id>[/events]. Uses
a temp DB + FakeRedis so enqueue persists without a real Redis/GPU. The SSE
delegates to the shared jobs stream (asserted by content-type + a resumed state).
"""
from __future__ import annotations

import pytest

from app import create_app, jobs
from tests._fakeredis import FakeRedis


@pytest.fixture
def client(tmp_path):
    app = create_app({"TESTING": True, "DB_PATH": tmp_path / "films_api.db",
                      "STORAGE_DIR": tmp_path / "storage"})
    return app.test_client()


@pytest.fixture
def fake_redis():
    r = FakeRedis()
    jobs.set_redis(r)
    yield r
    jobs.set_redis(None)


def _spec(**kw):
    base = {"videoType": "lektionsfilm", "subject": "fysik", "area": "ljud",
            "narration": "Ljud är vågor. De rör sig genom luften."}
    base.update(kw)
    return base


# ── templates listing ───────────────────────────────────────────────────────


def test_templates_lists_six(client):
    r = client.get("/api/films/templates")
    assert r.status_code == 200
    tmpls = r.get_json()["templates"]
    assert len(tmpls) == 6
    slugs = {t["video_type"] for t in tmpls}
    assert "lektionsfilm" in slugs and "ken-burns" in slugs
    # each template carries its shot skeleton + image type + default voice
    for t in tmpls:
        assert t["image_type"]
        assert t["narration_voice"] in ("sv-standard", "sv-lattlast")
        assert t["shot_count"] >= 2


# ── POST /api/films ─────────────────────────────────────────────────────────


def test_submit_film_accepts_and_queues(client, fake_redis):
    r = client.post("/api/films", json=_spec())
    assert r.status_code == 202
    body = r.get_json()
    assert body["status"] == "queued"
    assert body["kind"] == "film"
    assert body["job_id"]
    # it landed on the FILM queue
    from app import film
    assert fake_redis.llen(film.FILM_QUEUE_KEY) == 1


def test_submit_film_with_voice_and_prompts(client, fake_redis):
    r = client.post("/api/films", json=_spec(
        videoType="kort-forklaring", narrationVoice="sv-lattlast",
        shotPrompts=["a", "b", "c"]))
    assert r.status_code == 202


def test_submit_missing_videotype_400(client, fake_redis):
    body = _spec()
    del body["videoType"]
    r = client.post("/api/films", json=body)
    assert r.status_code == 400
    assert "valid_video_types" in r.get_json()


def test_submit_unknown_videotype_400(client, fake_redis):
    r = client.post("/api/films", json=_spec(videoType="nope"))
    assert r.status_code == 400


def test_submit_bad_area_400(client, fake_redis):
    r = client.post("/api/films", json=_spec(area="not-an-area"))
    assert r.status_code == 400
    assert "valid_areas" in r.get_json()


def test_submit_missing_narration_400(client, fake_redis):
    body = _spec()
    del body["narration"]
    r = client.post("/api/films", json=body)
    assert r.status_code == 400


def test_submit_bad_voice_400(client, fake_redis):
    r = client.post("/api/films", json=_spec(narrationVoice="en-US"))
    assert r.status_code == 400
    assert "valid_voices" in r.get_json()


def test_submit_bad_shot_prompts_type_400(client, fake_redis):
    r = client.post("/api/films", json=_spec(shotPrompts="not-a-list"))
    assert r.status_code == 400


def test_submit_wrong_shot_prompt_count_400(client, fake_redis):
    # kort-forklaring has 3 shots; supplying 2 prompts is a 400 (FilmError)
    r = client.post("/api/films", json=_spec(videoType="kort-forklaring",
                                             shotPrompts=["a", "b"]))
    assert r.status_code == 400


def test_submit_ignores_client_lora(client, fake_redis):
    # a client-supplied lora must be ignored (backend-authoritative); still 202
    r = client.post("/api/films", json=_spec(lora="smuggled-nsfw-v9"))
    assert r.status_code == 202


# ── GET /api/films/<id> + SSE ───────────────────────────────────────────────


def test_get_film_state(client, fake_redis):
    job_id = client.post("/api/films", json=_spec()).get_json()["job_id"]
    r = client.get(f"/api/films/{job_id}")
    assert r.status_code == 200
    assert r.get_json()["status"] == "queued"


def test_get_unknown_film_404(client, fake_redis):
    r = client.get("/api/films/does-not-exist")
    assert r.status_code == 404


def test_film_events_is_sse_stream(client, fake_redis, monkeypatch):
    # mark the job terminal so the stream resumes + closes immediately
    job_id = client.post("/api/films", json=_spec()).get_json()["job_id"]
    jobs.write_job_state(job_id, {"job_id": job_id, "status": "done",
                                  "kind": "film", "library_id": "lib-x"})
    import app.routes.jobs as jr
    monkeypatch.setattr(jr, "SSE_IDLE_TIMEOUT_S", 0.5)
    r = client.get(f"/api/films/{job_id}/events")
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    data = r.get_data(as_text=True)
    assert '"status": "done"' in data           # RESUME emitted the current state
    assert "event: end" in data                  # terminal → stream closed


def test_film_events_unknown_404(client, fake_redis):
    r = client.get("/api/films/nope/events")
    assert r.status_code == 404
