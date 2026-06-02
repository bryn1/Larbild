"""Boot-time stuck-job reconcile (lb-qtr, partial — orphaned 'running' jobs).

Verifies the build-plan §P10 'orphaned/stuck jobs on boot' slice: on startup a
job left ``running`` by a crashed worker is reconciled to ``failed`` (with a
reason + finished stamp), while ``queued`` and terminal jobs are left untouched;
the operation is idempotent.
"""
from __future__ import annotations

import json

import pytest

from app import jobs
from app.db import connect, init_db, new_id
from tests._fakeredis import FakeRedis


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "reconcile.db"
    init_db(p)  # runs migrations → jobs table exists
    fake = FakeRedis()
    jobs.set_redis(fake)
    yield p, fake
    jobs.set_redis(None)


def _insert(status: str) -> str:
    job_id = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, kind, status, subject, area, image_type, "
            "started_at) VALUES (?, 'bild', ?, 'fysik', 'ljud', 'foto', "
            "datetime('now'))",
            (job_id, status),
        )
    return job_id


def _status(job_id: str) -> str:
    with connect() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row["status"]


def test_running_job_is_failed_on_reconcile(db):
    running = _insert("running")
    reconciled = jobs.reconcile_stuck_jobs()
    assert running in reconciled
    assert _status(running) == "failed"
    # reason + finished stamp recorded
    with connect() as conn:
        row = conn.execute(
            "SELECT error, finished_at FROM jobs WHERE id = ?", (running,)
        ).fetchone()
    assert "reconciled on boot" in (row["error"] or "")
    assert row["finished_at"]


def test_queued_job_is_left_pending(db):
    """A queued job is NOT failed — a fresh worker can still pick it up."""
    queued = _insert("queued")
    jobs.reconcile_stuck_jobs()
    assert _status(queued) == "queued"


def test_terminal_jobs_untouched(db):
    done = _insert("done")
    failed = _insert("failed")
    cancelled = _insert("cancelled")
    jobs.reconcile_stuck_jobs()
    assert _status(done) == "done"
    assert _status(failed) == "failed"
    assert _status(cancelled) == "cancelled"


def test_reconcile_is_idempotent(db):
    _insert("running")
    first = jobs.reconcile_stuck_jobs()
    assert len(first) == 1
    second = jobs.reconcile_stuck_jobs()
    assert second == []  # nothing left running on the second boot


def test_reconcile_mirrors_failed_state_into_redis(db):
    _, fake = db
    running = _insert("running")
    jobs.reconcile_stuck_jobs()
    raw = fake.get(f"{jobs.JOB_STATE_PREFIX}{running}")
    assert raw is not None
    state = json.loads(raw)
    assert state["status"] == "failed"


def test_create_app_reconciles_on_boot(tmp_path):
    """create_app() runs the reconcile after migrations (the wired path)."""
    from app import create_app

    db_path = tmp_path / "boot.db"
    init_db(db_path)
    jobs.set_redis(FakeRedis())
    try:
        running = _insert("running")
        # Boot a fresh app pointed at the same DB; _init_database reconciles.
        create_app({"DB_PATH": db_path, "START_GPU_TELEMETRY": False})
        assert _status(running) == "failed"
    finally:
        jobs.set_redis(None)
