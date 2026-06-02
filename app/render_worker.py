"""Render worker — the P1 'it actually works' glue (lb-ocx, P1.3).

Ties the bridge together end to end: a queued job →
:func:`app.workflow.assemble_txt2img` (LoRA resolved + allowlist-validated, the
SFW guard) → the SHARED :func:`app.gpu_lock.gpu_lock` + :func:`app.vram_pause`
(so a render coexists with imagegen + chat on the one VM100 3090 without OOM,
BUILD_PLAN R4) → :class:`app.comfyui.ComfyUIClient` submit + poll → download the
PNG into the isolated output dir → write a ``library`` row **auto-sorted by
subject / area / imageType** (lb-b1r) and link it back onto the ``jobs`` row →
the ``done`` state for the SSE stream.

────────────────────────────────────────────────────────────────────────────
NO LIVE GPU RENDER IN THIS BEAD (CRITICAL).
A large imagegen sweep is on BOTH GPUs (VM100 + vm153). This module is built +
unit/integration-tested with a **mocked ComfyUI** (an injected client) and
**stubbed** gpu_lock / vram_pause — no live GPU or network call is made. The
"first real render on vm100" validation is **DEFERRED** until the GPU is free;
see :data:`DEFERRED_LIVE_RENDER` and the bead notes. The shared gpu_lock is
exactly what will let larbild + imagegen coexist safely when that live run
happens.
────────────────────────────────────────────────────────────────────────────

Dependency injection: every side-effecting collaborator (the ComfyUI client
factory, the gpu_lock + vram_pause context managers) is a parameter with a
production default, so tests pass mocks/stubs and exercise the full orchestration
without touching a GPU. The production loop :func:`run_worker_loop` is the real
entry point (a BRPOP loop); it is intentionally NOT run during this bead.

SFW-only: orchestration over the SFW workflow + SFW library; no restricted
import, model, or reference.

Env overrides:
  LARBILD_RENDER_BACKEND   default 'vm100' — the render target (telemetry
                           registry key; P4 makes this an `auto` choice).
  LARBILD_RENDER_POLL_S    default 1.0 — ComfyUI history poll cadence.
  LARBILD_RENDER_TIMEOUT_S default 600.0 — give up on a single render after this.
  LARBILD_DIFFUSION_IMAGE_TYPES  default 'foto,illustration,mikroskop,tvarsnitt'
                           — the imageTypes whose thin prompt is replaced by a
                           concept-aligned educational prompt (lb-edu-prompt).
  LARBILD_FLUX_ROUTE_ENABLE      default 0 — when a FLUX render lane exists,
                           flip on to route illustration/mikroskop/tvärsnitt to
                           it (dormant/advisory until a FLUX target is wired).
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from flask import current_app

from . import edu_prompt as _edu_prompt
from . import gpu_lock as _gpu_lock
from . import vram_pause as _vram_pause
from . import workflow as _workflow
from .comfyui import ComfyResult, ComfyUIClient, default_comfyui_url, extract_output_images
from .db import connect, new_id, now_iso
from .gpu_telemetry import RENDER_TARGETS
from .jobs import QUEUE_KEY, get_job_row, get_redis, write_job_state

# redis exception types for the BRPOP idle/connectivity handling (hq-wc6i).
# Imported defensively: redis is a hard dep for the worker, but the module is
# also imported in tests/dev boxes where it may be absent — fall back to the
# builtin TimeoutError + a never-matching sentinel so import never fails and the
# loop's except-clauses still parse. (The real socket read timeout surfaces as
# redis.exceptions.TimeoutError, which subclasses RedisError, NOT builtin
# TimeoutError — so the fallback alias matters only when redis is missing.)
try:  # pragma: no cover - exercised implicitly wherever redis is installed
    from redis.exceptions import (
        ConnectionError as RedisConnectionError,
        TimeoutError as RedisTimeoutError,
    )
except ImportError:  # pragma: no cover - redis absent (dev/CI without the dep)
    RedisTimeoutError = TimeoutError  # builtin

    class RedisConnectionError(Exception):  # type: ignore[no-redef]
        """Sentinel when redis isn't importable — never raised by a fake."""

log = logging.getLogger("larbild.render_worker")

__all__ = [
    "render_job",
    "run_worker_loop",
    "build_specific_prompt",
    "flux_route_for",
    "DIFFUSION_IMAGE_TYPES",
    "DEFERRED_LIVE_RENDER",
    "RenderError",
]

# Flag the single thing this bead does NOT do — a real GPU render. Kept as a
# module constant so it's discoverable + greppable; the live validation is a
# follow-up for when the imagegen sweep frees the GPU.
DEFERRED_LIVE_RENDER = (
    "First real render on vm100 is DEFERRED (imagegen sweep on both GPUs). The "
    "worker + auto-sort are built + tested with a mocked ComfyUI; run the live "
    "validation when the GPU is free — the shared gpu_lock will coordinate it."
)

DEFAULT_BACKEND = os.environ.get("LARBILD_RENDER_BACKEND", "vm100")
POLL_S = float(os.environ.get("LARBILD_RENDER_POLL_S", "1.0"))
RENDER_TIMEOUT_S = float(os.environ.get("LARBILD_RENDER_TIMEOUT_S", "600.0"))

# The DIFFUSION image types — the ones this render path produces with a
# diffusion model (SDXL today). diagram / tidslinje / karta now go to the
# PROCEDURAL engine (clean vector renders); foto / illustration / mikroskop /
# tvärsnitt stay diffusion and are the ones the concept-aligned prompt engine
# (lb-edu-prompt) improves. A job whose image_type is in this set has its thin
# prompt replaced by a SPECIFIC, begrepp-grounded educational prompt before the
# graph is assembled. (A type NOT in this set still renders via the same path,
# just without the diffusion-specific prompt rewrite — defensive default.)
DIFFUSION_IMAGE_TYPES = frozenset(
    (os.environ.get("LARBILD_DIFFUSION_IMAGE_TYPES")
     or "foto,illustration,mikroskop,tvarsnitt").replace(" ", "").split(",")
)

# FLUX-routing flag (P-follow-up). illustration / mikroskop / tvärsnitt render
# noticeably cleaner on a FLUX model than on SDXL, so the design wants them on a
# FLUX lane WHEN one exists. No FLUX render target is wired yet (the registry is
# SDXL-only — vm100 3090 + vm153 B70), so this is a DORMANT routing hook: it is
# OFF by default and, even when enabled, only annotates the job's intended lane
# (meta['flux_route']) — it does NOT change the backend until a FLUX target is
# registered. Flip LARBILD_FLUX_ROUTE_ENABLE=1 once a FLUX lane lands (tracked as
# a follow-up bead) to make these types prefer it. Until then: SDXL + the right
# per-cell LoRA (the taxonomy matrix), exactly as before.
FLUX_ROUTE_ENABLE = (os.environ.get("LARBILD_FLUX_ROUTE_ENABLE", "0").strip().lower()
                     in {"1", "true", "yes", "on"})
FLUX_ROUTE_IMAGE_TYPES = frozenset(
    (os.environ.get("LARBILD_FLUX_ROUTE_IMAGE_TYPES")
     or "illustration,mikroskop,tvarsnitt").replace(" ", "").split(",")
)
# The FLUX render target key (in gpu_telemetry.RENDER_TARGETS) once it exists.
# Absent today → no actual reroute; the flag stays advisory.
FLUX_ROUTE_TARGET = os.environ.get("LARBILD_FLUX_ROUTE_TARGET", "flux")


class RenderError(RuntimeError):
    """A render could not be completed (submission, timeout, or no output)."""


# Type aliases for the injected collaborators.
ComfyFactory = Callable[[], ComfyUIClient]
LockCM = Callable[..., Any]   # context manager factory -> yields bool
PauseCM = Callable[..., Any]  # context manager factory -> yields free MiB


@contextmanager
def _noop_pause(reason: str = "", **_: Any) -> Iterator[int]:
    """A vram_pause stand-in for environments without the VM100 SSH path.

    Yields a large free-MiB so the worker proceeds. NOT used in production (the
    real :func:`app.vram_pause.vram_pause` is the default); it exists so a render
    can be driven in a test/dev box that can't SSH to VM100.
    """
    yield 1 << 20


def _default_comfy_factory() -> ComfyUIClient:
    target = RENDER_TARGETS.get(DEFAULT_BACKEND, {})
    return ComfyUIClient(target.get("http") or default_comfyui_url())


def _backend_label() -> str:
    return RENDER_TARGETS.get(DEFAULT_BACKEND, {}).get("label", DEFAULT_BACKEND)


def _concept_from_job(job: dict) -> str:
    """The begrepp (concept) a render should depict.

    Resolution order, most-specific first:
      1. an explicit ``begrepp`` in the submitted params blob (a funnel/batch can
         send the concept name distinct from a free-form prompt),
      2. the job ``title`` (the funnel's concept label),
      3. the job ``prompt`` (the operator's typed text — usually just the concept).
    The first non-empty wins. This is the seed the edu-prompt engine grounds the
    image in (its förklaring + vardagligt_exempel are looked up from it).
    """
    params = job.get("params_json")
    if params:
        try:
            blob = json.loads(params)
            if isinstance(blob, dict):
                cand = (blob.get("begrepp") or blob.get("concept") or "").strip()
                if cand:
                    return cand
        except (ValueError, TypeError):  # pragma: no cover - malformed params
            pass
    title = (job.get("title") or "").strip()
    if title:
        return title
    return (job.get("prompt") or "").strip()


def _params_field(job: dict, *keys: str) -> Optional[str]:
    """Pull the first present non-empty value for *keys* from params_json."""
    params = job.get("params_json")
    if not params:
        return None
    try:
        blob = json.loads(params)
    except (ValueError, TypeError):  # pragma: no cover - malformed params
        return None
    if not isinstance(blob, dict):
        return None
    for k in keys:
        v = blob.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def build_specific_prompt(job: dict) -> tuple[str, Optional[str], dict]:
    """Concept-aligned positive prompt (+ SFW negative) for a diffusion job.

    For a DIFFUSION image type (:data:`DIFFUSION_IMAGE_TYPES`), the thin job
    prompt is replaced by a SPECIFIC, begrepp-grounded educational prompt from
    :func:`app.edu_prompt.build_edu_prompt` — driven by the 14B composer with a
    rich template fallback, matched to the imageType style. The SFW negative
    scaffold is used so the deformed/inappropriate failure modes are pushed away.

    For a non-diffusion type (defensive — those normally never reach this still
    worker), the original prompt + the default negative are returned unchanged.

    Returns ``(positive, negative, edu_meta)`` where ``edu_meta`` records what
    happened (source = llm|fallback, the concept, the flux-route intent) for the
    library row / telemetry. ``negative`` is None to mean "let assemble_txt2img
    use its default".
    """
    image_type = job.get("image_type")
    original = job.get("prompt") or ""

    if not image_type or image_type not in DIFFUSION_IMAGE_TYPES:
        return original, None, {"edu_source": "passthrough", "image_type": image_type}

    concept = _concept_from_job(job)
    if not concept:
        # No concept to ground on — keep the operator's prompt, still apply the
        # SFW negative so a bare submit is at least scaffolded.
        return original, _edu_prompt.SFW_NEGATIVE, {
            "edu_source": "passthrough_no_concept", "image_type": image_type,
        }

    built = _edu_prompt.build_edu_prompt(
        subject=job["subject"],
        image_type=image_type,
        begrepp=concept,
        area=job.get("area"),
        # A funnel/batch may carry the förklaring/exempel explicitly; else the
        # engine back-fills them from the bundled begrepp dataset.
        forklaring=_params_field(job, "forklaring", "förklaring"),
        vardagligt_exempel=_params_field(job, "vardagligt_exempel", "exempel"),
    )
    edu_meta = {
        "edu_source": built["source"],          # "llm" or "fallback"
        "edu_error": built.get("error"),
        "concept": concept,
        "image_type": image_type,
        "original_prompt": original,
        "flux_route": flux_route_for(image_type),
    }
    log.info(
        "render prompt for %s: concept=%r source=%s flux_route=%s",
        job.get("id"), concept, built["source"], edu_meta["flux_route"],
    )
    return built["prompt"], _edu_prompt.SFW_NEGATIVE, edu_meta


def flux_route_for(image_type: Optional[str]) -> Optional[str]:
    """The FLUX render-target this image type SHOULD use, or None.

    The FLUX-routing flag (a follow-up): illustration / mikroskop / tvärsnitt
    render cleaner on FLUX. Returns the configured FLUX target key when the flag
    is on AND a FLUX target is actually registered AND this type is FLUX-eligible;
    otherwise None (SDXL stays the lane). Today no FLUX target is registered, so
    this returns None and is a dormant, ready-to-flip hook — flipping
    LARBILD_FLUX_ROUTE_ENABLE without a registered target still safely no-ops.
    """
    if not FLUX_ROUTE_ENABLE or not image_type:
        return None
    if image_type not in FLUX_ROUTE_IMAGE_TYPES:
        return None
    if FLUX_ROUTE_TARGET not in RENDER_TARGETS:
        # Flag is on but no FLUX lane is wired yet — stay on SDXL, don't pretend.
        return None
    return FLUX_ROUTE_TARGET


def _storage_dir() -> Path:
    """The isolated output dir (STORAGE_DIR/output). Created if missing."""
    base = Path(current_app.config["STORAGE_DIR"]) / "output"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _relative_output_path(meta: dict, filename: str) -> Path:
    """Auto-sort target path RELATIVE to STORAGE_DIR/output.

    ``<subject>/<area>/<imageType>/<filename>`` — the same taxonomy keys the
    ``library`` row carries, so the on-disk layout mirrors the Bibliotek tree.
    """
    subject = meta["subject"]
    image_type = meta.get("image_type") or "övrigt"
    # `area` rides on the job row; meta doesn't carry it, so the caller passes
    # the full sort key. Filled in by render_job from the job row.
    area = meta.get("area") or "övrigt"
    return Path(subject) / area / image_type / filename


def _download_first_image(
    client: ComfyUIClient, results: list[ComfyResult], dest: Path
) -> None:
    """Download the first ComfyUI output image to ``dest`` (mkdir parents)."""
    first = results[0]
    data = client.view(first.filename, first.subfolder, first.type)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _autosort_to_library(job: dict, meta: dict, rel_path: Path) -> str:
    """Insert the auto-sorted ``library`` row; return its id (lb-b1r).

    The row carries the taxonomy keys from the job (subject/area/imageType) so
    GET /api/library counts + sorts it into the right Bibliotek node, plus the
    render provenance (prompt, LoRA, seed, backend, dims). ``file_path`` is the
    STORAGE_DIR-relative path under ``output/``. Written BEFORE the job is marked
    done + linked, so the jobs.library_id FK is satisfied.

    A regenerate carries ``jobs.parent_id`` — the library row this render was made
    FROM — which is copied onto the new row's ``parent_id`` so the version chain
    is preserved across the async queue→worker hop (lb-review-regen). A first-time
    render leaves it NULL (an original / chain root). ``job.get`` is used so an
    older job row (pre-0005, no parent_id column) still inserts cleanly.
    """
    lib_id = new_id()
    lora = meta.get("lora") or {}
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO library
                (id, kind, subject, area, image_type, video_type, title, job_id,
                 file_path, prompt, lora, lora_strength, seed, backend,
                 width, height, workflow_json, parent_id, created_at)
            VALUES (?, 'bild', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                lib_id,
                job["subject"], job["area"], job.get("image_type"),
                job.get("title") or meta.get("prompt", "")[:80] or "Bild",
                job["id"],
                str(Path("output") / rel_path),
                meta.get("prompt"),
                lora.get("name"),
                lora.get("strength"),
                meta.get("seed"),
                _backend_label(),
                meta.get("width"), meta.get("height"),
                job.get("parent_id"),
                now_iso(),
            ),
        )
    log.info("auto-sorted render → library %s (%s/%s/%s)", lib_id,
             job["subject"], job["area"], job.get("image_type"))
    return lib_id


def render_job(
    job_id: str,
    *,
    comfy_factory: Optional[ComfyFactory] = None,
    gpu_lock_cm: Optional[LockCM] = None,
    vram_pause_cm: Optional[PauseCM] = None,
    poll_s: float = POLL_S,
    timeout_s: float = RENDER_TIMEOUT_S,
) -> str:
    """Render one queued job end-to-end and auto-sort the result into the library.

    Steps: load the durable job row → assemble the SFW txt2img graph (LoRA
    resolved + allowlist-validated) → acquire the SHARED gpu_lock + vram_pause →
    submit to ComfyUI + poll until done → download the PNG into the isolated
    output dir, auto-sorted by subject/area/imageType → write the ``library``
    row + link it onto the job → mark the job ``done``. Progress + terminal
    states are published via :func:`app.jobs.write_job_state` for the SSE stream.

    Collaborators are injected (``comfy_factory`` / ``gpu_lock_cm`` /
    ``vram_pause_cm``) with production defaults; tests pass mocks so NO live GPU
    is touched. Returns the new ``library`` row id. On failure, writes a
    ``failed`` state (with the error) and re-raises :class:`RenderError`.
    """
    comfy_factory = comfy_factory or _default_comfy_factory
    gpu_lock_cm = gpu_lock_cm or _gpu_lock.gpu_lock
    vram_pause_cm = vram_pause_cm or _vram_pause.vram_pause

    job = get_job_row(job_id)
    if job is None:
        raise RenderError(f"job {job_id} not found")
    if job["kind"] != "bild":
        # P1 renders stills only; film is P6 (lb-4vw). Fail loudly rather than
        # silently dropping a film job.
        raise RenderError(f"render_job handles kind='bild' only, got {job['kind']!r}")

    try:
        write_job_state(job_id, {"job_id": job_id, "status": "running",
                                 "progress_step": 0, "progress_total": 1})

        # Replace the thin job prompt with a SPECIFIC, begrepp-grounded
        # educational prompt for a diffusion image type (lb-edu-prompt). This is
        # the specificity fix: the positive now depicts the concept's actual
        # content (förklaring + everyday example), matched to the imageType
        # style, instead of a generic "pedagogiskt <type> av <concept>". For a
        # non-diffusion type it returns the original prompt unchanged.
        positive, negative, edu_meta = build_specific_prompt(job)

        # Build the graph. resolve_and_validate_lora is the SFW choke point: an
        # off-allowlist LoRA raises here, before anything reaches the GPU. The
        # LoRA is STILL resolved server-side from (subject, imageType) — the
        # concept-aligned prompt changes only the positive text, never the LoRA
        # allowlist or the negative scaffold.
        workflow, meta = _workflow.assemble_txt2img(
            subject=job["subject"],
            image_type=job["image_type"],
            prompt=positive or job["prompt"] or "",
            neg_prompt=negative,  # SFW negative for diffusion; None → default
            seed=job["seed"] if job["seed"] is not None else _default_seed(),
        )
        meta["area"] = job["area"]  # carry the sort key for the output path
        meta["edu"] = edu_meta      # provenance: how the prompt was built

        # Acquire the SHARED VM100 lock, then pause VRAM, then submit. The lock
        # makes larbild wait while imagegen holds the 3090 (and vice-versa).
        with gpu_lock_cm(reason=f"larbild render {job_id}") as held:
            if not held:
                log.warning("render %s proceeding without cross-host lock", job_id)
            with vram_pause_cm(reason=f"larbild render {job_id}"):
                results = _submit_and_wait(
                    comfy_factory(), workflow, job_id, poll_s, timeout_s,
                )

        # Download + auto-sort.
        client = comfy_factory()
        rel = _relative_output_path(meta, results[0].filename)
        _download_first_image(client, results, _storage_dir() / rel)
        lib_id = _autosort_to_library(job, meta, rel)

        write_job_state(job_id, {
            "job_id": job_id, "status": "done", "library_id": lib_id,
            "width": meta.get("width"), "height": meta.get("height"),
        })
        return lib_id
    except _workflow.LoraNotAllowedError as e:
        # An off-allowlist LoRA — the SFW guard fired. Record + re-raise.
        write_job_state(job_id, {"job_id": job_id, "status": "failed",
                                 "error": f"SFW guard: {e}"})
        raise RenderError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        write_job_state(job_id, {"job_id": job_id, "status": "failed", "error": str(e)})
        raise RenderError(str(e)) from e


def _submit_and_wait(
    client: ComfyUIClient, workflow: dict, job_id: str,
    poll_s: float, timeout_s: float,
) -> list[ComfyResult]:
    """Queue the graph, poll /history until outputs appear, return them.

    Raises :class:`RenderError` on timeout or an empty output set.
    """
    prompt_id = client.queue_prompt(workflow)
    log.info("render %s submitted to ComfyUI prompt_id=%s", job_id, prompt_id)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        entry = client.history(prompt_id)
        if entry is not None:
            results = extract_output_images(entry)
            if results:
                return results
            raise RenderError(f"render {job_id}: ComfyUI finished with no image output")
        write_job_state(job_id, {"job_id": job_id, "status": "running",
                                 "progress_step": 0, "progress_total": 1})
        time.sleep(poll_s)
    raise RenderError(f"render {job_id}: timed out after {timeout_s}s")


def _default_seed() -> int:
    """A fresh random seed when the job didn't pin one."""
    import secrets
    return secrets.randbelow(2**31)


# BRPOP server-side block timeout. The Redis connection (gpu_lock.make_redis_client)
# sets socket_timeout=5; a BRPOP that blocks for the SAME duration races that read
# timeout — the socket can raise redis.exceptions.TimeoutError at ~5s instead of
# BRPOP returning None when idle (hq-wc6i). We hold the block strictly UNDER the
# socket timeout for margin, AND treat any read timeout as an idle no-op below, so
# an idle worker can NEVER crash-loop regardless of timing or network jitter.
BRPOP_TIMEOUT_S = int(os.environ.get("LARBILD_BRPOP_TIMEOUT_S", "2"))


def run_worker_loop(stop: Optional[Callable[[], bool]] = None) -> None:
    """Production BRPOP loop: pull job ids off the queue and render them.

    ``stop`` is an optional predicate to break the loop (for a supervised run /
    tests). Each job is rendered under the SHARED gpu_lock so it coexists with
    imagegen on the VM100 3090.

    Idle-resilient (hq-wc6i): when the queue is empty the worker must keep
    looping, not die. Two things can signal "nothing to do":
      * BRPOP returns ``None`` (its block elapsed with an empty list), or
      * the connection's ``socket_timeout`` fires first and BRPOP raises
        :class:`redis.exceptions.TimeoutError` — a read timeout, NOT a render
        failure.
    Both are handled identically: continue the loop. Only a genuine connection
    loss surfaces (logged + a short backoff) so systemd's Restart=on-failure
    stays a real-failure mechanism, not an idle-spin.
    """
    log.info("render worker loop starting; backend=%s. %s",
             DEFAULT_BACKEND, DEFERRED_LIVE_RENDER)
    r = get_redis()
    while stop is None or not stop():
        try:
            popped = r.brpop(QUEUE_KEY, timeout=BRPOP_TIMEOUT_S)
        except RedisTimeoutError:
            # The socket read timeout fired before BRPOP's server-side block
            # returned None — an IDLE tick, not an error. Loop again. (This is
            # the hq-wc6i idle-crash fix: previously this propagated out of
            # main() and systemd crash-looped the worker every ~socket_timeout
            # seconds whenever the queue was empty.)
            continue
        except RedisConnectionError as e:
            # A real connectivity problem (Redis down / network gone). Back off
            # briefly and retry rather than busy-spinning; a persistent outage
            # still surfaces in the log.
            log.warning("render worker: redis connection error, retrying: %s", e)
            time.sleep(min(BRPOP_TIMEOUT_S, 5))
            continue
        if popped is None:
            continue
        _key, job_id = popped
        try:
            render_job(job_id)
        except RenderError as e:
            log.error("render job %s failed: %s", job_id, e)
