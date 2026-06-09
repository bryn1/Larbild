"""Film pipeline — storyboard → keyframes → narration → mux → library (lb-4vw,
P6.3).

The end-to-end producer of a Lärbild educational clip. Given a film spec
(videoType + subject/area + a Swedish narration script), it:

  1. **Storyboards** the shots via :mod:`app.film_templates` (lb-1bh) — the
     keyframe skeleton + the backend-authoritative, allowlisted LoRA.
  2. **Renders each keyframe** by REUSING the still pipeline + GPU router. The
     keyframe renderer is *injectable* (:class:`KeyframeRenderer`): the
     production default submits a ``bild`` job through the bridge
     (:func:`app.jobs.enqueue_job` + :func:`app.render_worker.render_job`) so
     frames go through the SAME GPU-locked, SFW-guarded path as stills; tests
     inject a stub renderer so **no live GPU is touched** (CRITICAL: the imagegen
     sweep owns the GPUs — the live keyframe render is DEFERRED, see
     :data:`DEFERRED_LIVE_RENDER`).
  3. **Synthesises narration** via Piper sv-SE on VM600 (:func:`app.tts.synthesize`,
     lb-ayi) — GPU-independent, so this step is real even during the sweep.
  4. **Muxes** the keyframes (with Ken-Burns motion) + narration into an
     ``.mp4`` via ffmpeg (:func:`app.mux.mux_film`, an injectable runner).
  5. **Auto-sorts** the clip into the library as a **VIDEO record** (``kind=
     'film'`` with ``video_type`` / ``duration_s`` / ``narration``), under the
     isolated output dir at ``output/<subject>/<area>/film/<file>.mp4``, and
     links it back onto the film job — the same auto-sort contract as stills
     (lb-b1r). Progress + terminal state are published for the existing
     ``/api/jobs/<id>/events`` SSE (a film is a ``jobs`` row, so the P1 SSE
     already streams it).

Film jobs ride the SAME durable ``jobs`` table + Redis live-state as stills
(``kind='film'``), so submission/SSE/reconcile all reuse :mod:`app.jobs`. A
dedicated film queue key keeps the film worker's BRPOP loop separate from the
still worker's (a film fans out into N keyframe renders; mixing the two queues
would let a film block the stills lane).

SFW-only: orchestration over the SFW still pipeline + SFW narration + the SFW
library; no restricted import, model, or feature. The narration is the teacher's
lesson script; the keyframes resolve only allowlisted LoRAs.

Env overrides:
  LARBILD_FILM_QUEUE_KEY        default 'larbild:film:task_queue'
  LARBILD_FILM_KEYFRAME_TIMEOUT_S default 600  (per keyframe render)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from flask import current_app

from . import film_templates, jobs, mux, tts
from .db import connect, new_id, now_iso
from .gpu_telemetry import RENDER_TARGETS

log = logging.getLogger("larbild.film")

__all__ = [
    "FilmError",
    "KeyframeSpec",
    "KeyframeRenderer",
    "BridgeKeyframeRenderer",
    "enqueue_film",
    "render_film",
    "run_film_worker_loop",
    "FILM_QUEUE_KEY",
    "DEFERRED_LIVE_RENDER",
]

FILM_QUEUE_KEY = os.environ.get("LARBILD_FILM_QUEUE_KEY", "larbild:film:task_queue")
KEYFRAME_TIMEOUT_S = float(os.environ.get("LARBILD_FILM_KEYFRAME_TIMEOUT_S", "600"))
RENDER_BACKEND = os.environ.get("LARBILD_RENDER_BACKEND", "vm100")

# The single thing this bead does NOT do live — a real GPU keyframe render +
# the full end-to-end film validation. The narration (Piper/VM600) IS real (GPU-
# independent); the mux is real ffmpeg but is exercised in tests with a stub
# runner + stub frames. Discoverable + greppable, like the stills worker's flag.
DEFERRED_LIVE_RENDER = (
    "Live keyframe rendering + full end-to-end film validation are DEFERRED "
    "(imagegen sweep owns the GPUs). The pipeline is built + tested with a "
    "MOCKED keyframe renderer; TTS narration (Piper sv-SE on VM600) is real and "
    "GPU-independent; the ffmpeg mux is tested with stub frames + the real "
    "narration wav. Run the live keyframe render + the playable-clip check when "
    "the GPU is free — the shared gpu_lock (reused via the still worker) will "
    "coordinate it."
)


class FilmError(RuntimeError):
    """A film could not be produced (bad spec, render, narration, or mux)."""


@dataclass(frozen=True)
class KeyframeSpec:
    """A single keyframe to render, fully specified (prompt + sort keys + motion).

    The pipeline derives one per storyboard shot: the taxonomy keys (so the
    keyframe render goes through the SFW LoRA resolver for that subject/imageType)
    plus the per-shot prompt, hold duration, and Ken-Burns motion (for the mux).
    """

    index: int
    subject: str
    area: str
    image_type: str
    prompt: str
    duration_s: float
    ken_burns: dict


class KeyframeRenderer(Protocol):
    """Renders one keyframe to an on-disk image, returning its path.

    The seam that lets the film pipeline (a) reuse the real still pipeline in
    production and (b) be fully tested with NO GPU. Implementations MUST return
    a path to an existing image file (the mux reads it).
    """

    def render(self, spec: KeyframeSpec, *, film_job_id: str) -> Path: ...  # pragma: no cover


class BridgeKeyframeRenderer:
    """Production keyframe renderer: render via the bridge + still worker.

    Each keyframe is submitted as a ``bild`` job through the SAME path stills
    take — :func:`app.jobs.enqueue_job` then :func:`app.render_worker.render_job`
    — so it inherits the shared render-residency (gpu_lock) + the SFW LoRA allowlist.
    The produced ``library`` row's ``file_path`` (under the isolated output dir)
    is resolved to an absolute path for the mux.

    NOT exercised in this bead (no live GPU). Imported lazily so the test path
    (stub renderer) never imports the render worker / ComfyUI client.
    """

    def __init__(self, *, comfy_factory: Optional[Callable] = None,
                 timeout_s: float = KEYFRAME_TIMEOUT_S):
        self._comfy_factory = comfy_factory
        self._timeout_s = timeout_s

    def render(self, spec: KeyframeSpec, *, film_job_id: str) -> Path:  # pragma: no cover - live GPU
        from . import render_worker

        kf_job_id = jobs.enqueue_job(
            kind="bild", subject=spec.subject, area=spec.area,
            image_type=spec.image_type, prompt=spec.prompt, seed=None,
            title=f"keyframe {spec.index} for film {film_job_id}",
            params={"film_job_id": film_job_id, "keyframe_index": spec.index},
        )
        kwargs = {"timeout_s": self._timeout_s}
        if self._comfy_factory is not None:
            kwargs["comfy_factory"] = self._comfy_factory
        lib_id = render_worker.render_job(kf_job_id, **kwargs)
        with connect() as conn:
            row = conn.execute(
                "SELECT file_path FROM library WHERE id = ?", (lib_id,)
            ).fetchone()
        if row is None:
            raise FilmError(f"keyframe {spec.index} produced no library row")
        return Path(current_app.config["STORAGE_DIR"]) / row["file_path"]


# ── submission ──────────────────────────────────────────────────────────────


def enqueue_film(
    *,
    video_type: str,
    subject: str,
    area: str,
    narration: str,
    title: Optional[str] = None,
    narration_voice: Optional[str] = None,
    shot_prompts: Optional[list[str]] = None,
) -> str:
    """Validate a film spec, persist a ``jobs`` row, and queue it. Returns job id.

    Builds the storyboard (validates videoType/subject/area/voice via
    :mod:`app.film_templates`) so a bad spec fails *before* anything is queued.
    ``narration`` is the Swedish lesson script (required). ``shot_prompts``, if
    given, supplies a per-keyframe image prompt (one per shot); otherwise each
    shot's prompt is scaffolded from the template hint + subject/area. The whole
    spec is stored in the job's ``params_json`` so the film worker can rebuild it.

    The durable row is written first (crash-safety: never queued without a
    record), then the live ``queued`` state, then the LPUSH onto the FILM queue.
    Raises :class:`FilmError` on a bad spec or :class:`app.jobs.QueueFull`.
    """
    narration = (narration or "").strip()
    if not narration:
        raise FilmError("narration script is required")

    try:
        storyboard = film_templates.build_storyboard(
            video_type, subject, area, narration_voice=narration_voice
        )
    except film_templates.FilmTemplateError as e:
        raise FilmError(str(e)) from e

    if shot_prompts is not None and len(shot_prompts) != len(storyboard["shots"]):
        raise FilmError(
            f"shot_prompts has {len(shot_prompts)} entries but the "
            f"{video_type} storyboard has {len(storyboard['shots'])} shots"
        )

    spec = {
        "video_type": video_type,
        "subject": subject,
        "area": area,
        "title": (title or "").strip() or None,
        "narration": narration,
        "narration_voice": storyboard["narration_voice"],
        "shot_prompts": shot_prompts,
        "storyboard": storyboard,
    }

    # Reuse the shared queue-depth guard (films and stills share the depth cap).
    depth = jobs.queue_depth()
    if depth >= jobs.MAX_DEPTH:
        raise jobs.QueueFull(depth, jobs.MAX_DEPTH)

    job_id = new_id()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs
                (id, kind, status, subject, area, video_type, title, prompt,
                 params_json)
            VALUES (?, 'film', 'queued', ?, ?, ?, ?, ?, ?)
            """,
            (job_id, subject, area, video_type, spec["title"], narration,
             json.dumps(spec)),
        )

    initial = {"job_id": job_id, "status": "queued", "kind": "film"}
    try:
        r = jobs.get_redis()
        r.set(jobs._state_key(job_id), json.dumps(initial), ex=jobs.JOB_STATE_TTL_S)
        r.lpush(FILM_QUEUE_KEY, job_id)
    except Exception:
        log.exception("enqueue_film: redis push failed for %s", job_id)
        raise
    log.info("enqueued film job %s %s %s/%s", job_id, video_type, subject, area)
    return job_id


# ── the producer ────────────────────────────────────────────────────────────


def _shot_prompt(storyboard: dict, shot: dict, override: Optional[str]) -> str:
    """The image prompt for one keyframe: an explicit override or a scaffold.

    The scaffold combines the subject/area + the shot's pedagogical hint into a
    neutral educational prompt. It never names a LoRA (the LoRA is resolved
    backend-side from subject + imageType — the SFW guard).
    """
    if override and override.strip():
        return override.strip()
    return (
        f"educational {storyboard['image_type']} for {storyboard['subject']} "
        f"({storyboard['area']}): {shot['prompt_hint']}"
    )


def _keyframe_specs(storyboard: dict, shot_prompts: Optional[list[str]]) -> list[KeyframeSpec]:
    specs: list[KeyframeSpec] = []
    for i, shot in enumerate(storyboard["shots"]):
        override = shot_prompts[i] if shot_prompts else None
        specs.append(KeyframeSpec(
            index=i,
            subject=storyboard["subject"],
            area=storyboard["area"],
            image_type=storyboard["image_type"],
            prompt=_shot_prompt(storyboard, shot, override),
            duration_s=float(shot["duration_s"]),
            ken_burns=shot["ken_burns"],
        ))
    return specs


def _film_output_path(subject: str, area: str, filename: str) -> Path:
    """Auto-sort target RELATIVE to STORAGE_DIR/output: <subject>/<area>/film/."""
    return Path(subject) / area / "film" / filename


def _autosort_film_to_library(job: dict, spec: dict, rel_path: Path,
                              duration_s: float, lora: dict) -> str:
    """Insert the auto-sorted VIDEO ``library`` row; return its id (lb-b1r).

    A ``kind='film'`` row carrying the videoType + duration + narration voice (the
    film-only columns) plus provenance, sorted under the same subject/area the
    Bibliotek tree groups on. Written BEFORE the film job is marked done + linked,
    so the jobs.library_id FK is satisfied.
    """
    lib_id = new_id()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO library
                (id, kind, subject, area, image_type, video_type, title, job_id,
                 file_path, prompt, lora, lora_strength, backend,
                 duration_s, narration, created_at)
            VALUES (?, 'film', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lib_id, job["subject"], job["area"], spec["video_type"],
                spec.get("title") or f"{spec['video_type']} — {job['subject']}",
                job["id"], str(Path("output") / rel_path),
                spec.get("narration", "")[:2000],
                lora.get("name"), lora.get("strength"),
                _backend_label(),
                int(round(duration_s)), spec.get("narration_voice"),
                now_iso(),
            ),
        )
    log.info("auto-sorted film → library %s (%s/%s/%s)", lib_id,
             job["subject"], job["area"], spec["video_type"])
    return lib_id


def _backend_label() -> str:
    return RENDER_TARGETS.get(RENDER_BACKEND, {}).get("label", RENDER_BACKEND)


def render_film(
    film_job_id: str,
    *,
    keyframe_renderer: Optional[KeyframeRenderer] = None,
    tts_client: Optional[tts.PiperClient] = None,
    mux_run: Optional[mux.RunFn] = None,
) -> str:
    """Produce one queued film end-to-end and auto-sort it as a VIDEO record.

    Loads the film job, rebuilds the storyboard, renders every keyframe (via the
    injected ``keyframe_renderer`` — production reuses the still pipeline; tests
    stub it so NO live GPU is touched), synthesises the Swedish narration (Piper,
    via the injected ``tts_client``), muxes keyframes + Ken-Burns + narration
    into an mp4 (ffmpeg, via the injected ``mux_run``), writes it to the isolated
    output dir auto-sorted as a film ``library`` row, links it onto the job, and
    marks the job ``done`` (publishing progress for the SSE stream).

    Returns the new ``library`` row id. On any failure writes a ``failed`` state
    + re-raises :class:`FilmError`. Collaborators are injected with production
    defaults; the mux is CPU-only and the narration is GPU-independent, so the
    only deferred-until-GPU step is the keyframe render (lb-4vw).
    """
    job = jobs.get_job_row(film_job_id)
    if job is None:
        raise FilmError(f"film job {film_job_id} not found")
    if job["kind"] != "film":
        raise FilmError(f"render_film handles kind='film' only, got {job['kind']!r}")

    spec = json.loads(job["params_json"]) if job["params_json"] else {}
    storyboard = spec.get("storyboard")
    if not storyboard:
        raise FilmError(f"film job {film_job_id} has no storyboard in params")

    if keyframe_renderer is None:  # pragma: no cover - production default (live GPU)
        keyframe_renderer = BridgeKeyframeRenderer()

    specs = _keyframe_specs(storyboard, spec.get("shot_prompts"))
    total = len(specs) + 2  # keyframes + narration + mux
    try:
        jobs.write_job_state(film_job_id, {"job_id": film_job_id, "status": "running",
                                           "kind": "film", "progress_step": 0,
                                           "progress_total": total})

        # 1) keyframes (each via the still pipeline / stub). NO live GPU here.
        shots: list[mux.Shot] = []
        for i, ks in enumerate(specs):
            img = Path(keyframe_renderer.render(ks, film_job_id=film_job_id))
            if not img.is_file():
                raise FilmError(f"keyframe {ks.index} renderer returned a missing file: {img}")
            shots.append(mux.Shot(image_path=img, duration_s=ks.duration_s,
                                  ken_burns=ks.ken_burns))
            jobs.write_job_state(film_job_id, {
                "job_id": film_job_id, "status": "running", "kind": "film",
                "progress_step": i + 1, "progress_total": total,
                "stage": f"keyframe {i + 1}/{len(specs)}"})

        # 2) narration (Piper sv-SE on VM600 — GPU-independent, real even now).
        wav = tts.synthesize(spec["narration"], storyboard["narration_voice"],
                             client=tts_client)
        narration_path = _storage_dir() / "output" / _film_output_path(
            job["subject"], job["area"], f"{film_job_id}.wav")
        narration_path.parent.mkdir(parents=True, exist_ok=True)
        narration_path.write_bytes(wav)
        narration_s = tts.wav_duration_s(wav)
        jobs.write_job_state(film_job_id, {
            "job_id": film_job_id, "status": "running", "kind": "film",
            "progress_step": len(specs) + 1, "progress_total": total,
            "stage": "narration"})

        # 3) mux keyframes + narration → mp4 (ffmpeg; stubbed in tests).
        rel = _film_output_path(job["subject"], job["area"], f"{film_job_id}.mp4")
        out_path = _storage_dir() / "output" / rel
        mux.mux_film(shots, narration_path, out_path, run=mux_run)

        # 4) auto-sort as a VIDEO library record + link onto the job.
        duration_s = max(narration_s, sum(s.duration_s for s in shots))
        lib_id = _autosort_film_to_library(job, spec, rel, duration_s,
                                            storyboard.get("lora", {}))

        jobs.write_job_state(film_job_id, {
            "job_id": film_job_id, "status": "done", "kind": "film",
            "library_id": lib_id, "duration_s": int(round(duration_s)),
            "progress_step": total, "progress_total": total})
        return lib_id
    except (tts.TTSError, mux.MuxError, film_templates.FilmTemplateError, FilmError) as e:
        jobs.write_job_state(film_job_id, {"job_id": film_job_id, "status": "failed",
                                           "kind": "film", "error": str(e)})
        raise FilmError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        jobs.write_job_state(film_job_id, {"job_id": film_job_id, "status": "failed",
                                           "kind": "film", "error": str(e)})
        raise FilmError(str(e)) from e


def _storage_dir() -> Path:
    base = Path(current_app.config["STORAGE_DIR"])
    base.mkdir(parents=True, exist_ok=True)
    return base


def run_film_worker_loop(stop: Optional[Callable[[], bool]] = None) -> None:  # pragma: no cover
    """Production BRPOP loop for the film queue. NOT run during this bead (no GPU).

    Pulls film job ids off ``FILM_QUEUE_KEY`` and produces each. Each film's
    keyframes go through the still pipeline under the shared gpu_lock, so films +
    stills + imagegen coexist on the GPUs.
    """
    log.info("film worker loop starting. %s", DEFERRED_LIVE_RENDER)
    r = jobs.get_redis()
    while stop is None or not stop():
        popped = r.brpop(FILM_QUEUE_KEY, timeout=5)
        if popped is None:
            continue
        _key, job_id = popped
        try:
            render_film(job_id)
        except FilmError as e:
            log.error("film job %s failed: %s", job_id, e)
