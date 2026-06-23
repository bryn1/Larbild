"""Tests for the render-job HTTP API + SSE (lb-3gi, P1.2).

Acceptance criteria, end-to-end through the Flask app with a FakeRedis (no live
GPU/network):
  1. POST /api/jobs enqueues a job and returns a job id.
  2. an SSE endpoint streams queued→running→done events for that job.
  3. larbild job keys are namespaced separately from imagegen in Redis.
  4. reconnect to the SSE resumes the current job state.
"""
from __future__ import annotations

from app import create_app, jobs
from app.routes import jobs as jobs_routes
from tests._fakeredis import FakeRedis


def _app(tmp_path):
    app = create_app({"TESTING": True, "DB_PATH": tmp_path / "api.db"})
    return app


def _fresh_redis():
    r = FakeRedis()
    jobs.set_redis(r)
    return r


# ── 1. POST enqueues + returns a job id ─────────────────────────────────────


def test_post_jobs_enqueues_and_returns_id(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().post("/api/jobs", json={
            "kind": "bild", "subject": "fysik", "area": "ljud",
            "imageType": "illustration", "prompt": "a transverse wave",
        })
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["job_id"]
        assert body["status"] == "queued"
        # 3. namespaced under larbild:
        assert r.llen("larbild:render:task_queue") == 1
        assert f"larbild:render:job:{body['job_id']}" in r.all_keys()
    finally:
        jobs.set_redis(None)


def test_post_count_fans_out_n_jobs(tmp_path):
    # lb-9d5 (A1): "Antal" N -> N independent render jobs (one image per job),
    # with the first id returned for SSE tailing. count is re-clamped server-side
    # so a forged request can't queue a runaway batch.
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().post("/api/jobs", json={
            "subject": "fysik", "area": "ljud", "imageType": "illustration",
            "prompt": "a transverse wave", "params": {"count": 3},
        })
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["count"] == 3
        assert len(body["job_ids"]) == 3
        assert body["job_id"] == body["job_ids"][0]
        assert len(set(body["job_ids"])) == 3  # distinct ids
        assert r.llen("larbild:render:task_queue") == 3
    finally:
        jobs.set_redis(None)


def test_post_count_clamped_and_default_single(tmp_path):
    # count=1 (default) is the prior single-job behaviour; over-cap clamps to 8.
    _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        one = c.post("/api/jobs", json={
            "subject": "fysik", "area": "ljud", "imageType": "illustration",
            "prompt": "x",
        }).get_json()
        assert one["count"] == 1 and len(one["job_ids"]) == 1
        many = c.post("/api/jobs", json={
            "subject": "fysik", "area": "ljud", "imageType": "illustration",
            "prompt": "y", "params": {"count": 999},
        }).get_json()
        assert many["count"] == 8 and len(many["job_ids"]) == 8
    finally:
        jobs.set_redis(None)


def test_post_rejects_unknown_subject(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().post("/api/jobs", json={
            "subject": "nope", "area": "ljud", "imageType": "foto", "prompt": "x",
        })
        assert resp.status_code == 400
        assert "valid_subjects" in resp.get_json()
        assert r.llen("larbild:render:task_queue") == 0  # nothing enqueued
    finally:
        jobs.set_redis(None)


def test_post_rejects_area_not_in_subject(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().post("/api/jobs", json={
            "subject": "fysik", "area": "celler",  # celler is a biologi area
            "imageType": "foto", "prompt": "x",
        })
        assert resp.status_code == 400
    finally:
        jobs.set_redis(None)


def test_post_bild_requires_image_type(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().post("/api/jobs", json={
            "kind": "bild", "subject": "fysik", "area": "ljud", "prompt": "x",
        })
        assert resp.status_code == 400
        assert "valid_image_types" in resp.get_json()
    finally:
        jobs.set_redis(None)


def test_post_film_requires_video_type(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().post("/api/jobs", json={
            "kind": "film", "subject": "biologi", "area": "celler", "prompt": "x",
        })
        assert resp.status_code == 400
        assert "valid_video_types" in resp.get_json()
    finally:
        jobs.set_redis(None)


def test_post_ignores_client_supplied_lora(tmp_path):
    # The SFW guard: a client cannot pick the LoRA. A 'lora' in the body is
    # dropped — the worker resolves it server-side from (subject, imageType).
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().post("/api/jobs", json={
            "subject": "fysik", "area": "ljud", "imageType": "foto",
            "prompt": "x", "lora": "off-allowlist-fake-v9",
        })
        assert resp.status_code == 202
        job_id = resp.get_json()["job_id"]
        # the durable row's params blob did NOT capture the client lora
        row = jobs.get_job_row(job_id)
        import json as _json
        params = _json.loads(row["params_json"]) if row["params_json"] else {}
        assert "lora" not in params
        # and the jobs.lora column is unset (worker fills it at render time)
        assert row["lora"] is None
    finally:
        jobs.set_redis(None)


def test_post_queue_full_returns_429(tmp_path, monkeypatch):
    _fresh_redis()
    try:
        monkeypatch.setattr(jobs, "MAX_DEPTH", 1)
        app = _app(tmp_path)
        c = app.test_client()
        ok = c.post("/api/jobs", json={"subject": "fysik", "area": "ljud",
                                       "imageType": "foto", "prompt": "a"})
        assert ok.status_code == 202
        full = c.post("/api/jobs", json={"subject": "fysik", "area": "ljud",
                                         "imageType": "foto", "prompt": "b"})
        assert full.status_code == 429
        assert "limit" in full.get_json()
    finally:
        jobs.set_redis(None)


def test_post_rejects_non_integer_seed(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().post("/api/jobs", json={
            "subject": "fysik", "area": "ljud", "imageType": "foto",
            "prompt": "x", "seed": "abc",
        })
        assert resp.status_code == 400
    finally:
        jobs.set_redis(None)


# ── GET /api/jobs/<id> ──────────────────────────────────────────────────────


def test_get_job_returns_state(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        job_id = c.post("/api/jobs", json={"subject": "fysik", "area": "ljud",
                                           "imageType": "foto", "prompt": "x"}).get_json()["job_id"]
        resp = c.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "queued"
    finally:
        jobs.set_redis(None)


def test_get_unknown_job_404(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().get("/api/jobs/nope")
        assert resp.status_code == 404
    finally:
        jobs.set_redis(None)


# ── 2 + 4. SSE streams queued→running→done; reconnect resumes ───────────────


def _parse_sse_data(text: str) -> list[str]:
    """Return the payloads of every `data:` line in an SSE response body."""
    return [ln[len("data:"):].strip() for ln in text.splitlines() if ln.startswith("data:")]


def test_sse_resumes_current_state_on_connect(tmp_path, monkeypatch):
    # 4. A reconnecting client gets the CURRENT state immediately. Drive the job
    # to 'done' BEFORE connecting; the SSE must emit done then end (not hang).
    _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        job_id = c.post("/api/jobs", json={"subject": "fysik", "area": "ljud",
                                           "imageType": "foto", "prompt": "x"}).get_json()["job_id"]
        jobs.write_job_state(job_id, {"job_id": job_id, "status": "done",
                                      "library_id": "lib-1"})
        resp = c.get(f"/api/jobs/{job_id}/events")
        body = resp.get_data(as_text=True)
        payloads = _parse_sse_data(body)
        assert any("done" in p for p in payloads)  # current state resumed on connect
        assert "event: end" in body                # terminal → stream closes
    finally:
        jobs.set_redis(None)


def test_sse_streams_queued_running_done(tmp_path, monkeypatch):
    # 2. Stream the full lifecycle. We pre-stage states the SSE poll will read in
    # order by making read_job_state return queued, then running, then done on
    # successive calls — no background worker/thread needed in the test.
    _fresh_redis()
    try:
        monkeypatch.setattr(jobs_routes, "SSE_POLL_S", 0.0)
        monkeypatch.setattr(jobs_routes, "SSE_IDLE_TIMEOUT_S", 5.0)
        app = _app(tmp_path)
        c = app.test_client()
        job_id = c.post("/api/jobs", json={"subject": "fysik", "area": "ljud",
                                           "imageType": "foto", "prompt": "x"}).get_json()["job_id"]

        seq = iter([
            {"job_id": job_id, "status": "queued"},
            {"job_id": job_id, "status": "running", "progress_step": 10, "progress_total": 28},
            {"job_id": job_id, "status": "done", "library_id": "lib-2"},
            {"job_id": job_id, "status": "done", "library_id": "lib-2"},
        ])
        last = {"v": {"job_id": job_id, "status": "queued"}}

        def _fake_read(jid):
            try:
                last["v"] = next(seq)
            except StopIteration:
                pass
            return last["v"]

        monkeypatch.setattr(jobs_routes, "read_job_state", _fake_read)
        body = c.get(f"/api/jobs/{job_id}/events").get_data(as_text=True)
        payloads = " | ".join(_parse_sse_data(body))
        assert "queued" in payloads
        assert "running" in payloads
        assert "done" in payloads
        assert "event: end" in body
    finally:
        jobs.set_redis(None)


def test_sse_unknown_job_404(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().get("/api/jobs/nope/events")
        assert resp.status_code == 404
    finally:
        jobs.set_redis(None)


def test_sse_connection_cap_returns_503(tmp_path, monkeypatch):
    _fresh_redis()
    try:
        monkeypatch.setattr(jobs_routes, "SSE_MAX_CONN", 0)
        app = _app(tmp_path)
        c = app.test_client()
        job_id = c.post("/api/jobs", json={"subject": "fysik", "area": "ljud",
                                           "imageType": "foto", "prompt": "x"}).get_json()["job_id"]
        resp = c.get(f"/api/jobs/{job_id}/events")
        assert resp.status_code == 503
    finally:
        jobs.set_redis(None)


# ── lb-07y: Kö management API (list / cancel / retry) ────────────────────────


def _enqueue(client):
    resp = client.post("/api/jobs", json={
        "kind": "bild", "subject": "fysik", "area": "ljud",
        "imageType": "illustration", "prompt": "a wave",
    })
    return resp.get_json()["job_id"]


def test_get_jobs_lists_and_filters(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        jid = _enqueue(c)
        resp = c.get("/api/jobs")
        assert resp.status_code == 200
        ids = [j["id"] for j in resp.get_json()["jobs"]]
        assert jid in ids
        # filter
        assert all(j["status"] == "queued"
                   for j in c.get("/api/jobs?status=queued").get_json()["jobs"])
        # bad status → 400
        assert c.get("/api/jobs?status=bogus").status_code == 400
    finally:
        jobs.set_redis(None)


def test_post_cancel_marks_cancelled(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        jid = _enqueue(c)
        assert r.llen("larbild:render:task_queue") == 1
        resp = c.post(f"/api/jobs/{jid}/cancel")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "cancelled"
        assert r.llen("larbild:render:task_queue") == 0  # no orphaned queue entry
        # unknown → 404
        assert c.post("/api/jobs/nope/cancel").status_code == 404
    finally:
        jobs.set_redis(None)


def test_post_cancel_terminal_is_409(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        jid = _enqueue(c)
        jobs.write_job_state(jid, {"job_id": jid, "status": "done", "library_id": "L"})
        assert c.post(f"/api/jobs/{jid}/cancel").status_code == 409
    finally:
        jobs.set_redis(None)


def test_post_retry_requeues_failed(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        jid = _enqueue(c)
        r.brpop("larbild:render:task_queue")  # worker claimed it
        jobs.write_job_state(jid, {"job_id": jid, "status": "failed", "error": "x"})
        resp = c.post(f"/api/jobs/{jid}/retry")
        assert resp.status_code == 202
        assert resp.get_json()["status"] == "queued"
        assert r.llen("larbild:render:task_queue") == 1
        # retry a queued job → 409 (already in flight)
        assert c.post(f"/api/jobs/{jid}/retry").status_code == 409
        # unknown → 404
        assert c.post("/api/jobs/nope/retry").status_code == 404
    finally:
        jobs.set_redis(None)

