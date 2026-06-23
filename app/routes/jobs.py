"""Render-job submission + progress API (lb-3gi, P1.2).

Three endpoints, the funnel's render contract:

  * ``POST /api/jobs``            — validate a render request against the
    taxonomy SoT, enqueue it, return ``202 {job_id, status: "queued"}``.
  * ``GET  /api/jobs/<id>``       — the current job state (404 if unknown).
  * ``GET  /api/jobs/<id>/events`` — Server-Sent Events: emit the current state
    on connect (RESUME), then one event per state change until a terminal status,
    then close. Reconnecting resumes the current state — so a dropped tab catches
    up without re-submitting.

The taxonomy keys (``subject`` / ``area`` / ``imageType`` | ``videoType``) are
validated here against :mod:`app.taxonomy` (lb-582) — the same SoT the DB
documents its string keys against — so a typo can't enqueue an un-sortable job.
The **LoRA is not accepted from the client**: the render worker resolves +
allowlist-validates it from (subject, imageType) at render time (the SFW guard,
app.workflow). A ``lora`` field in the body is ignored.

SFW-only: request validation + queue plumbing + SSE fan-out; no restricted
import, model, or reference.

Env overrides (SSE lifecycle, mirrors the GPU SSE):
  LARBILD_JOB_SSE_MAX_CONN          default 20
  LARBILD_JOB_SSE_IDLE_TIMEOUT_S    default 900 (15 min — a render won't outlast)
  LARBILD_JOB_SSE_POLL_S            default 0.5
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request, stream_with_context

from .. import taxonomy
from ..jobs import (
    JobNotActionable,
    QueueFull,
    UnknownJob,
    cancel_job,
    enqueue_job,
    list_jobs,
    read_job_state,
    retry_job,
)

jobs_bp = Blueprint("jobs", __name__)

_TERMINAL = ("done", "failed", "cancelled")

SSE_MAX_CONN = int(os.environ.get("LARBILD_JOB_SSE_MAX_CONN", "20"))
SSE_IDLE_TIMEOUT_S = float(os.environ.get("LARBILD_JOB_SSE_IDLE_TIMEOUT_S", str(15 * 60)))
SSE_POLL_S = float(os.environ.get("LARBILD_JOB_SSE_POLL_S", "0.5"))
_sse_count_lock = threading.Lock()
_sse_active_count = 0


def _body() -> dict:
    """Pull the JSON/form body as a dict (empty dict if absent/non-object)."""
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
    if request.form:
        return dict(request.form)
    return {}


def _validate_submission(data: dict) -> tuple[Optional[dict], Optional[tuple[dict, int]]]:
    """Validate + normalise a render request. Returns (clean, None) or (None, err).

    ``kind`` defaults to ``bild``. ``subject`` + ``area`` are required and must be
    a valid (subject-scoped) pair. For ``bild`` an ``imageType`` is required and
    validated; for ``film`` a ``videoType`` is required and validated. The
    ``prompt`` is required and non-empty. Any client-supplied ``lora`` is ignored
    (backend-authoritative). Unknown keys are dropped.
    """
    kind = (data.get("kind") or "bild").strip()
    if kind not in ("bild", "film"):
        return None, ({"error": f"invalid kind: {kind!r}", "valid": ["bild", "film"]}, 400)

    subject = (data.get("subject") or "").strip()
    area = (data.get("area") or "").strip()
    if not subject or not area:
        return None, ({"error": "subject and area are required",
                       "valid_subjects": list(taxonomy.subject_slugs())}, 400)
    if not taxonomy.is_valid_subject(subject):
        return None, ({"error": f"unknown subject: {subject!r}",
                       "valid_subjects": list(taxonomy.subject_slugs())}, 400)
    if not taxonomy.is_valid_area(subject, area):
        return None, ({"error": f"unknown area {area!r} for subject {subject!r}",
                       "valid_areas": list(taxonomy.area_slugs(subject))}, 400)

    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return None, ({"error": "prompt is required"}, 400)

    image_type = (data.get("imageType") or data.get("image_type") or "").strip() or None
    video_type = (data.get("videoType") or data.get("video_type") or "").strip() or None
    if kind == "bild":
        if not image_type:
            return None, ({"error": "imageType is required for kind=bild",
                           "valid_image_types": list(taxonomy.image_type_slugs())}, 400)
        if not taxonomy.is_valid_image_type(image_type):
            return None, ({"error": f"unknown imageType: {image_type!r}",
                           "valid_image_types": list(taxonomy.image_type_slugs())}, 400)
        video_type = None
    else:  # film
        if not video_type:
            return None, ({"error": "videoType is required for kind=film",
                           "valid_video_types": list(taxonomy.video_type_slugs())}, 400)
        if not taxonomy.is_valid_video_type(video_type):
            return None, ({"error": f"unknown videoType: {video_type!r}",
                           "valid_video_types": list(taxonomy.video_type_slugs())}, 400)
        image_type = None

    # seed: optional int; reject a non-int rather than silently coercing.
    seed = data.get("seed")
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            return None, ({"error": "seed must be an integer"}, 400)

    clean = {
        "kind": kind,
        "subject": subject,
        "area": area,
        "image_type": image_type,
        "video_type": video_type,
        "title": (data.get("title") or "").strip() or None,
        "prompt": prompt,
        "seed": seed,
        # The full submitted params blob is stored for provenance; the LoRA is
        # NOT read from it (the worker resolves it server-side).
        "params": {k: v for k, v in data.items() if k != "lora" and k != "loraName"},
    }
    return clean, None


@jobs_bp.post("/api/jobs")
def submit_job():
    """Validate + enqueue a render job. Returns 202 {job_id, status}.

    202 (Accepted) — the work is queued, not done; the client then tails
    ``/api/jobs/<id>/events``. 400 on a bad selection; 429 when the queue is full.
    """
    body = _body()
    clean, err = _validate_submission(body)
    if err is not None:
        payload, status = err
        return jsonify(payload), status
    # lb-9d5 (A1): honour the "Antal" count — the worker renders one image per job,
    # so N images means N jobs. The frontend tails the first job's SSE for progress;
    # the rest render and land in the library. The client sends count under the
    # nested `params` blob (buildJobPayload); re-clamp 1..8 here so a forged request
    # can't queue a runaway batch. count==1 (default) == the prior single-job path.
    try:
        count = int((body.get("params") or {}).get("count", 1))
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, 8))
    # The N renders are independent jobs (no batches-row grouping — jobs.batch_id
    # is a FK to the bulk-CSV `batches` table, which this count fan-out doesn't use).
    job_ids: list[str] = []
    try:
        for _ in range(count):
            job_ids.append(enqueue_job(**clean))
    except QueueFull as e:
        if not job_ids:
            return jsonify({"error": "render queue is full, try again shortly",
                            "depth": e.depth, "limit": e.limit}), 429
        # Partial success: some jobs queued before the queue filled — return them
        # rather than silently dropping the whole request.
    return jsonify({
        "job_id": job_ids[0],
        "job_ids": job_ids,
        "count": len(job_ids),
        "status": "queued",
    }), 202


_LISTABLE_STATUS = ("queued", "running", "done", "failed", "cancelled")


@jobs_bp.get("/api/jobs")
def list_jobs_route():
    """The recent jobs feed for the owner-only Kö management view (lb-07y).

    Query: ``?status=`` (one of queued/running/done/failed/cancelled — omit for
    all), ``?limit=`` (1..500, default 100). Returns ``{jobs: [...]}`` newest
    first, each row carrying the live-overlaid status so the UI shows current
    state. Owner-only by construction: this blueprint is not registered on the
    public read-only instance.
    """
    status = (request.args.get("status") or "").strip() or None
    if status is not None and status not in _LISTABLE_STATUS:
        return jsonify({"error": f"invalid status: {status!r}",
                        "valid": list(_LISTABLE_STATUS)}), 400
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify({"jobs": list_jobs(status=status, limit=limit)}), 200


@jobs_bp.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    """The current state of a job, or 404 if unknown to both Redis and SQLite."""
    state = read_job_state(job_id)
    if state is None:
        return jsonify({"error": "unknown job", "job_id": job_id}), 404
    return jsonify(state), 200


@jobs_bp.post("/api/jobs/<job_id>/cancel")
def cancel_job_route(job_id: str):
    """Cancel a queued or running job (owner-only Kö management, lb-07y).

    200 with the new ``cancelled`` state; 404 unknown id; 409 if the job is
    already done/failed (not actionable). Re-cancelling a cancelled job is an
    idempotent 200. The queue LREM + cooperative-cancel flag mean no orphaned job
    is left behind.
    """
    try:
        state = cancel_job(job_id)
    except UnknownJob:
        return jsonify({"error": "unknown job", "job_id": job_id}), 404
    except JobNotActionable as e:
        return jsonify({"error": str(e), "job_id": job_id, "status": e.status}), 409
    return jsonify(state), 200


@jobs_bp.post("/api/jobs/<job_id>/retry")
def retry_job_route(job_id: str):
    """Re-queue a failed or cancelled job under the same id (owner-only, lb-07y).

    202 ``{job_id, status: "queued"}`` (the work is re-queued, not done); 404
    unknown id; 409 if the job is not in a retryable (failed/cancelled) state.
    Reuses the durable row's render params — a true re-drive, not a new job.
    """
    try:
        retry_job(job_id)
    except UnknownJob:
        return jsonify({"error": "unknown job", "job_id": job_id}), 404
    except JobNotActionable as e:
        return jsonify({"error": str(e), "job_id": job_id, "status": e.status}), 409
    return jsonify({"job_id": job_id, "status": "queued"}), 202


@jobs_bp.get("/api/jobs/<job_id>/events")
def job_events(job_id: str):
    """Server-Sent Events stream of a job's progress.

    On connect, emit the current state immediately (RESUME — a reconnecting tab
    sees where the job is now, not nothing). Then poll the live state and emit on
    every change, until a terminal status (done/failed/cancelled), then send a
    final ``event: end`` and close. A per-stream idle timeout + a process-wide
    connection cap (503 when reached) keep dead tabs from pinning slots; a
    try/finally decrements the counter even on mid-yield disconnect.

    404 if the job is unknown at connect time. The counter/timeout are read from
    this module's globals so tests can monkeypatch
    ``app.routes.jobs.SSE_IDLE_TIMEOUT_S = 0.0``.
    """
    initial = read_job_state(job_id)
    if initial is None:
        return jsonify({"error": "unknown job", "job_id": job_id}), 404

    global _sse_active_count
    with _sse_count_lock:
        if _sse_active_count >= SSE_MAX_CONN:
            return jsonify({"error": "too many active job event streams",
                            "max_connections": SSE_MAX_CONN}), 503
        _sse_active_count += 1

    deadline = time.monotonic() + SSE_IDLE_TIMEOUT_S

    @stream_with_context
    def generate():
        global _sse_active_count
        try:
            last = json.dumps(initial, sort_keys=True)
            yield f"data: {last}\n\n"  # RESUME: current state on connect
            if initial.get("status") in _TERMINAL:
                yield "event: end\ndata: {}\n\n"
                return
            while time.monotonic() < deadline:
                time.sleep(SSE_POLL_S)
                state = read_job_state(job_id)
                if state is None:
                    continue
                cur = json.dumps(state, sort_keys=True)
                if cur != last:
                    last = cur
                    yield f"data: {cur}\n\n"
                if state.get("status") in _TERMINAL:
                    yield "event: end\ndata: {}\n\n"
                    return
            # idle close sentinel (distinguishes a server timeout from a blip)
            yield "event: timeout\ndata: {}\n\n"
        finally:
            with _sse_count_lock:
                _sse_active_count -= 1

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
