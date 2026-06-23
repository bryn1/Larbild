"""Redis render-job queue + job state (lb-3gi, P1.2).

Forks imagegen's Redis queue + worker-result pattern (``sibbis-app/job_status.py``
+ ``api_compose.py``) for Lärbild, with two deliberate differences:

1. **Namespace.** Every key is under ``larbild:`` — never imagegen's
   ``vm100:image_gen:``. ``LARBILD_QUEUE_KEY = "larbild:render:task_queue"``; live
   per-job state at ``larbild:render:job:{job_id}``. So Lärbild and imagegen jobs
   share the same VM400 Redis *server* (one Redis, reached via the same
   :func:`app.gpu_lock.make_redis_client`) but live in disjoint keyspaces — they
   never read or pop each other's jobs. (The GPU *lock* key is the one thing
   shared verbatim — coordination, not data.)

2. **Two stores, one truth each.** The durable job record is a row in the
   ``jobs`` SQLite table (lb-ah2) — it survives a Redis flush and is what the
   library auto-sort (lb-b1r) + reconcile-on-boot (lb-qtr) read. Redis holds the
   **live progress** state for the SSE stream: a small JSON blob with a 24h TTL,
   updated by the render worker (the worker contract below) and tailed by the
   ``/api/jobs/<id>/events`` SSE endpoint. SQLite is authoritative for "did this
   job exist / what was it"; Redis is authoritative for "where is it right now".

Worker contract (the render worker — lb-ocx — writes these to
``larbild:render:job:{job_id}`` as JSON):

    {"status": "queued"}
    {"status": "running", "progress_step": int, "progress_total": int}
    {"status": "done",   "library_id": str, "width": int, "height": int}
    {"status": "failed", "error": str}
    {"status": "cancelled"}

SFW-only: pure queue plumbing; no restricted import, model, or reference.

Env overrides:
  LARBILD_QUEUE_MAX_DEPTH        default 64  (enqueue past this → QueueFull/429)
  LARBILD_JOB_STATE_TTL_S        default 86400 (24h) live-state TTL in Redis
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from . import gpu_lock
from .db import connect, new_id, now_iso

log = logging.getLogger("larbild.jobs")

__all__ = [
    "QUEUE_KEY",
    "JOB_STATE_PREFIX",
    "QueueFull",
    "UnknownJob",
    "JobNotActionable",
    "get_redis",
    "set_redis",
    "enqueue_job",
    "read_job_state",
    "write_job_state",
    "queue_depth",
    "get_job_row",
    "list_jobs",
    "cancel_job",
    "retry_job",
    "is_cancelled",
    "reconcile_stuck_jobs",
]

# larbild-namespaced keys — disjoint from imagegen's vm100:image_gen:* keyspace.
QUEUE_KEY = os.environ.get("LARBILD_QUEUE_KEY", "larbild:render:task_queue")
JOB_STATE_PREFIX = os.environ.get("LARBILD_JOB_STATE_PREFIX", "larbild:render:job:")

MAX_DEPTH = int(os.environ.get("LARBILD_QUEUE_MAX_DEPTH", "64"))
JOB_STATE_TTL_S = int(os.environ.get("LARBILD_JOB_STATE_TTL_S", str(24 * 3600)))

_TERMINAL = ("done", "failed", "cancelled")

# Process-wide Redis singleton (lazy). A test can inject a FakeRedis via
# set_redis() — that override wins over the real client.
_redis_client: Optional[Any] = None
_redis_override: Optional[Any] = None


class QueueFull(RuntimeError):
    """The render queue is at capacity; the caller should back off (HTTP 429)."""

    def __init__(self, depth: int, limit: int):
        self.depth = depth
        self.limit = limit
        super().__init__(f"render queue full: {depth} >= {limit}")


class UnknownJob(LookupError):
    """No durable ``jobs`` row for this id (HTTP 404)."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__(f"unknown job: {job_id}")


class JobNotActionable(RuntimeError):
    """The requested action does not apply to the job's current status (409).

    e.g. cancelling a job that is already terminal, or retrying a job that did
    not fail. Carries the current status so the caller can explain it.
    """

    def __init__(self, job_id: str, status: str, action: str):
        self.job_id = job_id
        self.status = status
        self.action = action
        super().__init__(f"cannot {action} job {job_id}: status is {status!r}")


def set_redis(client: Optional[Any]) -> None:
    """Inject a Redis client (or a FakeRedis) — highest precedence.

    Tests call ``set_redis(FakeRedis())`` so no real Redis is touched; pass
    ``None`` to clear the override.
    """
    global _redis_override
    _redis_override = client


def get_redis() -> Any:
    """The Redis client — the SAME VM400 server imagegen uses (larbild keyspace).

    Precedence: an injected test override, then a cached singleton built via
    :func:`app.gpu_lock.make_redis_client` (so the connection config is shared
    with the GPU lock and the rest of Lärbild). Raises if Redis is unavailable —
    the queue requires it, unlike the GPU lock which degrades.
    """
    if _redis_override is not None:
        return _redis_override
    global _redis_client
    if _redis_client is None:
        client = gpu_lock.make_redis_client()
        if client is None:
            raise RuntimeError(
                "Redis unavailable: install redis + set LARBILD_REDIS_* "
                "(or IMAGE_GEN_REDIS_*) to point at the VM400 server"
            )
        _redis_client = client
    return _redis_client


def queue_depth() -> int:
    """Current number of queued (not-yet-claimed) render jobs. 0 on Redis error."""
    try:
        return int(get_redis().llen(QUEUE_KEY) or 0)
    except Exception as e:  # noqa: BLE001 — depth is advisory; never crash a caller
        log.warning("queue_depth: redis error: %s", e)
        return 0


def _state_key(job_id: str) -> str:
    return f"{JOB_STATE_PREFIX}{job_id}"


def write_job_state(job_id: str, state: dict) -> None:
    """Set the live job state in Redis (24h TTL). The worker-contract writer.

    Stored as a JSON blob at ``larbild:render:job:{job_id}``. Overwrites wholly
    (the worker sends the full current state each update). Also mirrors a
    terminal status into the durable ``jobs`` row so a later GET after the Redis
    TTL still reflects the outcome.
    """
    r = get_redis()
    r.set(_state_key(job_id), json.dumps(state), ex=JOB_STATE_TTL_S)
    status = state.get("status")
    if status in _TERMINAL or status == "running":
        _mirror_state_to_db(job_id, state)


def read_job_state(job_id: str) -> Optional[dict]:
    """The live job state from Redis, or a state reconstructed from SQLite.

    Resolution: the live Redis blob (fresh, what the worker last wrote); if it
    has expired (24h) or Redis is down, fall back to the durable ``jobs`` row so
    a GET still returns the last-known status instead of 404-ing a job that
    really ran. Returns None only when neither store knows the job.
    """
    try:
        raw = get_redis().get(_state_key(job_id))
        if raw is not None:
            return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("read_job_state: redis error for %s: %s", job_id, e)
    return _state_from_db(job_id)


def _mirror_state_to_db(job_id: str, state: dict) -> None:
    """Best-effort: reflect a live state transition into the durable jobs row.

    The status / progress / error / timestamp mirror is written in one statement
    that cannot fail on a foreign key. The ``library_id`` link (which has an FK to
    ``library``) is set in a SEPARATE, independently-guarded statement: the worker
    (lb-ocx) writes the ``library`` row BEFORE marking the job ``done``, so the FK
    is normally satisfied — but isolating it means a marker arriving before its
    library row (a contract slip) still persists the ``done`` status instead of
    rolling the whole mirror back.
    """
    status = state.get("status")
    sets = ["status = ?"]
    params: list[Any] = [status]
    if status == "running":
        sets.append("started_at = COALESCE(started_at, ?)")
        params.append(now_iso())
    if "progress_step" in state:
        sets.append("progress_step = ?")
        params.append(state.get("progress_step"))
    if "progress_total" in state:
        sets.append("progress_total = ?")
        params.append(state.get("progress_total"))
    if status in _TERMINAL:
        sets.append("finished_at = COALESCE(finished_at, ?)")
        params.append(now_iso())
    if status == "failed" and state.get("error"):
        sets.append("error = ?")
        params.append(state.get("error"))
    params.append(job_id)
    try:
        with connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
    except Exception as e:  # noqa: BLE001 — durable mirror is best-effort
        log.warning("_mirror_state_to_db failed for %s: %s", job_id, e)

    # Link the produced library row, guarded separately so its FK can't abort the
    # status mirror above.
    if status == "done" and state.get("library_id"):
        try:
            with connect() as conn:
                conn.execute(
                    "UPDATE jobs SET library_id = ? WHERE id = ?",
                    (state["library_id"], job_id),
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "_mirror_state_to_db: could not link library_id=%s for job %s "
                "(library row not yet written?): %s",
                state.get("library_id"), job_id, e,
            )


def _state_from_db(job_id: str) -> Optional[dict]:
    """Reconstruct a job-state dict from the durable jobs row, or None."""
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT id, status, progress_step, progress_total, error, "
                "library_id FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
    except Exception as e:  # noqa: BLE001
        log.warning("_state_from_db failed for %s: %s", job_id, e)
        return None
    if row is None:
        return None
    state: dict[str, Any] = {"job_id": row["id"], "status": row["status"]}
    if row["progress_step"] is not None:
        state["progress_step"] = row["progress_step"]
    if row["progress_total"] is not None:
        state["progress_total"] = row["progress_total"]
    if row["error"]:
        state["error"] = row["error"]
    if row["library_id"]:
        state["library_id"] = row["library_id"]
    return state


def get_job_row(job_id: str) -> Optional[dict]:
    """The full durable ``jobs`` row as a dict, or None."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


def enqueue_job(
    *,
    kind: str,
    subject: str,
    area: str,
    image_type: Optional[str] = None,
    video_type: Optional[str] = None,
    title: Optional[str] = None,
    prompt: Optional[str] = None,
    lora: Optional[str] = None,
    lora_strength: Optional[float] = None,
    seed: Optional[int] = None,
    backend: Optional[str] = None,
    params: Optional[dict] = None,
    batch_id: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> str:
    """Persist a durable jobs row, seed live Redis state, push onto the queue.

    Returns the new ``job_id``. The order matters for crash-safety: the durable
    row is written first (so a job is never on the queue without a record), then
    the live ``queued`` state, then the LPUSH onto ``QUEUE_KEY``. Raises
    :class:`QueueFull` (before any write) when the queue is at capacity, so the
    caller can return 429 without leaving a half-enqueued job.

    ``batch_id`` (lb-y9c) links this job to a batch group when it was fanned out
    from a batch submission, so aggregate progress can be read straight off the
    jobs table; a single-submit job leaves it NULL. The column is added by
    migration 0003 (run on boot), so it is always present here.

    ``parent_id`` (lb-review-regen) links this job to the ``library`` row it was
    regenerated FROM, so the render worker can copy the link onto the new library
    row and extend the version chain. A first-time render leaves it NULL. The
    column is added by migration 0005 (run on boot), so it is always present.

    The render worker (lb-ocx) BRPOPs ``QUEUE_KEY``, renders, and writes
    progress/terminal states back via :func:`write_job_state`. The LoRA is NOT
    chosen here — the worker resolves + allowlist-validates it from
    (subject, image_type) via app.workflow at render time (the SFW guard).
    """
    # Capacity check first — fail closed before persisting anything.
    depth = queue_depth()
    if depth >= MAX_DEPTH:
        raise QueueFull(depth, MAX_DEPTH)

    job_id = new_id()
    params_json = json.dumps(params) if params is not None else None
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs
                (id, kind, status, subject, area, image_type, video_type,
                 title, prompt, lora, lora_strength, seed, backend, params_json,
                 batch_id, parent_id)
            VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, kind, subject, area, image_type, video_type, title, prompt,
             lora, lora_strength, seed, backend, params_json, batch_id, parent_id),
        )

    # Live state (SSE reads this immediately on connect = RESUME).
    initial = {"job_id": job_id, "status": "queued"}
    try:
        r = get_redis()
        r.set(_state_key(job_id), json.dumps(initial), ex=JOB_STATE_TTL_S)
        r.lpush(QUEUE_KEY, job_id)
    except Exception:
        # The durable row exists; surface the failure so the caller can 503.
        log.exception("enqueue_job: redis push failed for %s", job_id)
        raise
    log.info("enqueued render job %s kind=%s subject=%s/%s", job_id, kind, subject, area)
    return job_id


def list_jobs(*, status: Optional[str] = None, limit: int = 100) -> list[dict]:
    """The most-recent durable ``jobs`` rows, newest first — the Kö management feed.

    Backs the owner-only Kö (queue) management view (lb-07y): the per-job table it
    needs to cancel/retry against. Reads SQLite (the authoritative "did this job
    exist / what was it" store), then overlays the LIVE Redis status so a row that
    just transitioned (queued→running→done) reflects reality before the durable
    mirror catches up.

    ``status`` optionally filters to one lifecycle state (queued / running / done /
    failed / cancelled) — the view typically asks for the actionable
    (queued, running, failed) rows. ``limit`` is clamped 1..500.
    """
    limit = max(1, min(int(limit), 500))
    where = ""
    params: list[Any] = []
    if status:
        where = "WHERE status = ?"
        params.append(status)
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, kind, status, subject, area, image_type, video_type, "
            "title, backend, error, library_id, progress_step, progress_total, "
            "queued_at, started_at, finished_at "
            f"FROM jobs {where} ORDER BY queued_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        job = dict(row)
        # Overlay the live Redis state (fresher than the durable mirror) so the UI
        # never shows a stale 'queued' for a job already running. Best-effort: if
        # Redis is down the durable row stands.
        live = None
        try:
            raw = get_redis().get(_state_key(job["id"]))
            if raw is not None:
                live = json.loads(raw)
        except Exception:  # noqa: BLE001 — overlay is advisory
            live = None
        if live:
            if live.get("status"):
                job["status"] = live["status"]
            for k in ("progress_step", "progress_total", "error", "library_id"):
                if live.get(k) is not None:
                    job[k] = live[k]
    # filter AGAIN on the overlaid status when a status filter was asked, so a row
    # whose live state diverged from the DB doesn't leak into the wrong bucket.
        if status and job["status"] != status:
            continue
        out.append(job)
    return out


def is_cancelled(job_id: str) -> bool:
    """True if the job's live state is ``cancelled`` — the worker's cancel check.

    The render worker calls this right after BRPOP (before it starts a render) so
    a job cancelled while it sat in the queue — or in the BRPOP race window — is
    skipped instead of rendered. Best-effort: on a Redis error it returns False so
    a transient blip never silently drops a legitimate job.
    """
    try:
        raw = get_redis().get(_state_key(job_id))
    except Exception:  # noqa: BLE001 — fail open: render rather than drop
        return False
    if raw is None:
        return False
    try:
        return json.loads(raw).get("status") == "cancelled"
    except Exception:  # noqa: BLE001
        return False


def cancel_job(job_id: str) -> dict:
    """Cancel a queued or running job. Returns the new state dict.

    Idempotent on a queued/running job; raises :class:`UnknownJob` for an unknown
    id and :class:`JobNotActionable` for a job already in a terminal state
    (done/failed/cancelled — re-cancelling cancelled is a no-op success, not an
    error).

    Mechanics — no orphaned jobs left behind:
      1. Mark the live Redis state ``cancelled`` + mirror the durable row. The
         worker checks :func:`is_cancelled` after BRPOP, so a job popped after this
         write is skipped without rendering.
      2. LREM the id off the queue list (count=0 → all copies) so an unclaimed job
         never even reaches a worker. A running job is already off the list; the
         state flag above is what stops it (cooperative cancel at the next safe
         point — the worker re-checks before the GPU submit).
    """
    job = get_job_row(job_id)
    if job is None:
        raise UnknownJob(job_id)
    # Resolve the effective current status (live Redis overlay wins).
    state = read_job_state(job_id) or {}
    status = state.get("status") or job["status"]
    if status == "cancelled":
        return {"job_id": job_id, "status": "cancelled"}  # idempotent no-op
    if status in ("done", "failed"):
        raise JobNotActionable(job_id, status, "cancel")

    cancelled = {"job_id": job_id, "status": "cancelled"}
    write_job_state(job_id, cancelled)  # mirrors to the durable row too
    # Drop every copy of the id from the pending queue list. Best-effort: a
    # running job isn't on the list (LREM is a 0 no-op), the state flag handles it.
    try:
        get_redis().lrem(QUEUE_KEY, 0, job_id)
    except Exception as e:  # noqa: BLE001 — the cancelled state already stands
        log.warning("cancel_job: LREM failed for %s: %s", job_id, e)
    log.info("cancelled render job %s (was %s)", job_id, status)
    return cancelled


def retry_job(job_id: str) -> str:
    """Re-queue a failed or cancelled job under the SAME id. Returns the job id.

    Raises :class:`UnknownJob` for an unknown id and :class:`JobNotActionable`
    unless the job is in a terminal-but-not-done state (failed / cancelled —
    re-running a ``done`` job would duplicate a good artefact; a queued/running job
    is already in flight). Reuses the durable row's render parameters (no second
    enqueue path): resets the lifecycle columns, seeds a fresh ``queued`` live
    state, and LPUSHes the id back onto the queue so the worker re-drives it.
    """
    job = get_job_row(job_id)
    if job is None:
        raise UnknownJob(job_id)
    state = read_job_state(job_id) or {}
    status = state.get("status") or job["status"]
    if status not in ("failed", "cancelled"):
        raise JobNotActionable(job_id, status, "retry")

    # Reset the durable lifecycle columns on the existing row (REUSE its render
    # params — same id, same subject/area/prompt/seed/lora) so retry is a true
    # re-drive, not a second job record.
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'queued', error = NULL, library_id = NULL, "
            "progress_step = NULL, progress_total = NULL, started_at = NULL, "
            "finished_at = NULL WHERE id = ?",
            (job_id,),
        )
    initial = {"job_id": job_id, "status": "queued"}
    r = get_redis()
    r.set(_state_key(job_id), json.dumps(initial), ex=JOB_STATE_TTL_S)
    r.lpush(QUEUE_KEY, job_id)
    log.info("retried render job %s (was %s)", job_id, status)
    return job_id


def reconcile_stuck_jobs() -> list[str]:
    """Boot-time reconcile of orphaned ``running`` jobs (lb-qtr, partial).

    A job in ``running`` was mid-render in a worker process that has since died
    (a crash / restart). The render is in-process + queue-driven, so nothing will
    ever re-drive it — it would otherwise hang forever as "running" in the UI/SSE.
    On boot we mark every such job ``failed`` with a clear reason and stamp
    ``finished_at``, so the durable record (and the SSE fallback that reads it)
    reflects reality instead of a phantom in-flight render.

    Deliberately conservative — it touches ONLY ``running`` jobs:
      * ``queued`` jobs are left alone: a fresh worker can still BRPOP and run
        them (they are not owned by a dead process), so failing them would lose
        legitimately-pending work.
      * terminal jobs (done/failed/cancelled) are already settled.

    Idempotent: a second boot finds no ``running`` rows (the first run failed
    them) and is a no-op. Returns the ids reconciled this call. Best-effort — a
    DB error is logged, never raised, so a reconcile hiccup can't block boot.
    """
    reason = "reconciled on boot: worker process restarted while rendering"
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status = 'running'"
            ).fetchall()
            stuck = [r["id"] for r in rows]
            if stuck:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', "
                    "error = COALESCE(error, ?), "
                    "finished_at = COALESCE(finished_at, ?) "
                    "WHERE status = 'running'",
                    (reason, now_iso()),
                )
    except Exception as e:  # noqa: BLE001 — reconcile must never block boot
        log.warning("reconcile_stuck_jobs failed: %s", e)
        return []

    # Reflect the terminal state into the live Redis store too, so a tab that
    # reconnects to the SSE sees 'failed' rather than a stale 'running' blob.
    for job_id in stuck:
        try:
            r = get_redis()
            r.set(_state_key(job_id),
                  json.dumps({"job_id": job_id, "status": "failed", "error": reason}),
                  ex=JOB_STATE_TTL_S)
        except Exception:  # noqa: BLE001 — Redis mirror is best-effort
            pass
    if stuck:
        log.info("reconcile_stuck_jobs: marked %d orphaned running job(s) failed",
                 len(stuck))
    return stuck
