"""CSV / txt batch parse → validated render specs (lb-zht, P5.1).

The teacher fills the library fast by uploading a batch spec — a CSV (or a
whitespace/header-less ``.txt`` table) of render requests, one per row — instead
of clicking the Skapa funnel N times. This module turns that text into a list of
**validated, normalised job specs** ready to fan out through the existing P1
bridge (:func:`app.jobs.enqueue_job`), and a parallel list of per-row rejections
with line numbers + reasons so nothing garbage is ever enqueued.

It is the parse-and-validate half of the batch layer (the fan-out + HTTP route is
lb-y9c); kept Flask-free and side-effect-free so it can be unit-tested directly
and reused by the route without a request context.

Contract (the SFW + taxonomy guard, identical to ``POST /api/jobs``):

* Each row carries ``subject``, ``area``, ``imageType`` (or ``videoType`` for a
  film row), a ``prompt``, and optional ``kind`` / ``title`` / ``seed`` / ``count``.
* Every taxonomy key is validated against :mod:`app.taxonomy` (lb-582) — the same
  single source of truth the DB documents its keys against — so a typo can't
  enqueue an un-sortable job. ``area`` is subject-scoped.
* **The LoRA is never read from the row.** A ``lora`` / ``loraName`` column is
  dropped exactly like the single-submit path: the render worker resolves +
  allowlist-validates the LoRA from (subject, imageType) at render time (the SFW
  guard, app.workflow). A crafted ``lora`` column can therefore never inject a
  non-allowlisted (unsafe) LoRA.
* ``count`` (optional, default 1) repeats a valid row N times — one queued job
  each — capped at :data:`MAX_ROW_COUNT` so a single line can't flood the queue.
* A row that fails any check is rejected with its 1-based line number and a clear
  reason; valid rows still parse (a batch is best-effort, never all-or-nothing).

Two input shapes are accepted:

* **CSV** with a header row naming the columns (``subject,area,imageType,prompt``
  in any order; extra columns ignored, ``lora`` dropped). This is the normal
  path — robust to column reordering and quoted prompts with commas.
* **Header-less positional** rows (CSV or whitespace-delimited ``.txt``) in the
  fixed order ``subject  area  imageType  prompt`` (+ optional ``count`` as a
  trailing integer). A leading ``#`` comments a line out; blank lines are skipped.

SFW-only: pure text parsing + taxonomy validation; no restricted import, model,
or reference. No GPU, no Redis, no DB — fan-out (lb-y9c) does the enqueue.

Env overrides:
  LARBILD_BATCH_MAX_ROWS        default 500  (reject a spec with more rows than this)
  LARBILD_BATCH_MAX_ROW_COUNT   default 50   (cap a single row's ``count`` expansion)
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import taxonomy
from .db import connect, new_id
from .jobs import QueueFull, enqueue_job

log = logging.getLogger("larbild.batch")

__all__ = [
    "MAX_ROWS",
    "MAX_ROW_COUNT",
    "RowError",
    "BatchSpec",
    "parse_batch",
    "fan_out",
    "batch_progress",
    "get_batch_row",
]

# A batch with more than this many rows is rejected whole (a paste-bomb guard).
MAX_ROWS = int(os.environ.get("LARBILD_BATCH_MAX_ROWS", "500"))
# A single row's optional ``count`` is clamped to this (one line can't flood).
MAX_ROW_COUNT = int(os.environ.get("LARBILD_BATCH_MAX_ROW_COUNT", "50"))

# Positional column order for header-less rows. CSV with a header may reorder /
# omit-extra freely; these names are also the accepted header keys.
_POSITIONAL = ("subject", "area", "imageType", "prompt")

# Header keys we recognise (case-insensitive). image_type/video_type snake_case
# aliases mirror the single-submit route's tolerance. ``lora`` is recognised only
# so it can be *dropped* loudly-safely (never read).
_KNOWN_KEYS = {
    "kind",
    "subject",
    "area",
    "imagetype", "image_type",
    "videotype", "video_type",
    "prompt",
    "title",
    "seed",
    "count",
}
_DROPPED_KEYS = {"lora", "loraname"}


@dataclass
class RowError:
    """One rejected row: its 1-based source line, the reason, and the raw cells."""

    line: int
    error: str
    raw: Any = None

    def as_dict(self) -> dict:
        return {"line": self.line, "error": self.error, "raw": self.raw}


@dataclass
class BatchSpec:
    """The parsed batch: validated job specs + per-row rejections.

    ``jobs`` are normalised dicts shaped exactly for ``enqueue_job(**spec)``
    (minus the LoRA, which the worker resolves). Each carries a ``_line`` key
    (the 1-based source line) for provenance/debugging; the fan-out strips it
    before calling enqueue. ``errors`` holds the rejected rows.
    """

    jobs: list[dict] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)

    @property
    def valid_count(self) -> int:
        return len(self.jobs)

    @property
    def rejected_count(self) -> int:
        return len(self.errors)

    def as_dict(self) -> dict:
        return {
            "valid_count": self.valid_count,
            "rejected_count": self.rejected_count,
            "jobs": self.jobs,
            "errors": [e.as_dict() for e in self.errors],
        }


def _looks_like_header(first_row: list[str]) -> bool:
    """True if the first CSV row names columns rather than holding data.

    A header is identified by the presence of the column *names* ``subject`` and
    ``prompt`` (both always required) among its cells — tolerating extra/unknown
    columns (e.g. a ``note`` column) and any column order. This is robust where a
    "all cells must be known keys" rule wasn't: an unknown extra header column no
    longer defeats detection and mis-routes the file into positional parsing.

    A header-less positional file never trips this: its first cell is a subject
    *slug* (``fysik``) and a prompt *value*, not the literal strings ``subject`` /
    ``prompt``, so the file correctly falls through to positional parsing.
    """
    cells = {c.strip().lower() for c in first_row if c.strip()}
    if not cells:
        return False
    return "subject" in cells and "prompt" in cells


def _normalise_keys(row: dict) -> dict:
    """Lowercase header keys, fold snake/camel aliases, drop the LoRA columns.

    Returns a dict keyed by the canonical names used downstream
    (``imageType`` / ``videoType`` camelCase to match the JSON API).
    """
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k is None:
            continue
        key = k.strip().lower()
        if key in _DROPPED_KEYS:
            continue  # SFW guard: never carry a client LoRA past the door
        if key in ("imagetype", "image_type"):
            out["imageType"] = v
        elif key in ("videotype", "video_type"):
            out["videoType"] = v
        elif key in _KNOWN_KEYS:
            out[key] = v
        # unknown columns are ignored (forward-compatible with extra columns)
    return out


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _validate_row(raw: dict, line: int) -> tuple[Optional[dict], Optional[RowError]]:
    """Validate + normalise one row dict. Returns (spec, None) or (None, RowError).

    Mirrors ``app.routes.jobs._validate_submission`` exactly so a batch row and a
    single POST are accepted/rejected identically: kind∈{bild,film}, a valid
    subject-scoped (subject, area), the kind-appropriate image/video type, a
    non-empty prompt, an optional integer seed, and an optional 1..MAX_ROW_COUNT
    ``count``. The LoRA is already gone (dropped in :func:`_normalise_keys`).
    """
    kind = _clean_str(raw.get("kind")) or "bild"
    if kind not in ("bild", "film"):
        return None, RowError(line, f"invalid kind {kind!r} (expected bild|film)", raw)

    subject = _clean_str(raw.get("subject"))
    area = _clean_str(raw.get("area"))
    if not subject or not area:
        return None, RowError(line, "subject and area are required", raw)
    if not taxonomy.is_valid_subject(subject):
        return None, RowError(line, f"unknown subject {subject!r}", raw)
    if not taxonomy.is_valid_area(subject, area):
        return None, RowError(line, f"unknown area {area!r} for subject {subject!r}", raw)

    prompt = _clean_str(raw.get("prompt"))
    if not prompt:
        return None, RowError(line, "prompt is required", raw)

    image_type = _clean_str(raw.get("imageType")) or None
    video_type = _clean_str(raw.get("videoType")) or None
    if kind == "bild":
        if not image_type:
            return None, RowError(line, "imageType is required for kind=bild", raw)
        if not taxonomy.is_valid_image_type(image_type):
            return None, RowError(line, f"unknown imageType {image_type!r}", raw)
        video_type = None
    else:  # film
        if not video_type:
            return None, RowError(line, "videoType is required for kind=film", raw)
        if not taxonomy.is_valid_video_type(video_type):
            return None, RowError(line, f"unknown videoType {video_type!r}", raw)
        image_type = None

    seed_raw = raw.get("seed")
    seed: Optional[int] = None
    if seed_raw is not None and _clean_str(seed_raw) != "":
        try:
            seed = int(_clean_str(seed_raw))
        except (TypeError, ValueError):
            return None, RowError(line, f"seed must be an integer, got {seed_raw!r}", raw)

    count_raw = raw.get("count")
    count = 1
    if count_raw is not None and _clean_str(count_raw) != "":
        try:
            count = int(_clean_str(count_raw))
        except (TypeError, ValueError):
            return None, RowError(line, f"count must be an integer, got {count_raw!r}", raw)
        if count < 1:
            return None, RowError(line, f"count must be >= 1, got {count}", raw)
        if count > MAX_ROW_COUNT:
            return None, RowError(
                line, f"count {count} exceeds per-row max {MAX_ROW_COUNT}", raw
            )

    spec = {
        "kind": kind,
        "subject": subject,
        "area": area,
        "image_type": image_type,
        "video_type": video_type,
        "title": _clean_str(raw.get("title")) or None,
        "prompt": prompt,
        "seed": seed,
        "count": count,
        # provenance for the fan-out's per-job error reporting; stripped before
        # enqueue (enqueue_job has no _line / count kwarg).
        "_line": line,
        # the submitted row, sans LoRA, for the durable params blob (matches the
        # single-submit route storing the request body for provenance).
        "params": {k: v for k, v in raw.items()},
    }
    return spec, None


def _rows_from_csv_with_header(text: str) -> Iterable[tuple[int, dict]]:
    """Yield (line_no, normalised_row_dict) for a header CSV.

    ``DictReader`` maps each data row onto the header; we re-key via
    :func:`_normalise_keys`. The line number counts the header as line 1, so the
    first data row is line 2 — matching what the user sees in an editor.
    """
    reader = csv.DictReader(io.StringIO(text))
    # DictReader consumes the header; enumerate data rows starting at line 2.
    for offset, row in enumerate(reader, start=2):
        yield offset, _normalise_keys(row)


def _rows_from_positional(text: str) -> Iterable[tuple[int, dict]]:
    """Yield (line_no, row_dict) for a header-less positional file.

    Each physical line is parsed first as CSV (so quoted prompts with commas
    work), and if it is a single field with no comma, split on whitespace (the
    ``.txt`` table form). Blank lines and ``#`` comments are skipped but still
    consume a line number so reported lines match the source file. Columns map
    positionally to ``subject area imageType prompt`` then an optional trailing
    integer ``count``.
    """
    for line_no, physical in enumerate(text.splitlines(), start=1):
        stripped = physical.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # A comma in the line → CSV cells (so quoted commas + an explicit count
        # cell work). No comma → whitespace table: the first 3 tokens are
        # subject/area/imageType and the REST is the (multi-word) prompt — a
        # natural ``.txt`` row like ``fysik ljud foto a transverse wave``.
        if "," in physical:
            cells = [c.strip() for c in next(csv.reader(io.StringIO(physical)))]
            row: dict[str, Any] = {}
            for key, value in zip(_POSITIONAL, cells):
                row[key] = value
            if len(cells) > len(_POSITIONAL):  # a 5th cell is the optional count
                row["count"] = cells[len(_POSITIONAL)]
        else:
            tokens = stripped.split()
            row = {}
            lead = _POSITIONAL[:-1]  # subject, area, imageType
            for key, value in zip(lead, tokens):
                row[key] = value
            if len(tokens) > len(lead):
                row["prompt"] = " ".join(tokens[len(lead):])  # remainder = prompt
        # A short row is passed through so _validate_row reports the missing
        # field (e.g. "prompt is required") rather than being silently dropped.
        yield line_no, _normalise_keys(row)


def parse_batch(text: str) -> BatchSpec:
    """Parse a CSV/txt batch spec into validated job specs + per-row rejections.

    The single entry point. Auto-detects a header CSV vs a header-less positional
    file from the first non-empty row, parses every row, validates each against
    the taxonomy SoT + the SFW LoRA rule (LoRA columns dropped), and returns a
    :class:`BatchSpec`. A malformed CSV (e.g. an unbalanced quote that the csv
    module raises on) yields an all-rejected spec with a single explanatory error
    rather than throwing — the route can always answer 200/400 with detail.

    The whole spec is rejected (one synthetic error, no jobs) only when it is
    empty or exceeds :data:`MAX_ROWS`; otherwise parsing is per-row best-effort.
    """
    if text is None or not text.strip():
        return BatchSpec(errors=[RowError(0, "empty batch: no rows to parse", None)])

    # Peek the first non-empty physical line to pick the parse mode.
    try:
        first_physical = next(
            ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")
        )
        first_cells = next(csv.reader(io.StringIO(first_physical)))
        use_header = _looks_like_header(first_cells)
    except StopIteration:
        return BatchSpec(errors=[RowError(0, "empty batch: no rows to parse", None)])

    spec = BatchSpec()
    try:
        row_iter = (
            _rows_from_csv_with_header(text) if use_header else _rows_from_positional(text)
        )
        rows = list(row_iter)
    except csv.Error as e:
        return BatchSpec(errors=[RowError(0, f"malformed CSV: {e}", None)])

    if not rows:
        return BatchSpec(errors=[RowError(0, "no data rows found", None)])
    if len(rows) > MAX_ROWS:
        return BatchSpec(
            errors=[RowError(0, f"too many rows: {len(rows)} exceeds max {MAX_ROWS}", None)]
        )

    for line_no, raw in rows:
        job_spec, err = _validate_row(raw, line_no)
        if err is not None:
            spec.errors.append(err)
        else:
            spec.jobs.append(job_spec)
    return spec


# ── fan-out: parsed spec → N queued jobs under one batch group (lb-y9c) ──────

# Statuses the aggregate progress rolls up. 'queued'/'running'/'done'/'failed'/
# 'cancelled' are the jobs-table lifecycle (0001_initial CHECK). 'enqueue_failed'
# is a synthetic bucket for rows that validated but could not be pushed onto the
# queue (e.g. the queue filled mid-batch) — they never became a jobs row.
_PROGRESS_STATUSES = ("queued", "running", "done", "failed", "cancelled")


def _enqueue_one(spec: dict, batch_id: str) -> str:
    """Enqueue a single validated spec under ``batch_id``; return its job_id.

    Strips the parse-only keys (``_line`` provenance, ``count`` expansion factor)
    the bridge's :func:`app.jobs.enqueue_job` does not accept, then delegates to
    it unchanged — so each fanned-out job follows the exact same
    validate-already-done → queue → worker → auto-sort path as a single submit.
    The LoRA is not passed (the worker resolves it); ``batch_id`` is threaded so
    the durable jobs row records its group.
    """
    enqueue_kwargs = {
        k: v for k, v in spec.items() if k not in ("_line", "count")
    }
    return enqueue_job(batch_id=batch_id, **enqueue_kwargs)


def fan_out(spec: BatchSpec, *, source: Optional[str] = None,
            title: Optional[str] = None) -> dict:
    """Fan a parsed :class:`BatchSpec` out into individual queued jobs.

    Creates one ``batches`` group row (the returned ``batch_id``), then enqueues
    each valid spec through the standard bridge — expanding a spec's ``count`` into
    that many jobs. Returns::

        {"batch_id": str,
         "job_ids": [str, ...],          # successfully enqueued, in row order
         "rejected": [{line, error, raw}, ...],  # parse-time rejections
         "enqueue_failures": [{line, error}, ...],  # validated-but-couldn't-queue
         "valid_count": int, "rejected_count": int, "enqueued_count": int}

    Partial failures never abort the batch (acceptance): if a single
    :func:`enqueue_job` raises (``QueueFull`` when the queue fills mid-batch, or a
    Redis error), that unit is recorded in ``enqueue_failures`` and the loop
    continues with the rest. The batch row's ``rejected_count`` folds in both
    parse rejections and enqueue failures so a later GET reports the full picture.

    SFW note: every job goes through :func:`app.jobs.enqueue_job`, which never
    accepts a LoRA — the worker resolves it server-side. The batch layer adds no
    new path to a model/LoRA.
    """
    batch_id = new_id()
    job_ids: list[str] = []
    enqueue_failures: list[dict] = []
    queue_full = False  # latched once the queue fills — stop pushing further rows

    # The batch group row MUST exist before any job references it: jobs.batch_id
    # is a FK to batches.id (PRAGMA foreign_keys = ON), so enqueuing first would
    # fail the constraint. Insert with the parse-time counts now; the final
    # enqueued/rejected tallies + errors are written back after the fan-out loop.
    parse_rejections = [e.as_dict() for e in spec.errors]
    total_rows = spec.valid_count + spec.rejected_count
    _insert_batch_row(
        batch_id=batch_id,
        source=source,
        title=title,
        total_rows=total_rows,
        valid_count=spec.valid_count,        # provisional; corrected post-loop
        rejected_count=spec.rejected_count,  # provisional; corrected post-loop
        errors=parse_rejections,
    )

    for job_spec in spec.jobs:
        if queue_full:
            # Queue is full; remaining rows can't enqueue. Record them as failures
            # so the response accounts for every valid row, but don't keep trying.
            enqueue_failures.append({"line": job_spec.get("_line"),
                                     "error": "render queue full: batch truncated"})
            continue
        count = int(job_spec.get("count", 1) or 1)
        line = job_spec.get("_line")
        for _ in range(count):
            try:
                job_ids.append(_enqueue_one(job_spec, batch_id))
            except QueueFull as e:
                # Queue filled mid-batch: latch + stop. Every further push would
                # also fail; keep what we enqueued and report the rest.
                enqueue_failures.append({"line": line, "error": str(e)})
                log.warning("fan_out: queue full at line %s, batch %s partial",
                            line, batch_id)
                queue_full = True
                break
            except Exception as e:  # noqa: BLE001 — one bad enqueue can't sink the batch
                enqueue_failures.append({"line": line, "error": f"enqueue failed: {e}"})
                log.warning("fan_out: enqueue failed for line %s: %s", line, e)

    # Write back the final tallies: actual enqueued count + all rejections
    # (parse-time + enqueue-time) so a later GET reports the full picture.
    all_rejections = parse_rejections + enqueue_failures
    _update_batch_counts(
        batch_id=batch_id,
        valid_count=len(job_ids),
        rejected_count=len(all_rejections),
        errors=all_rejections,
    )

    return {
        "batch_id": batch_id,
        "job_ids": job_ids,
        "rejected": parse_rejections,
        "enqueue_failures": enqueue_failures,
        "valid_count": spec.valid_count,
        "rejected_count": spec.rejected_count,
        "enqueued_count": len(job_ids),
    }


def _insert_batch_row(*, batch_id: str, source: Optional[str], title: Optional[str],
                      total_rows: int, valid_count: int, rejected_count: int,
                      errors: list[dict]) -> None:
    """Persist the ``batches`` group record (one row per submitted batch)."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO batches
                (id, source, title, total_rows, valid_count, rejected_count, errors_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (batch_id, source, title, total_rows, valid_count, rejected_count,
             json.dumps(errors) if errors else None),
        )


def _update_batch_counts(*, batch_id: str, valid_count: int, rejected_count: int,
                         errors: list[dict]) -> None:
    """Write the final enqueued/rejected tallies + errors onto the batch row.

    ``valid_count`` here is the number of jobs that actually enqueued (so it
    reflects a count-expanded or partially-failed fan-out), distinct from the
    provisional parse-time count written at insert.
    """
    with connect() as conn:
        conn.execute(
            "UPDATE batches SET valid_count = ?, rejected_count = ?, errors_json = ? "
            "WHERE id = ?",
            (valid_count, rejected_count, json.dumps(errors) if errors else None,
             batch_id),
        )


def get_batch_row(batch_id: str) -> Optional[dict]:
    """The durable ``batches`` row as a dict (errors_json decoded), or None."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    if out.get("errors_json"):
        try:
            out["errors"] = json.loads(out["errors_json"])
        except (TypeError, ValueError):
            out["errors"] = []
    else:
        out["errors"] = []
    out.pop("errors_json", None)
    return out


def batch_progress(batch_id: str) -> Optional[dict]:
    """Aggregate per-job state for a batch, or None if the batch is unknown.

    Reuses the jobs table as the single source of truth: groups the batch's jobs
    by ``status`` and counts each, so progress reflects the live lifecycle the
    render worker drives (the same statuses the SSE streams) without a second
    bookkeeping store. Returns::

        {"batch_id": str, "total": int, "done": bool,
         "counts": {"queued": int, "running": int, "done": int,
                    "failed": int, "cancelled": int},
         "job_ids": [str, ...],
         "rejected_count": int, "enqueued_count": int,
         "source": str | None, "title": str | None, "created_at": str,
         "errors": [...]}

    ``done`` is True once no job is still ``queued`` or ``running`` (every job
    reached a terminal status) — the signal the UI uses to stop polling. A batch
    that enqueued zero jobs (all rows rejected) is ``done`` immediately.
    """
    record = get_batch_row(batch_id)
    if record is None:
        return None

    counts = {s: 0 for s in _PROGRESS_STATUSES}
    job_ids: list[str] = []
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, status FROM jobs WHERE batch_id = ? ORDER BY queued_at",
            (batch_id,),
        ).fetchall()
    for r in rows:
        job_ids.append(r["id"])
        status = r["status"]
        if status in counts:
            counts[status] += 1
    total = len(job_ids)
    in_flight = counts["queued"] + counts["running"]

    return {
        "batch_id": batch_id,
        "total": total,
        "done": in_flight == 0,
        "counts": counts,
        "job_ids": job_ids,
        "rejected_count": record.get("rejected_count", 0),
        "enqueued_count": record.get("valid_count", 0),
        "source": record.get("source"),
        "title": record.get("title"),
        "created_at": record.get("created_at"),
        "errors": record.get("errors", []),
    }
