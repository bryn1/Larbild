"""Batch render submission + aggregate progress API (lb-y9c, P5.2).

The batch layer on top of the P1 single-submit bridge: upload a CSV / ``.txt``
table (or post a JSON row list) and fan it out into N individual queued jobs in
one request, then watch the group's aggregate progress. Three endpoints:

  * ``POST /api/jobs/batch``            — parse + validate the spec, enqueue one
    job per valid row through the existing bridge, return ``202 {batch_id,
    job_ids, rejected, ...}``. Invalid rows are reported (line + reason), never
    enqueued; partial enqueue failures don't abort the batch.
  * ``GET  /api/jobs/batch/<id>``       — the batch's aggregate progress: per-
    status counts rolled up from the jobs table, plus the job ids + rejected
    rows. 404 if the batch id is unknown.
  * ``GET  /api/jobs/batch/<id>/events`` — Server-Sent Events: emit aggregate
    progress on connect, then on every change until every job is terminal, then
    close. Lets the Skapa-batch view show one live progress lane.

The parse + per-row taxonomy validation (lb-zht) and the fan-out + aggregate
(this file's :mod:`app.batch` backend) live in ``app.batch``; this blueprint is
the thin HTTP layer. The **LoRA is never accepted** from any row — the worker
resolves it server-side (the SFW guard), exactly as ``POST /api/jobs``.

Accepted request bodies:
  * ``text/csv`` / ``text/plain`` raw body — the CSV/txt spec verbatim.
  * ``multipart/form-data`` with a ``file`` field — an uploaded ``.csv`` / ``.txt``.
  * ``application/json`` — either a list of row objects ``[{subject,area,…}, …]``
    or ``{"rows": [...], "title": "..."}``. Rows are serialised to CSV and run
    through the same parser, so JSON and file uploads validate identically.

SFW-only: HTTP plumbing over the parse/fan-out backend; no restricted import,
model, or reference. No GPU here — fan-out only enqueues; the render is the
worker's deferred job.

Env overrides (SSE lifecycle, mirrors the per-job SSE):
  LARBILD_BATCH_SSE_MAX_CONN          default 10
  LARBILD_BATCH_SSE_IDLE_TIMEOUT_S    default 3600 (1h — a batch can be long)
  LARBILD_BATCH_SSE_POLL_S            default 1.0
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request, stream_with_context

from .. import batch as batch_mod

batch_bp = Blueprint("batch", __name__)

SSE_MAX_CONN = int(os.environ.get("LARBILD_BATCH_SSE_MAX_CONN", "10"))
SSE_IDLE_TIMEOUT_S = float(os.environ.get("LARBILD_BATCH_SSE_IDLE_TIMEOUT_S", str(60 * 60)))
SSE_POLL_S = float(os.environ.get("LARBILD_BATCH_SSE_POLL_S", "1.0"))
_sse_count_lock = threading.Lock()
_sse_active_count = 0

# CSV header used when serialising a JSON row list, so JSON and file uploads run
# through the identical parser/validator. Extra row keys are appended as columns.
_JSON_BASE_COLUMNS = ["kind", "subject", "area", "imageType", "videoType",
                      "prompt", "title", "seed", "count"]


def _rows_to_csv(rows: list[dict]) -> str:
    """Serialise a JSON list of row dicts to CSV text for the shared parser.

    The column set is the known base columns plus any extra keys present on the
    rows (so a forward-compatible field still reaches the parser, where unknown
    columns are ignored and a ``lora`` column is dropped). Camel/snake key
    variants are preserved verbatim — the parser normalises them.
    """
    extra: list[str] = []
    for row in rows:
        for k in row:
            if k not in _JSON_BASE_COLUMNS and k not in extra:
                extra.append(k)
    columns = _JSON_BASE_COLUMNS + extra
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue()


def _extract_spec_text() -> tuple[Optional[str], Optional[str], Optional[str], Optional[tuple[dict, int]]]:
    """Pull the batch spec text + source + optional title from the request.

    Returns ``(text, source, title, None)`` on success or ``(None, None, None,
    err)`` where ``err`` is a ``(payload, status)`` for an unusable request.
    Resolution order: a multipart ``file`` upload, then a JSON body (list or
    ``{rows}``), then a raw text body.
    """
    # 1. multipart file upload (a .csv / .txt the teacher picked)
    if request.files:
        upload = request.files.get("file") or next(iter(request.files.values()))
        if upload is not None and upload.filename:
            try:
                text = upload.read().decode("utf-8")
            except (UnicodeDecodeError, OSError) as e:
                return None, None, None, ({"error": f"could not read upload: {e}"}, 400)
            source = "txt" if upload.filename.lower().endswith(".txt") else "csv"
            title = (request.form.get("title") or "").strip() or upload.filename
            return text, source, title, None

    # 2. JSON body — a row list or {"rows": [...], "title": "..."}
    if request.is_json:
        data = request.get_json(silent=True)
        rows: Optional[list] = None
        title: Optional[str] = None
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            maybe = data.get("rows")
            if isinstance(maybe, list):
                rows = maybe
            title = (data.get("title") or "").strip() or None
        if rows is None:
            return None, None, None, (
                {"error": "JSON body must be a list of rows or {\"rows\": [...]}"}, 400)
        if not all(isinstance(r, dict) for r in rows):
            return None, None, None, ({"error": "every JSON row must be an object"}, 400)
        return _rows_to_csv(rows), "json", title, None

    # 3. raw text body (text/csv or text/plain)
    raw = request.get_data(as_text=True)
    if raw and raw.strip():
        ctype = (request.mimetype or "").lower()
        source = "txt" if "plain" in ctype else "csv"
        title = (request.args.get("title") or "").strip() or None
        return raw, source, title, None

    return None, None, None, ({"error": "empty request: upload a CSV/txt file, "
                               "post JSON rows, or a raw CSV body"}, 400)


@batch_bp.post("/api/jobs/batch")
def submit_batch():
    """Parse + validate a batch spec and fan it out into N queued jobs.

    202 (Accepted) — the valid rows are queued (not done); the client then polls
    ``GET /api/jobs/batch/<id>`` (or tails its SSE) and may also tail each
    ``/api/jobs/<job_id>/events``. 400 when the request carries no usable spec or
    the parse rejected *every* row (nothing to enqueue) — the body still details
    the per-row reasons so the teacher can fix the file.

    The response always reports the rejected rows (with line numbers + reasons)
    alongside the enqueued ``job_ids`` and the ``batch_id`` — a partially valid
    batch enqueues its good rows and tells you about the bad ones.
    """
    text, source, title, err = _extract_spec_text()
    if err is not None:
        payload, status = err
        return jsonify(payload), status

    spec = batch_mod.parse_batch(text)
    if spec.valid_count == 0:
        # Nothing enqueueable — surface every rejection so the file can be fixed.
        return jsonify({
            "error": "no valid rows to enqueue",
            "valid_count": 0,
            "rejected_count": spec.rejected_count,
            "rejected": [e.as_dict() for e in spec.errors],
        }), 400

    result = batch_mod.fan_out(spec, source=source, title=title)
    return jsonify({
        "batch_id": result["batch_id"],
        "job_ids": result["job_ids"],
        "enqueued_count": result["enqueued_count"],
        "rejected_count": result["rejected_count"],
        "rejected": result["rejected"],
        "enqueue_failures": result["enqueue_failures"],
    }), 202


@batch_bp.get("/api/jobs/batch/<batch_id>")
def get_batch(batch_id: str):
    """Aggregate progress for a batch (per-status counts), or 404 if unknown."""
    progress = batch_mod.batch_progress(batch_id)
    if progress is None:
        return jsonify({"error": "unknown batch", "batch_id": batch_id}), 404
    return jsonify(progress), 200


@batch_bp.get("/api/jobs/batch/<batch_id>/events")
def batch_events(batch_id: str):
    """Server-Sent Events stream of a batch's aggregate progress.

    Emits the current aggregate on connect (RESUME), then re-reads on an interval
    and emits whenever the rolled-up counts change, until every job is terminal
    (``done`` true), then sends ``event: end`` and closes. A per-stream idle
    timeout + a process-wide connection cap (503 when reached) bound dead tabs; a
    try/finally decrements the counter even on mid-yield disconnect. 404 if the
    batch is unknown at connect. Globals are read off this module so tests can
    monkeypatch ``app.routes.batch.SSE_POLL_S = 0.0``.
    """
    initial = batch_mod.batch_progress(batch_id)
    if initial is None:
        return jsonify({"error": "unknown batch", "batch_id": batch_id}), 404

    global _sse_active_count
    with _sse_count_lock:
        if _sse_active_count >= SSE_MAX_CONN:
            return jsonify({"error": "too many active batch event streams",
                            "max_connections": SSE_MAX_CONN}), 503
        _sse_active_count += 1

    deadline = time.monotonic() + SSE_IDLE_TIMEOUT_S

    @stream_with_context
    def generate():
        global _sse_active_count
        try:
            last = json.dumps(initial, sort_keys=True)
            yield f"data: {last}\n\n"  # RESUME: current aggregate on connect
            if initial.get("done"):
                yield "event: end\ndata: {}\n\n"
                return
            while time.monotonic() < deadline:
                time.sleep(SSE_POLL_S)
                state = batch_mod.batch_progress(batch_id)
                if state is None:
                    continue
                cur = json.dumps(state, sort_keys=True)
                if cur != last:
                    last = cur
                    yield f"data: {cur}\n\n"
                if state.get("done"):
                    yield "event: end\ndata: {}\n\n"
                    return
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
