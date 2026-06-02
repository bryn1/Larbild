"""Tests for the Redis render-job queue + state (lb-3gi, P1.2).

No real Redis (FakeRedis injected via set_redis) and no live GPU. Covers the
queue core, the worker-contract state writes, the durable-row mirror, the
queue-depth cap, and — critically — that larbild keys are namespaced disjoint
from imagegen's ``vm100:image_gen:`` keyspace.
"""
from __future__ import annotations

import json

import pytest

from app import jobs
from app.db import connect, init_db
from tests._fakeredis import FakeRedis


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "jobs.db"
    init_db(p)  # runs migrations → jobs/library/settings tables exist
    return p


@pytest.fixture
def fake_redis():
    r = FakeRedis()
    jobs.set_redis(r)
    yield r
    jobs.set_redis(None)


def test_enqueue_returns_id_and_pushes_to_namespaced_queue(db, fake_redis):
    job_id = jobs.enqueue_job(
        kind="bild", subject="fysik", area="ljud", image_type="illustration",
        prompt="a sound wave diagram",
    )
    assert job_id
    # pushed onto the larbild-namespaced queue key, not imagegen's
    assert jobs.QUEUE_KEY == "larbild:render:task_queue"
    assert fake_redis.llen("larbild:render:task_queue") == 1
    # the job id is the queued payload
    assert fake_redis.brpop("larbild:render:task_queue")[1] == job_id


def test_keys_are_namespaced_under_larbild(db, fake_redis):
    job_id = jobs.enqueue_job(
        kind="bild", subject="fysik", area="ljud", image_type="illustration",
        prompt="x",
    )
    # EVERY key the queue created is under larbild: — none under vm100:image_gen:
    for k in fake_redis.all_keys():
        assert k.startswith("larbild:"), f"non-namespaced key leaked: {k}"
        assert "vm100:image_gen:" not in k
    # the live state key has the documented shape
    assert f"larbild:render:job:{job_id}" in fake_redis.all_keys()


def test_no_collision_with_imagegen_keyspace(db, fake_redis):
    # Pre-seed an imagegen-style key + queue entry; a larbild enqueue must not
    # read, pop, or overwrite it.
    fake_redis.set("vm100:image_gen:job:other", json.dumps({"status": "running"}))
    fake_redis.rpush("vm100:image_gen:task_queue", "imagegen-job-1")

    jobs.enqueue_job(kind="bild", subject="kemi", area="vatten",
                     image_type="illustration", prompt="x")

    # imagegen's key + queue are untouched
    assert fake_redis.get("vm100:image_gen:job:other") is not None
    assert fake_redis.llen("vm100:image_gen:task_queue") == 1
    # larbild's queue has exactly its own one job
    assert fake_redis.llen("larbild:render:task_queue") == 1


def test_initial_state_is_queued_resume(db, fake_redis):
    job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                              image_type="foto", prompt="x")
    state = jobs.read_job_state(job_id)
    assert state["status"] == "queued"
    assert state["job_id"] == job_id


def test_durable_row_written_on_enqueue(db, fake_redis):
    job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                              image_type="foto", prompt="a pendulum", seed=99)
    row = jobs.get_job_row(job_id)
    assert row["status"] == "queued"
    assert row["subject"] == "fysik" and row["area"] == "ljud"
    assert row["image_type"] == "foto"
    assert row["seed"] == 99


def _insert_library_row(db_path, lib_id, *, subject="fysik", area="ljud"):
    """Insert a library row (mirrors lb-ocx writing the artefact before linking)."""
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO library (id, kind, subject, area, image_type, title, file_path) "
            "VALUES (?, 'bild', ?, ?, 'foto', 'T', ?)",
            (lib_id, subject, area, f"{subject}/{area}/{lib_id}.png"),
        )


def test_worker_contract_running_then_done(db, fake_redis):
    job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                              image_type="foto", prompt="x")
    # worker: running with progress
    jobs.write_job_state(job_id, {"job_id": job_id, "status": "running",
                                  "progress_step": 5, "progress_total": 28})
    st = jobs.read_job_state(job_id)
    assert st["status"] == "running" and st["progress_step"] == 5
    # mirrored into durable row
    assert jobs.get_job_row(job_id)["status"] == "running"
    assert jobs.get_job_row(job_id)["progress_step"] == 5

    # worker: writes the library artefact FIRST, then marks done + links it
    # (the lb-ocx contract — the jobs.library_id FK references library.id).
    _insert_library_row(db, "lib-123")
    jobs.write_job_state(job_id, {"job_id": job_id, "status": "done",
                                  "library_id": "lib-123", "width": 1024, "height": 1024})
    st = jobs.read_job_state(job_id)
    assert st["status"] == "done" and st["library_id"] == "lib-123"
    row = jobs.get_job_row(job_id)
    assert row["status"] == "done" and row["library_id"] == "lib-123"
    assert row["finished_at"] is not None


def test_done_status_persists_even_if_library_link_fails(db, fake_redis):
    # Defensive: if a 'done' marker names a library_id whose row doesn't exist
    # (a contract slip — the worker should write the library row first), the FK
    # on jobs.library_id must NOT roll back the status mirror. The job still goes
    # 'done'; only the (impossible) link is dropped.
    job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                              image_type="foto", prompt="x")
    jobs.write_job_state(job_id, {"job_id": job_id, "status": "done",
                                  "library_id": "lib-does-not-exist"})
    row = jobs.get_job_row(job_id)
    assert row["status"] == "done"           # status persisted
    assert row["library_id"] is None         # the bad FK link was isolated/dropped


def test_worker_contract_failed_records_error(db, fake_redis):
    job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                              image_type="foto", prompt="x")
    jobs.write_job_state(job_id, {"job_id": job_id, "status": "failed",
                                  "error": "comfyui timeout"})
    assert jobs.read_job_state(job_id)["error"] == "comfyui timeout"
    assert jobs.get_job_row(job_id)["error"] == "comfyui timeout"


def test_state_falls_back_to_db_when_redis_state_gone(db, fake_redis):
    job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                              image_type="foto", prompt="x")
    _insert_library_row(db, "lib-9")
    jobs.write_job_state(job_id, {"job_id": job_id, "status": "done",
                                  "library_id": "lib-9"})
    # simulate the 24h Redis TTL expiring: drop the live state key
    fake_redis.delete(f"larbild:render:job:{job_id}")
    # GET still returns the last-known status from the durable jobs row
    st = jobs.read_job_state(job_id)
    assert st is not None and st["status"] == "done" and st["library_id"] == "lib-9"


def test_read_unknown_job_returns_none(db, fake_redis):
    assert jobs.read_job_state("does-not-exist") is None


def test_queue_full_raises_before_persisting(db, fake_redis, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_DEPTH", 2)
    jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                     image_type="foto", prompt="a")
    jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                     image_type="foto", prompt="b")
    with pytest.raises(jobs.QueueFull):
        jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                         image_type="foto", prompt="c")
    # the rejected job left no durable row (only the 2 that fit)
    with connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert n == 2


def test_queue_depth_reads_namespaced_key(db, fake_redis):
    assert jobs.queue_depth() == 0
    jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                     image_type="foto", prompt="x")
    assert jobs.queue_depth() == 1


def test_film_job_uses_video_type(db, fake_redis):
    job_id = jobs.enqueue_job(kind="film", subject="biologi", area="celler",
                              video_type="lektionsfilm", prompt="how cells divide")
    row = jobs.get_job_row(job_id)
    assert row["kind"] == "film"
    assert row["video_type"] == "lektionsfilm"
    assert row["image_type"] is None
