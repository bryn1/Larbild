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
  guard, app.workflow). A crafted ``lora`` column can therefore never inject an
  off-allowlist (NSFW) LoRA.
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
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import taxonomy

__all__ = [
    "MAX_ROWS",
    "MAX_ROW_COUNT",
    "RowError",
    "BatchSpec",
    "parse_batch",
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
        # csv handles quoted commas; fall back to whitespace split for plain txt.
        cells = next(csv.reader(io.StringIO(physical)))
        cells = [c.strip() for c in cells]
        if len(cells) == 1 and cells[0]:
            cells = stripped.split()
        row: dict[str, Any] = {}
        for key, value in zip(_POSITIONAL, cells):
            row[key] = value
        # a 5th positional cell is the optional count
        if len(cells) > len(_POSITIONAL):
            row["count"] = cells[len(_POSITIONAL)]
        # surface a too-few-columns row as a normaliser-validated reject rather
        # than silently dropping it: pass through; _validate_row reports the gap.
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
