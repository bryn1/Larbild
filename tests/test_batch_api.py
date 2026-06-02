"""Tests for the batch fan-out + aggregate-progress HTTP API (lb-y9c, P5.2).

Acceptance criteria, end-to-end through the Flask app with a FakeRedis (no live
GPU/network):
  1. POST /api/jobs/batch enqueues one job per valid row (valid CSV → N jobs),
     under one batch_id, through the existing /api/jobs bridge;
  2. invalid rows are rejected with reasons and NOT enqueued (partial batch);
  3. the batch has a group id whose aggregate progress is queryable, rolled up
     from the jobs table (queued/running/done/failed counts);
  4. partial failures don't abort the whole batch;
  5. a client-supplied LoRA is ignored (the worker resolves it server-side);
  6. JSON-row + file-upload bodies validate identically to a raw CSV;
  7. the batch SSE streams aggregate progress to completion.
"""
from __future__ import annotations

import io
import json

from app import create_app, jobs
from app import batch as batch_mod
from app.routes import batch as batch_routes
from tests._fakeredis import FakeRedis


def _app(tmp_path):
    return create_app({"TESTING": True, "DB_PATH": tmp_path / "batch.db"})


def _fresh_redis():
    r = FakeRedis()
    jobs.set_redis(r)
    return r


_CSV = (
    "subject,area,imageType,prompt\n"
    "fysik,ljud,illustration,a transverse wave\n"
    "biologi,celler,foto,a plant cell\n"
    "kemi,materia,diagram,the periodic table\n"
)


# ── 1. valid CSV enqueues N jobs under one batch_id ─────────────────────────


def test_post_batch_enqueues_n_jobs(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().post(
            "/api/jobs/batch", data=_CSV, content_type="text/csv"
        )
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["enqueued_count"] == 3
        assert len(body["job_ids"]) == 3
        assert body["batch_id"]
        assert body["rejected_count"] == 0
        # one batch_id, three jobs actually on the larbild queue
        assert r.llen("larbild:render:task_queue") == 3
        # each job row carries the batch_id (fan-out goes through the bridge)
        for jid in body["job_ids"]:
            row = jobs.get_job_row(jid)
            assert row["batch_id"] == body["batch_id"]
            assert row["status"] == "queued"
    finally:
        jobs.set_redis(None)


def test_post_batch_count_column_expands(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        csv = "subject,area,imageType,prompt,count\nfysik,ljud,foto,x,3\n"
        body = app.test_client().post(
            "/api/jobs/batch", data=csv, content_type="text/csv"
        ).get_json()
        assert body["enqueued_count"] == 3  # one row, count=3 → 3 jobs
        assert r.llen("larbild:render:task_queue") == 3
    finally:
        jobs.set_redis(None)


# ── 2. invalid rows rejected with reasons, valid still enqueued ─────────────


def test_post_batch_partial_rejects_bad_rows(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        csv = (
            "subject,area,imageType,prompt\n"
            "fysik,ljud,illustration,good\n"   # line 2 valid
            "nope,ljud,foto,bad subject\n"     # line 3 invalid
            "fysik,celler,foto,bad area\n"     # line 4 invalid (celler not fysik)
            "biologi,celler,foto,good two\n"   # line 5 valid
        )
        resp = app.test_client().post("/api/jobs/batch", data=csv,
                                      content_type="text/csv")
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["enqueued_count"] == 2
        assert body["rejected_count"] == 2
        assert r.llen("larbild:render:task_queue") == 2  # only valid rows enqueued
        lines = {e["line"] for e in body["rejected"]}
        assert lines == {3, 4}
        reasons = " ".join(e["error"] for e in body["rejected"])
        assert "unknown subject" in reasons
        assert "unknown area" in reasons
    finally:
        jobs.set_redis(None)


def test_post_batch_all_invalid_returns_400(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        csv = "subject,area,imageType,prompt\nnope,ljud,foto,x\nbad,x,y,z\n"
        resp = app.test_client().post("/api/jobs/batch", data=csv,
                                      content_type="text/csv")
        assert resp.status_code == 400  # nothing enqueueable
        body = resp.get_json()
        assert body["valid_count"] == 0
        assert body["rejected_count"] == 2
        assert r.llen("larbild:render:task_queue") == 0
    finally:
        jobs.set_redis(None)


def test_post_batch_empty_body_400(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().post("/api/jobs/batch", data="",
                                      content_type="text/csv")
        assert resp.status_code == 400
    finally:
        jobs.set_redis(None)


# ── 3. aggregate progress is queryable + rolled up from the jobs table ──────


def test_get_batch_progress_aggregates_job_states(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        body = c.post("/api/jobs/batch", data=_CSV,
                      content_type="text/csv").get_json()
        batch_id = body["batch_id"]
        job_ids = body["job_ids"]

        # all queued initially
        prog = c.get(f"/api/jobs/batch/{batch_id}").get_json()
        assert prog["total"] == 3
        assert prog["counts"]["queued"] == 3
        assert prog["done"] is False

        # drive one job to running, one to done (worker writeback path)
        jobs.write_job_state(job_ids[0], {"job_id": job_ids[0], "status": "running",
                                          "progress_step": 5, "progress_total": 28})
        jobs.write_job_state(job_ids[1], {"job_id": job_ids[1], "status": "done",
                                          "library_id": "lib-x"})
        prog = c.get(f"/api/jobs/batch/{batch_id}").get_json()
        assert prog["counts"]["running"] == 1
        assert prog["counts"]["done"] == 1
        assert prog["counts"]["queued"] == 1
        assert prog["done"] is False  # one still in flight

        # finish the rest → done flips true
        jobs.write_job_state(job_ids[0], {"job_id": job_ids[0], "status": "done",
                                          "library_id": "lib-y"})
        jobs.write_job_state(job_ids[2], {"job_id": job_ids[2], "status": "failed",
                                          "error": "boom"})
        prog = c.get(f"/api/jobs/batch/{batch_id}").get_json()
        assert prog["counts"]["done"] == 2
        assert prog["counts"]["failed"] == 1
        assert prog["done"] is True  # no job queued/running anymore
    finally:
        jobs.set_redis(None)


def test_get_unknown_batch_404(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().get("/api/jobs/batch/does-not-exist")
        assert resp.status_code == 404
    finally:
        jobs.set_redis(None)


def test_batch_progress_reports_rejected_rows(tmp_path):
    # the rejected rows captured at submit time are persisted + queryable later
    _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        csv = (
            "subject,area,imageType,prompt\n"
            "fysik,ljud,foto,ok\n"
            "nope,ljud,foto,bad\n"
        )
        batch_id = c.post("/api/jobs/batch", data=csv,
                          content_type="text/csv").get_json()["batch_id"]
        prog = c.get(f"/api/jobs/batch/{batch_id}").get_json()
        assert prog["rejected_count"] == 1
        assert prog["errors"][0]["line"] == 3
        assert "unknown subject" in prog["errors"][0]["error"]
    finally:
        jobs.set_redis(None)


def test_all_rejected_batch_is_done_immediately(tmp_path):
    # a batch that enqueued zero jobs is 'done' (nothing to wait on). Build it via
    # the backend directly since the route 400s an all-invalid POST.
    _fresh_redis()
    try:
        app = _app(tmp_path)
        with app.app_context():
            spec = batch_mod.parse_batch(
                "subject,area,imageType,prompt\nnope,ljud,foto,x\n")
            result = batch_mod.fan_out(spec, source="csv")
            prog = batch_mod.batch_progress(result["batch_id"])
        assert prog["total"] == 0
        assert prog["done"] is True
        assert prog["rejected_count"] == 1
    finally:
        jobs.set_redis(None)


# ── 4. partial enqueue failures don't abort the batch ───────────────────────


def test_partial_enqueue_failure_does_not_abort_batch(tmp_path, monkeypatch):
    # Queue fills after 2 jobs: the 3rd+ can't enqueue but the batch still
    # returns its batch_id + the 2 it managed, with the rest reported as failures.
    _fresh_redis()
    try:
        monkeypatch.setattr(jobs, "MAX_DEPTH", 2)
        app = _app(tmp_path)
        resp = app.test_client().post("/api/jobs/batch", data=_CSV,
                                      content_type="text/csv")
        assert resp.status_code == 202  # not aborted
        body = resp.get_json()
        assert body["enqueued_count"] == 2          # queue cap hit at 2
        assert len(body["enqueue_failures"]) >= 1   # the rest reported
        assert body["batch_id"]
        # aggregate reflects only the jobs that made it
        prog = app.test_client().get(f"/api/jobs/batch/{body['batch_id']}").get_json()
        assert prog["total"] == 2
    finally:
        jobs.set_redis(None)


# ── 5. client LoRA ignored (SFW guard) through the batch path ───────────────


def test_post_batch_ignores_client_lora(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        csv = (
            "subject,area,imageType,prompt,lora\n"
            "fysik,ljud,foto,x,off-allowlist-fake-v9\n"
        )
        body = app.test_client().post("/api/jobs/batch", data=csv,
                                      content_type="text/csv").get_json()
        jid = body["job_ids"][0]
        row = jobs.get_job_row(jid)
        # the jobs.lora column is unset (worker resolves it at render time)
        assert row["lora"] is None
        # and the client lora never reached the durable params blob
        params = json.loads(row["params_json"]) if row["params_json"] else {}
        assert "lora" not in {k.lower() for k in params}
    finally:
        jobs.set_redis(None)


# ── 6. JSON-row + file-upload bodies validate identically ───────────────────


def test_post_batch_json_rows(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        rows = [
            {"subject": "fysik", "area": "ljud", "imageType": "foto", "prompt": "a"},
            {"subject": "biologi", "area": "celler", "imageType": "illustration",
             "prompt": "b"},
            {"subject": "nope", "area": "ljud", "imageType": "foto", "prompt": "bad"},
        ]
        resp = app.test_client().post("/api/jobs/batch", json=rows)
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["enqueued_count"] == 2
        assert body["rejected_count"] == 1
        assert r.llen("larbild:render:task_queue") == 2
    finally:
        jobs.set_redis(None)


def test_post_batch_json_rows_object_with_title(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        payload = {"title": "fysik ljud fill",
                   "rows": [{"subject": "fysik", "area": "ljud",
                             "imageType": "foto", "prompt": "a"}]}
        body = app.test_client().post("/api/jobs/batch", json=payload).get_json()
        prog = app.test_client().get(f"/api/jobs/batch/{body['batch_id']}").get_json()
        assert prog["title"] == "fysik ljud fill"
        assert prog["source"] == "json"
    finally:
        jobs.set_redis(None)


def test_post_batch_json_row_lora_dropped(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        rows = [{"subject": "fysik", "area": "ljud", "imageType": "foto",
                 "prompt": "a", "lora": "sneaky-v9"}]
        body = app.test_client().post("/api/jobs/batch", json=rows).get_json()
        row = jobs.get_job_row(body["job_ids"][0])
        assert row["lora"] is None
        params = json.loads(row["params_json"]) if row["params_json"] else {}
        assert "lora" not in {k.lower() for k in params}
    finally:
        jobs.set_redis(None)


def test_post_batch_file_upload(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        data = {"file": (io.BytesIO(_CSV.encode("utf-8")), "fill.csv")}
        resp = app.test_client().post("/api/jobs/batch", data=data,
                                      content_type="multipart/form-data")
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["enqueued_count"] == 3
        assert r.llen("larbild:render:task_queue") == 3
        # source/title recorded from the upload
        prog = app.test_client().get(f"/api/jobs/batch/{body['batch_id']}").get_json()
        assert prog["source"] == "csv"
        assert prog["title"] == "fill.csv"
    finally:
        jobs.set_redis(None)


def test_post_batch_txt_upload_positional(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        txt = "fysik ljud foto a wave\nbiologi celler illustration mitosis\n"
        data = {"file": (io.BytesIO(txt.encode("utf-8")), "fill.txt")}
        resp = app.test_client().post("/api/jobs/batch", data=data,
                                      content_type="multipart/form-data")
        assert resp.status_code == 202
        assert resp.get_json()["enqueued_count"] == 2
        assert r.llen("larbild:render:task_queue") == 2
    finally:
        jobs.set_redis(None)


def test_post_batch_bad_json_body_400(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        # a JSON object that is neither a list nor has a 'rows' list
        resp = app.test_client().post("/api/jobs/batch", json={"foo": "bar"})
        assert resp.status_code == 400
    finally:
        jobs.set_redis(None)


# ── 7. batch SSE streams aggregate progress to completion ───────────────────


def _parse_sse_data(text: str) -> list[str]:
    return [ln[len("data:"):].strip() for ln in text.splitlines() if ln.startswith("data:")]


def test_batch_sse_resumes_then_ends_when_done(tmp_path, monkeypatch):
    _fresh_redis()
    try:
        monkeypatch.setattr(batch_routes, "SSE_POLL_S", 0.0)
        monkeypatch.setattr(batch_routes, "SSE_IDLE_TIMEOUT_S", 5.0)
        app = _app(tmp_path)
        c = app.test_client()
        body = c.post("/api/jobs/batch",
                      data="subject,area,imageType,prompt\nfysik,ljud,foto,x\n",
                      content_type="text/csv").get_json()
        batch_id = body["batch_id"]
        job_ids = body["job_ids"]

        # pre-stage aggregate states: queued → done across successive reads.
        seq = iter([
            {"batch_id": batch_id, "total": 1, "done": False,
             "counts": {"queued": 1, "running": 0, "done": 0, "failed": 0, "cancelled": 0}},
            {"batch_id": batch_id, "total": 1, "done": False,
             "counts": {"queued": 0, "running": 1, "done": 0, "failed": 0, "cancelled": 0}},
            {"batch_id": batch_id, "total": 1, "done": True,
             "counts": {"queued": 0, "running": 0, "done": 1, "failed": 0, "cancelled": 0}},
        ])
        last = {"v": None}

        def _fake_progress(bid):
            try:
                last["v"] = next(seq)
            except StopIteration:
                pass
            return last["v"]

        monkeypatch.setattr(batch_routes.batch_mod, "batch_progress", _fake_progress)
        body_txt = c.get(f"/api/jobs/batch/{batch_id}/events").get_data(as_text=True)
        payloads = " | ".join(_parse_sse_data(body_txt))
        assert "running" in payloads
        assert '"done": true' in body_txt or '"done":true' in payloads
        assert "event: end" in body_txt
        assert job_ids  # batch really enqueued
    finally:
        jobs.set_redis(None)


def test_batch_sse_unknown_batch_404(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        resp = app.test_client().get("/api/jobs/batch/nope/events")
        assert resp.status_code == 404
    finally:
        jobs.set_redis(None)


def test_batch_sse_connection_cap_503(tmp_path, monkeypatch):
    _fresh_redis()
    try:
        monkeypatch.setattr(batch_routes, "SSE_MAX_CONN", 0)
        app = _app(tmp_path)
        c = app.test_client()
        batch_id = c.post("/api/jobs/batch",
                          data="subject,area,imageType,prompt\nfysik,ljud,foto,x\n",
                          content_type="text/csv").get_json()["batch_id"]
        resp = c.get(f"/api/jobs/batch/{batch_id}/events")
        assert resp.status_code == 503
    finally:
        jobs.set_redis(None)
