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
from pathlib import Path
from typing import Any, Callable, Optional

from flask import current_app

from . import edu_prompt as _edu_prompt
from . import procedural as _procedural
from . import render_lane as _render_lane
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
    "PROCEDURAL_BACKEND",
    "RenderError",
]

# Backend label for a procedural (CPU) render — distinct from the GPU labels so
# the Bibliotek + library row honestly say "this diagram was drawn, not diffused".
PROCEDURAL_BACKEND = "procedural · CPU"

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
RenderCM = Callable[..., Any]  # render-residency context manager factory -> yields held (bool)


def _default_comfy_factory(target_key: Optional[str] = None) -> ComfyUIClient:
    """A ComfyUI client for a render target (default: DEFAULT_BACKEND = vm100).

    ``target_key`` lets the FLUX route point at its registered target; the FLUX
    lane shares vm100:8188 today so this resolves to the same endpoint, but it
    keeps the factory target-aware so a future split host needs no edit here.
    """
    target = RENDER_TARGETS.get(target_key or DEFAULT_BACKEND, {})
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

    # Is this job going to render on the FLUX lane? If so, build the FLUX-tuned
    # positive (scene-led, begrepp NOT named — FLUX renders a leading Swedish
    # concept name as literal text) + the harder FLUX no-text negative. Otherwise
    # the SDXL edu prompt + SFW negative, exactly as before. The route decision is
    # the single source of truth (flux_route_for), so the prompt + the graph
    # always agree on which lane this is.
    flux_route = flux_route_for(image_type)

    # ── PRE-COMPOSED (baked) prompt fast-path (lb batch render-all) ──
    # A large unattended batch (the full begrepp set) pre-generates every
    # diffusion prompt in ONE composer pass up front (the no-VRAM-contention
    # recipe — edu_prompt.batch_compose), then BAKES the final positive onto each
    # enqueued job's params blob as ``precomposed_prompt``. When present, render
    # WITHOUT re-invoking the 14B composer here: the composer service (:8091) can
    # then stay DOWN for the whole render drain, so ComfyUI never swaps VRAM with
    # it 700+ times. We still apply the lane-appropriate SFW negative (never let a
    # baked positive skip the negative scaffold), and record the provenance. The
    # baked text is a finished positive (style + framing + quality already folded
    # in by the batch pass), so it is used verbatim. Absent → the live compose
    # path below runs exactly as before (a UI single-submit is unaffected).
    precomposed = _params_field(job, "precomposed_prompt", "baked_prompt")
    if precomposed:
        negative = _edu_prompt.FLUX_SFW_NEGATIVE if flux_route else _edu_prompt.SFW_NEGATIVE
        edu_meta = {
            "edu_source": "precomposed",
            "concept": _concept_from_job(job),
            "image_type": image_type,
            "original_prompt": original,
            "flux_route": flux_route,
        }
        log.info("render prompt for %s: concept=%r source=precomposed flux_route=%s",
                 job.get("id"), edu_meta["concept"], flux_route)
        return precomposed, negative, edu_meta

    concept = _concept_from_job(job)
    if not concept:
        # No concept to ground on — keep the operator's prompt, still apply the
        # lane-appropriate SFW negative so a bare submit is at least scaffolded.
        neg = _edu_prompt.FLUX_SFW_NEGATIVE if flux_route else _edu_prompt.SFW_NEGATIVE
        return original, neg, {
            "edu_source": "passthrough_no_concept", "image_type": image_type,
            "flux_route": flux_route,
        }

    builder = _edu_prompt.build_flux_edu_prompt if flux_route else _edu_prompt.build_edu_prompt
    built = builder(
        subject=job["subject"],
        image_type=image_type,
        begrepp=concept,
        area=job.get("area"),
        # A funnel/batch may carry the förklaring/exempel explicitly; else the
        # engine back-fills them from the bundled begrepp dataset.
        forklaring=_params_field(job, "forklaring", "förklaring"),
        vardagligt_exempel=_params_field(job, "vardagligt_exempel", "exempel"),
    )
    negative = _edu_prompt.FLUX_SFW_NEGATIVE if flux_route else _edu_prompt.SFW_NEGATIVE
    edu_meta = {
        "edu_source": built["source"],          # "llm" or "fallback"
        "edu_error": built.get("error"),
        "concept": concept,
        "image_type": image_type,
        "original_prompt": original,
        "flux_route": flux_route,
    }
    log.info(
        "render prompt for %s: concept=%r source=%s flux_route=%s",
        job.get("id"), concept, built["source"], edu_meta["flux_route"],
    )
    return built["prompt"], negative, edu_meta


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
    # backend + workflow_json ride on meta so the SAME insert serves both engines:
    # a diffusion render leaves them unset (→ the vm100 label + NULL spec), a
    # procedural render passes "procedural · CPU" + the DiagramSpec JSON. Keeping
    # one insert means the auto-sort / Bibliotek browse is identical for both.
    backend = meta.get("backend") or _backend_label()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO library
                (id, kind, subject, area, image_type, video_type, title, job_id,
                 file_path, prompt, lora, lora_strength, seed, backend,
                 width, height, workflow_json, parent_id, created_at)
            VALUES (?, 'bild', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                backend,
                meta.get("width"), meta.get("height"),
                meta.get("workflow_json"),
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
    render_cm: Optional[RenderCM] = None,
    poll_s: float = POLL_S,
    timeout_s: float = RENDER_TIMEOUT_S,
) -> str:
    """Render one queued job end-to-end and auto-sort the result into the library.

    Steps: load the durable job row → assemble the SFW txt2img graph (LoRA
    resolved + allowlist-validated) → enter render-residency on the shared 3090
    (signal render pressure → wait for ComfyUI → hold the SHARED gpu_lock) →
    submit to ComfyUI + poll until done → download the PNG into the isolated
    output dir, auto-sorted by subject/area/imageType → write the ``library``
    row + link it onto the job → mark the job ``done``. Progress + terminal
    states are published via :func:`app.jobs.write_job_state` for the SSE stream.

    Collaborators are injected (``comfy_factory`` / ``render_cm``) with production
    defaults; tests pass mocks so NO live GPU is touched. Returns the new
    ``library`` row id. On failure, writes a ``failed`` state (with the error)
    and re-raises :class:`RenderError`.
    """
    comfy_factory = comfy_factory or _default_comfy_factory
    render_cm = render_cm or _render_lane.render_residency

    job = get_job_row(job_id)
    if job is None:
        raise RenderError(f"job {job_id} not found")
    if job["kind"] != "bild":
        # P1 renders stills only; film is P6 (lb-4vw). Fail loudly rather than
        # silently dropping a film job.
        raise RenderError(f"render_job handles kind='bild' only, got {job['kind']!r}")

    # ── ROUTE: structured imageTypes go to the PROCEDURAL engine, NOT the GPU ──
    # A schematic diagram / tidslinje / molekyl / reaction is exact structure +
    # real Swedish labels — diffusion garbles both (the owner's report). These
    # render deterministically on CPU (graphviz/matplotlib/RDKit), so they work
    # even while the VM100 3090 is busy with imagegen/chat, and never need the
    # gpu_lock / vram_pause / ComfyUI path below. (MODEL_LIBRARY.md §6 procedural-
    # first.) The PNG still auto-sorts into the same library tree.
    if _procedural.is_structured_image_type(job["image_type"]):
        return _render_procedural_job(job)

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
        #
        # ── FLUX-lane routing ──
        # When flux_route_for says this imageType should render on FLUX (the flag
        # is on AND a 'flux' target is registered AND the type is FLUX-eligible),
        # build the FLUX graph instead of the SDXL one. FLUX follows prompts far
        # better on the abstract / science-illustration concepts SDXL plateaus on.
        # The SAME concept-aligned positive + SFW negative feed both lanes (a fair
        # model-only switch); only the graph + LoRA set differ, and the FLUX path
        # has its OWN SFW allowlist (resolve_and_validate_flux_lora). When the flag
        # is off / no FLUX target, this is None and the SDXL path runs exactly as
        # before — the routing is fully gated by LARBILD_FLUX_ROUTE_ENABLE.
        flux_target = flux_route_for(job["image_type"])
        prompt_text = positive or job["prompt"] or ""
        seed = job["seed"] if job["seed"] is not None else _default_seed()
        if flux_target:
            workflow, meta = _workflow.assemble_flux_txt2img(
                subject=job["subject"],
                image_type=job["image_type"],
                prompt=prompt_text,
                neg_prompt=negative,
                seed=seed,
            )
            # Honest provenance: the library row says this was a FLUX render.
            meta["backend"] = RENDER_TARGETS.get(flux_target, {}).get(
                "label", _backend_label())
        else:
            workflow, meta = _workflow.assemble_txt2img(
                subject=job["subject"],
                image_type=job["image_type"],
                prompt=prompt_text,
                neg_prompt=negative,  # SFW negative for diffusion; None → default
                seed=seed,
            )
        meta["area"] = job["area"]  # carry the sort key for the output path
        meta["edu"] = edu_meta      # provenance: how the prompt was built

        # The ComfyUI client. FLUX shares the vm100:8188 endpoint with the SDXL
        # lane today (one box, one 3090 — only the graph differs), so the default
        # factory already yields the right client and we keep the call zero-arg
        # (the injected test factories are zero-arg). The factory is target-aware
        # for a future split host; the FLUX target it would use is recorded in
        # meta['backend'] above.
        comfy = comfy_factory()

        # Render on the SHARED VM100 3090 via render-residency (hq-jx6g): signal
        # render pressure so the dispatcher (hq-t4y8) evicts any LLM/compose +
        # keeps ComfyUI resident (renders-first), wait for ComfyUI WITHOUT holding
        # the lock, then hold the SHARED gpu_lock (lock:vm100-3090) for the submit
        # so a model swap can't co-reside with the render. Replaces the old
        # self-evicting vram_pause — the dispatcher is now the sole 3090 evictor.
        with render_cm(reason=f"larbild render {job_id}") as held:
            if not held:
                log.warning("render %s proceeding without cross-host lock", job_id)
            results = _submit_and_wait(
                comfy, workflow, job_id, poll_s, timeout_s,
            )

        # Download + auto-sort.
        client = comfy
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


def _concept_spec_from_job(job: dict) -> dict:
    """Pull the begrepp / förklaring / optional diagram spec out of a job row.

    The structured-diagram inputs ride in the job's ``params_json`` blob (the
    full submission, lb-3gi) so no schema change is needed: a submission for a
    diagram can carry ``begrepp`` / ``forklaring`` (the skola-begrepp shape) and
    optionally a ready ``diagram_spec`` (authored by an Opus step) or
    ``use_llm: true`` (ask the 14B composer to enrich the spec). Sensible
    fallbacks keep an old/plain submission renderable: begrepp falls back to the
    job ``title`` then ``prompt``; förklaring defaults empty (the templates don't
    require it).
    """
    params: dict = {}
    raw = job.get("params_json")
    if raw:
        try:
            params = json.loads(raw) or {}
        except (TypeError, ValueError):
            params = {}
    begrepp = (params.get("begrepp") or job.get("title")
               or (job.get("prompt") or "")[:60] or "").strip()
    return {
        "begrepp": begrepp,
        "forklaring": (params.get("forklaring") or params.get("förklaring") or "").strip(),
        "spec": params.get("diagram_spec"),  # an explicit spec, or None
        "use_llm": bool(params.get("use_llm")),
    }


def _render_procedural_job(job: str | dict) -> str:
    """Render a structured-imageType job deterministically + auto-sort it.

    The CPU/procedural twin of the GPU happy path: build the concept inputs from
    the job row, render a clean labeled PNG via :func:`app.procedural.render_concept`
    (graphviz/matplotlib/RDKit — no gpu_lock, no vram_pause, no ComfyUI), write it
    into the isolated output dir auto-sorted by subject/area/imageType, insert the
    ``library`` row, link it onto the job, and publish ``done`` for the SSE stream.
    Returns the new library row id. On failure writes ``failed`` + raises
    :class:`RenderError`, exactly like the GPU path — so the API/SSE contract is
    identical regardless of which engine produced the image.
    """
    job_id = job["id"]
    try:
        write_job_state(job_id, {"job_id": job_id, "status": "running",
                                 "progress_step": 0, "progress_total": 1})

        concept = _concept_spec_from_job(job)
        # Deterministic filename (no ComfyUI to name it). Stable + sortable.
        filename = f"diagram_{job_id[:8]}.png"
        meta_image_type = job["image_type"]
        rel = Path(job["subject"]) / job["area"] / meta_image_type / filename
        dest = _storage_dir() / rel

        _path, used_spec = _procedural.render_concept(
            subject=job["subject"], area=job["area"], image_type=meta_image_type,
            begrepp=concept["begrepp"], forklaring=concept["forklaring"],
            dest=dest, spec=concept["spec"], use_llm=concept["use_llm"],
        )

        # Build the same meta shape _autosort_to_library reads. The "prompt" we
        # persist is the human-readable diagram description (the labels) — that's
        # the provenance for a procedural render, in place of a diffusion prompt.
        meta = {
            "area": job["area"],
            "image_type": meta_image_type,
            "prompt": " · ".join(used_spec.all_labels())[:500],
            "seed": None,
            "width": _procedural.CANVAS_W,
            "height": _procedural.CANVAS_H,
            "lora": None,
            "backend": PROCEDURAL_BACKEND,
            # The full spec is kept as workflow provenance (mirrors workflow_json
            # for a diffusion render) so a regenerate/audit can replay it.
            "workflow_json": json.dumps(used_spec.to_dict(), ensure_ascii=False),
        }
        lib_id = _autosort_to_library(job, meta, rel)

        write_job_state(job_id, {
            "job_id": job_id, "status": "done", "library_id": lib_id,
            "width": meta["width"], "height": meta["height"],
        })
        log.info("procedural render %s done → library %s (%s/%s/%s, kind=%s)",
                 job_id, lib_id, job["subject"], job["area"], meta_image_type,
                 used_spec.kind)
        return lib_id
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
