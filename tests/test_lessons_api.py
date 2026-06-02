"""Route tests for the lesson/short manuscript API (lb-lesson).

POST /api/lessons + GET /api/lessons/schema + POST /api/lessons/preview +
GET /api/lessons/<id>[/events]. Uses a temp DB + FakeRedis so enqueue persists
without a real Redis/GPU. The SSE delegates to the shared jobs stream (asserted
by content-type + a resumed state). No render runs (submission only).
"""
from __future__ import annotations

import pytest

from app import create_app, jobs
from tests._fakeredis import FakeRedis


@pytest.fixture
def client(tmp_path):
    app = create_app({"TESTING": True, "DB_PATH": tmp_path / "lessons_api.db",
                      "STORAGE_DIR": tmp_path / "storage"})
    return app.test_client()


@pytest.fixture
def fake_redis():
    r = FakeRedis()
    jobs.set_redis(r)
    yield r
    jobs.set_redis(None)


def _beat(begrepp, **kw):
    base = {
        "begrepp": begrepp,
        "caption": {"short": f"{begrepp} kort", "longer": f"{begrepp} längre",
                    "long": f"{begrepp} lång"},
        "narration": f"Nu tittar vi på {begrepp}.",
    }
    base.update(kw)
    return base


def _manuscript(**kw):
    base = {
        "subject": "fysik",
        "area": "ljud",
        "level": "enkel",
        "kind": "lesson",
        "beats": [_beat("Ljudvåg"), _beat("Frekvens")],
    }
    base.update(kw)
    return base


# ── schema ───────────────────────────────────────────────────────────────────


def test_schema_lists_levels_voices_motions(client):
    r = client.get("/api/lessons/schema")
    assert r.status_code == 200
    body = r.get_json()
    assert body["levels"] == ["enkel", "utvecklad", "valutvecklad"]
    assert "sv-standard" in body["narration_voices"]
    assert "still" in body["motions"] and "short_clip" in body["motions"]
    assert body["kinds"] == ["lesson", "short"]
    # the machine-readable manuscript schema is present + requires subject/area/beats
    sch = body["schema"]
    assert set(sch["required"]) == {"subject", "area", "beats"}
    assert "caption" in sch["properties"]["beats"]["items"]["properties"]


# ── POST /api/lessons ────────────────────────────────────────────────────────


def test_submit_lesson_accepts_and_queues(client, fake_redis):
    r = client.post("/api/lessons", json=_manuscript())
    assert r.status_code == 202
    body = r.get_json()
    assert body["status"] == "queued"
    assert body["kind"] == "lesson"
    assert body["job_id"]
    from app import assembly
    assert fake_redis.llen(assembly.LESSON_QUEUE_KEY) == 1


def test_submit_all_levels_flag(client, fake_redis):
    r = client.post("/api/lessons", json=_manuscript(allLevels=True))
    assert r.status_code == 202
    assert r.get_json()["all_levels"] is True


def test_submit_short_kind(client, fake_redis):
    r = client.post("/api/lessons", json={
        "subject": "fysik", "area": "ljus", "kind": "short", "level": "enkel",
        "beats": [_beat("Reflektion")]})
    assert r.status_code == 202
    assert r.get_json()["kind"] == "short"


def test_submit_family_short_level_null(client, fake_redis):
    r = client.post("/api/lessons", json={
        "subject": "fysik", "area": "ljus", "kind": "short", "level": None,
        "title": "Konvex vs konkav",
        "beats": [_beat("Konvex spegel"), _beat("Konkav spegel")]})
    assert r.status_code == 202


def test_submit_missing_subject_area_400(client, fake_redis):
    r = client.post("/api/lessons", json={"beats": [_beat("X")]})
    assert r.status_code == 400
    assert "valid_subjects" in r.get_json()


def test_submit_unknown_subject_400(client, fake_redis):
    r = client.post("/api/lessons", json=_manuscript(subject="alkemi"))
    assert r.status_code == 400
    assert "valid_subjects" in r.get_json()


def test_submit_unknown_area_400(client, fake_redis):
    r = client.post("/api/lessons", json=_manuscript(area="not-an-area"))
    assert r.status_code == 400
    assert "valid_areas" in r.get_json()


def test_submit_unknown_level_400(client, fake_redis):
    r = client.post("/api/lessons", json=_manuscript(level="expert"))
    assert r.status_code == 400
    assert "valid_levels" in r.get_json()


def test_submit_aa_level_spelling_ok(client, fake_redis):
    r = client.post("/api/lessons", json=_manuscript(level="välutvecklad"))
    assert r.status_code == 202


def test_submit_bad_voice_400(client, fake_redis):
    r = client.post("/api/lessons", json=_manuscript(narrationVoice="en-US"))
    assert r.status_code == 400
    assert "valid_voices" in r.get_json()


def test_submit_no_beats_400(client, fake_redis):
    r = client.post("/api/lessons", json=_manuscript(beats=[]))
    assert r.status_code == 400


def test_submit_bad_kind_400(client, fake_redis):
    r = client.post("/api/lessons", json=_manuscript(kind="documentary"))
    assert r.status_code == 400


def test_submit_all_levels_on_short_400(client, fake_redis):
    r = client.post("/api/lessons", json={
        "subject": "fysik", "area": "ljus", "kind": "short", "level": "enkel",
        "allLevels": True, "beats": [_beat("Reflektion")]})
    assert r.status_code == 400


def test_submit_ignores_client_lora(client, fake_redis):
    # a smuggled lora field must be ignored (backend-authoritative) — still 202
    r = client.post("/api/lessons", json=_manuscript(lora="smuggled-nsfw-v9"))
    assert r.status_code == 202


def test_submit_empty_body_400(client, fake_redis):
    r = client.post("/api/lessons", json={})
    assert r.status_code == 400


# ── POST /api/lessons/preview (dry-run) ──────────────────────────────────────


def test_preview_returns_facets_and_estimate(client, fake_redis):
    r = client.post("/api/lessons/preview", json=_manuscript())
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["kind"] == "lesson"
    assert body["begrepp_count"] == 2
    assert body["estimated_duration_s"] > 0
    assert len(body["beats"]) == 2
    # each beat preview carries the level-resolved caption + spoken line
    assert body["beats"][0]["caption"] == "Ljudvåg kort"  # enkel → short rung
    assert "Ljudvåg" in body["beats"][0]["narration"]


def test_preview_validates_like_submit(client, fake_redis):
    r = client.post("/api/lessons/preview", json=_manuscript(level="expert"))
    assert r.status_code == 400


def test_preview_does_not_queue(client, fake_redis):
    from app import assembly
    client.post("/api/lessons/preview", json=_manuscript())
    assert fake_redis.llen(assembly.LESSON_QUEUE_KEY) == 0


# ── GET /api/lessons/<id> + SSE ──────────────────────────────────────────────


def test_get_lesson_state(client, fake_redis):
    job_id = client.post("/api/lessons", json=_manuscript()).get_json()["job_id"]
    r = client.get(f"/api/lessons/{job_id}")
    assert r.status_code == 200
    assert r.get_json()["status"] == "queued"


def test_get_unknown_lesson_404(client, fake_redis):
    r = client.get("/api/lessons/does-not-exist")
    assert r.status_code == 404


def test_lesson_events_is_sse_stream(client, fake_redis, monkeypatch):
    job_id = client.post("/api/lessons", json=_manuscript()).get_json()["job_id"]
    jobs.write_job_state(job_id, {"job_id": job_id, "status": "done",
                                  "kind": "lesson", "library_id": "lib-x"})
    import app.routes.jobs as jr
    monkeypatch.setattr(jr, "SSE_IDLE_TIMEOUT_S", 0.5)
    r = client.get(f"/api/lessons/{job_id}/events")
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    data = r.get_data(as_text=True)
    assert '"status": "done"' in data           # RESUME emitted the current state
    assert "event: end" in data                  # terminal → stream closed


def test_lesson_events_unknown_404(client, fake_redis):
    r = client.get("/api/lessons/nope/events")
    assert r.status_code == 404
