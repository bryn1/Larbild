"""Lesson assembly — manuscript → beat-clips → the merged overview video (lb-lesson).

The producer that turns a parsed :class:`app.lesson.Lesson` manuscript (the Opus-
generated pedagogical script, see :mod:`app.lesson`) into a playable Lärbild
*Lärbild* video and auto-sorts it into the ``library``. It is the lesson sibling
of :func:`app.film.render_film`, but built on the richer manuscript model:

  per BEAT (one *begrepp*):
    1. **render** the beat's keyframe through the SAME GPU-locked, SFW-guarded
       still pipeline films use (the injectable :class:`app.film.KeyframeRenderer`
       — production = the bridge + still worker; tests stub it so NO live GPU is
       touched);
    2. **overlay the bildtext** (the level-scaled caption) onto that keyframe as a
       lower-third label (:func:`app.caption.overlay_caption`, CPU/Pillow);
    3. **synthesise the narration** for the beat — the spoken talking-point line
       (+ its connection tail) — via Piper sv-SE (:func:`app.tts.synthesize`,
       GPU-independent), so each beat's audio length is known and the beat's hold
       can be timed to its voice (the caption + image never out-run the narration);
  then MERGE the captioned beat-stills (each held for its narration's length,
  Ken-Burns panning) + the continuous concatenated narration into one mp4 with
  smooth crossfades (:func:`app.mux.mux_lesson`), TARGET 5-10 min for an overview.

The 3 LEVELS (``enkel`` / ``utvecklad`` / ``välutvecklad``) are produced by
:func:`assemble_levels`: the captions (and, if the Opus step wrote per-level
manuscripts, the narration) differ per level; the keyframes are RENDERED ONCE and
REUSED across the three level-versions (the image of a *begrepp* is the same at
every level — only the bildtext rung + the level tag change), so three level-
videos cost one render pass, three caption passes, three narration passes, three
muxes. Each level-video auto-sorts as its own level-tagged ``library`` row.

SHORTS — a per-*begrepp* short and a FAMILY short (multiple related concepts
compared in one clip, e.g. ljus → konvex vs konkav spegel/lins) — are just lessons
with ``kind='short'`` and few beats; the same per-beat assembly + merge produces
them (:func:`assemble_lesson` handles both — ``kind`` only changes the library
artefact kind + the output sub-dir + the title).

Everything that touches a GPU or VM600 is INJECTED (the keyframe renderer, the
caption overlay fn, the TTS client, the ffmpeg runner) so the whole pipeline is
unit-testable against a SAMPLE manuscript with render + TTS + ffmpeg MOCKED — the
live GPU keyframe render is DEFERRED exactly like the film pipeline
(:data:`app.film.DEFERRED_LIVE_RENDER`). The narration (Piper) and the caption
overlay (Pillow) and the mux argv are real in tests; only the keyframe PIXELS are
stubbed.

SFW-only: orchestration over the SFW still pipeline + SFW narration + SFW caption
overlay + the SFW library — a curriculum overview video; no restricted import,
model, content, or reference. The LoRA is resolved backend-side per keyframe from
(subject, image_type) — never named in a manuscript (the SFW guard).

Env overrides:
  LARBILD_LESSON_QUEUE_KEY          default 'larbild:lesson:task_queue'
  LARBILD_LESSON_KEYFRAME_TIMEOUT_S default 600  (per keyframe render)
  LARBILD_LESSON_WPM                default 150  (Swedish narration words/min,
                                        used only to ESTIMATE a beat hold when the
                                        narration's real WAV length is unavailable)
  LARBILD_LESSON_BEAT_PAD_S         default 0.6  (a breath of silence padding the
                                        end of each beat's narration hold)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from flask import current_app

from . import caption as caption_mod
from . import film, jobs, lesson as lesson_mod, mux, tts
from .db import connect, new_id, now_iso
from .gpu_telemetry import RENDER_TARGETS

log = logging.getLogger("larbild.assembly")

__all__ = [
    "AssemblyError",
    "LESSON_QUEUE_KEY",
    "DEFERRED_LIVE_RENDER",
    "BeatAsset",
    "AssembledLesson",
    "enqueue_lesson",
    "assemble_lesson",
    "assemble_levels",
    "render_lesson_job",
    "run_lesson_worker_loop",
    "estimate_beat_duration_s",
    "store_manuscript",
]

LESSON_QUEUE_KEY = os.environ.get("LARBILD_LESSON_QUEUE_KEY", "larbild:lesson:task_queue")
KEYFRAME_TIMEOUT_S = float(os.environ.get("LARBILD_LESSON_KEYFRAME_TIMEOUT_S", "600"))
RENDER_BACKEND = os.environ.get("LARBILD_RENDER_BACKEND", "vm100")

# Swedish narration pace used ONLY to estimate a beat's hold when the real
# synthesised WAV length isn't available yet (e.g. a duration preview, or a beat
# whose narration the assembly hasn't voiced). The real assembly always prefers
# the measured WAV length so the bildtext + image are held exactly as long as the
# voice. ~150 wpm is a calm pedagogical sv-SE pace.
LESSON_WPM = float(os.environ.get("LARBILD_LESSON_WPM", "150"))
# A short breath of silence padding each beat's narration so the next concept
# doesn't begin the instant the last word ends (and the crossfade has room).
BEAT_PAD_S = float(os.environ.get("LARBILD_LESSON_BEAT_PAD_S", "0.6"))

# Same deferral the film pipeline carries — the GPU keyframe render + the real
# end-to-end playable-clip check are DEFERRED (imagegen sweep owns the GPUs). The
# narration (Piper/VM600) is real + GPU-independent; the caption overlay (Pillow)
# is real; the mux is real ffmpeg, exercised in tests with stub frames + the real
# narration wav + a stub runner. Greppable, like film.DEFERRED_LIVE_RENDER.
DEFERRED_LIVE_RENDER = (
    "Live keyframe rendering + the full end-to-end lesson playable-clip check are "
    "DEFERRED (imagegen sweep owns the GPUs). The assembly is built + tested with "
    "a MOCKED keyframe renderer; TTS narration (Piper sv-SE on VM600) is real and "
    "GPU-independent; the caption overlay (Pillow) is real; the ffmpeg mux is "
    "tested with stub frames + the real narration wav. Run the live keyframe "
    "render + the playable-clip check when the GPU is free — the shared gpu_lock "
    "(reused via the still worker) coordinates it. GPU use granted for this."
)


class AssemblyError(RuntimeError):
    """A lesson could not be assembled (bad manuscript, render, caption, narration, mux)."""


# ── per-beat estimate (no I/O) ───────────────────────────────────────────────


def estimate_beat_duration_s(beat: "lesson_mod.Beat", level: Optional[str]) -> float:
    """Estimate a beat's on-screen hold in seconds (no synthesis).

    Used for a duration PREVIEW (the API can report a target length before the
    render runs) and as the fallback hold when a beat's real narration WAV length
    isn't measured. Order of preference:

      1. the beat's explicit ``duration_s`` (the manuscript pinned it), else
      2. a words-per-minute estimate of the beat's spoken line at
         :data:`LESSON_WPM`, padded by :data:`BEAT_PAD_S`, floored at the
         lesson model's per-beat minimum.

    The REAL assembly overrides this with the measured WAV length whenever it has
    voiced the beat; this is only the no-synthesis estimate.
    """
    if beat.duration_s is not None:
        return float(beat.duration_s)
    line = beat.narration_for(level)
    words = len(line.split())
    secs = (words / LESSON_WPM) * 60.0 if words else 0.0
    return max(secs + BEAT_PAD_S, lesson_mod._MIN_BEAT_S)


def estimate_lesson_duration_s(lesson: "lesson_mod.Lesson") -> float:
    """Estimate the merged overview length (sum of beat estimates − crossfades).

    Mirrors :func:`app.mux.lesson_video_duration_s` over the per-beat estimates so
    the API can advertise a length without rendering. Advisory only.
    """
    durs = [estimate_beat_duration_s(b, lesson.level) for b in lesson.beats]
    n = len(durs)
    total = sum(durs)
    if n > 1 and mux.LESSON_TRANSITION_S > 0:
        shortest = min(durs)
        tr = min(mux.LESSON_TRANSITION_S, max(0.0, shortest * 0.5))
        total -= tr * (n - 1)
    return max(total, 0.0)


# ── per-beat asset (one rendered + captioned + voiced beat) ──────────────────


@dataclass(frozen=True)
class BeatAsset:
    """One fully-assembled beat: its captioned keyframe + its narration WAV + hold.

    ``image_path`` is the CAPTIONED keyframe (the bildtext already burned on).
    ``narration_wav`` is this beat's spoken line as PCM WAV bytes (None if the
    beat carried no narration — a silent hold, rare). ``duration_s`` is the
    on-screen hold (the measured narration length + a breath, or the estimate).
    ``ken_burns`` is the pan/zoom for the mux. Built by :func:`_assemble_beat`.
    """

    index: int
    image_path: Path
    narration_wav: Optional[bytes]
    duration_s: float
    ken_burns: dict


@dataclass(frozen=True)
class AssembledLesson:
    """The result of assembling one lesson/short: the mp4 + its facets.

    ``library_id`` is the auto-sorted ``library`` VIDEO row; ``out_path`` the mp4;
    ``duration_s`` the merged length; ``beat_count`` the begrepp covered.
    """

    library_id: str
    out_path: Path
    duration_s: float
    beat_count: int
    level: Optional[str]
    kind: str


# Ken-Burns presets cycled across beats so an overview isn't a static slideshow.
# A gentle alternating pan/zoom (the film templates use the same KenBurns shape:
# {zoom_from, zoom_to, pan}). Stills only — a short_clip beat would override this
# once i2v is wired (deferred); today every beat is a Ken-Burns still.
_KB_CYCLE = (
    {"zoom_from": 1.0, "zoom_to": 1.08, "pan": "right"},
    {"zoom_from": 1.08, "zoom_to": 1.0, "pan": "left"},
    {"zoom_from": 1.0, "zoom_to": 1.06, "pan": "up"},
    {"zoom_from": 1.06, "zoom_to": 1.0, "pan": "down"},
    {"zoom_from": 1.0, "zoom_to": 1.10, "pan": "in"},
)


def _ken_burns_for(index: int) -> dict:
    return dict(_KB_CYCLE[index % len(_KB_CYCLE)])


# ── the keyframe (render-once, reuse across levels) ──────────────────────────


def _render_keyframes(
    lesson: "lesson_mod.Lesson",
    *,
    keyframe_renderer: "film.KeyframeRenderer",
    job_id: str,
    on_step: Optional[Callable[[int, str], None]] = None,
) -> list[Path]:
    """Render one keyframe per beat, in order; return the image paths.

    Each beat is rendered through the injected :class:`app.film.KeyframeRenderer`
    (production reuses the still pipeline under the shared gpu_lock + SFW LoRA
    allowlist; tests stub it — NO live GPU). The keyframe is the UN-captioned image
    of the *begrepp*; it is RENDERED ONCE here and reused across all three level-
    versions (only the overlaid bildtext rung differs per level), so the expensive
    GPU step runs ``len(beats)`` times for an area, not ``3 × len(beats)``.

    ``on_step(step, label)`` is called after each keyframe so the caller can
    publish SSE progress. Raises :class:`AssemblyError` if a renderer returns a
    missing file.
    """
    paths: list[Path] = []
    for beat in lesson.beats:
        spec = film.KeyframeSpec(
            index=beat.index,
            subject=lesson.subject,
            area=lesson.area,
            image_type=beat.image_type,
            prompt=beat.image_prompt_or_scaffold(lesson.subject, lesson.area),
            duration_s=estimate_beat_duration_s(beat, lesson.level),
            ken_burns=_ken_burns_for(beat.index),
        )
        img = Path(keyframe_renderer.render(spec, film_job_id=job_id))
        if not img.is_file():
            raise AssemblyError(
                f"beat {beat.index} ({beat.begrepp!r}) keyframe renderer returned "
                f"a missing file: {img}"
            )
        paths.append(img)
        if on_step is not None:
            on_step(beat.index, f"keyframe {beat.index + 1}/{len(lesson.beats)}")
    return paths


# ── per-beat assembly: caption overlay + narration + hold ────────────────────


def _assemble_beat(
    beat: "lesson_mod.Beat",
    level: Optional[str],
    keyframe_path: Path,
    *,
    out_dir: Path,
    voice: str,
    overlay_fn: caption_mod.OverlayFn,
    tts_client: Optional[tts.PiperClient],
) -> BeatAsset:
    """Assemble one beat: overlay its bildtext + synth its narration + time the hold.

    1. Overlays the level-scaled caption (``beat.caption_for(level)``) onto the
       rendered keyframe via ``overlay_fn`` (default :func:`app.caption.overlay_
       caption`; a test stub records the call). An empty caption is a no-op copy
       so every beat yields one captioned image path.
    2. Synthesises the beat's spoken line (``beat.narration_for(level)`` — the
       talking-point + its connection tail) via Piper (:func:`app.tts.synthesize`,
       GPU-independent). A beat with no spoken line yields ``narration_wav=None``
       (a silent hold).
    3. Times the on-screen hold to the MEASURED narration length + a breath
       (:data:`BEAT_PAD_S`), floored at the lesson minimum — so the bildtext +
       image are held exactly as long as the voice (never cut mid-sentence). When
       a beat carries no narration, the WPM/explicit estimate is used.

    Returns a :class:`BeatAsset`. Raises :class:`AssemblyError` on a caption or
    narration failure (wrapping the underlying CaptionError / TTSError).
    """
    cap_text = beat.caption_for(level)
    cap_dest = out_dir / f"beat{beat.index:03d}_cap.png"
    try:
        captioned = Path(overlay_fn(keyframe_path, cap_text, cap_dest))
    except caption_mod.CaptionError as e:
        raise AssemblyError(f"beat {beat.index}: caption overlay failed: {e}") from e

    line = beat.narration_for(level)
    wav: Optional[bytes] = None
    if line:
        try:
            wav = tts.synthesize(line, voice, client=tts_client)
        except tts.TTSError as e:
            raise AssemblyError(f"beat {beat.index}: narration failed: {e}") from e

    if wav is not None:
        hold = tts.wav_duration_s(wav) + BEAT_PAD_S
        hold = max(hold, lesson_mod._MIN_BEAT_S)
        if beat.duration_s is not None:
            # An explicit hold is a FLOOR (never cut the voice): use the larger of
            # the pinned hold and the voiced length.
            hold = max(hold, float(beat.duration_s))
    else:
        hold = estimate_beat_duration_s(beat, level)

    return BeatAsset(
        index=beat.index,
        image_path=captioned,
        narration_wav=wav,
        duration_s=hold,
        ken_burns=_ken_burns_for(beat.index),
    )


def _concat_beat_narration(assets: list[BeatAsset], out_wav: Path) -> Optional[Path]:
    """Concatenate the per-beat narration WAVs into one continuous track.

    The *berättarröst that ties the content together*: each beat was voiced
    separately (so its hold could be timed to its own line), and the clips are now
    joined — with a short :data:`BEAT_PAD_S` of silence between beats — into a
    single WAV laid over the whole merged timeline. Reuses :func:`app.tts._concat_
    wavs` (stdlib ``wave`` — no ffmpeg/numpy). Returns the WAV path, or None when
    no beat carried narration (a fully silent lesson — the mux then has no audio).
    """
    clips = [a.narration_wav for a in assets if a.narration_wav is not None]
    if not clips:
        return None
    joined = tts._concat_wavs(clips, BEAT_PAD_S)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    out_wav.write_bytes(joined)
    return out_wav


# ── output paths + library auto-sort ─────────────────────────────────────────


def _lesson_output_dir(subject: str, area: str, kind: str) -> Path:
    """Auto-sort dir RELATIVE to STORAGE_DIR/output: <subject>/<area>/<kind>/."""
    return Path(subject) / area / kind


def _backend_label() -> str:
    return RENDER_TARGETS.get(RENDER_BACKEND, {}).get("label", RENDER_BACKEND)


def _autosort_to_library(
    lesson: "lesson_mod.Lesson",
    *,
    rel_path: Path,
    duration_s: float,
    job_id: Optional[str],
    lora: dict,
) -> str:
    """Insert the auto-sorted VIDEO ``library`` row for a lesson/short; return its id.

    A ``kind='lesson'`` (overview) or ``kind='short'`` row, LEVEL-TAGGED
    (``library.level`` — the owner's "level-tagged" requirement), carrying the
    duration + narration voice (the video columns) under the same subject/area the
    Bibliotek tree groups on. It inherits the Bibliotek browse + the review/
    regenerate loop (lb-review-regen) for free — a lesson video is just another
    library row to review OK / Inte-OK + comment.

    Written so the ``jobs.library_id`` FK (set when the job is marked done) is
    satisfied. ``file_path`` is the STORAGE_DIR-relative ``output/...`` path the
    media route serves.
    """
    lib_id = new_id()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO library
                (id, kind, subject, area, image_type, video_type, level, title,
                 job_id, file_path, prompt, lora, lora_strength, backend,
                 duration_s, narration, created_at)
            VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lib_id, lesson.kind, lesson.subject, lesson.area, lesson.level,
                lesson.display_title(), job_id,
                str(Path("output") / rel_path),
                lesson.narration_script()[:2000],
                lora.get("name"), lora.get("strength"), _backend_label(),
                int(round(duration_s)), lesson.narration_voice, now_iso(),
            ),
        )
    log.info("auto-sorted %s → library %s (%s/%s level=%s)", lesson.kind, lib_id,
             lesson.subject, lesson.area, lesson.level)
    return lib_id


def store_manuscript(
    lesson: "lesson_mod.Lesson",
    *,
    job_id: Optional[str] = None,
    library_id: Optional[str] = None,
) -> str:
    """Persist a manuscript into the ``lessons`` table; return the lessons row id.

    The durable record of the structured Opus manuscript a lesson/short was (or
    will be) assembled from — its facets (subject/area/level/kind/begrepp_count)
    plus the full beat-by-beat manuscript JSON and the soft links to the render
    job + produced library video. Lets a lesson be re-assembled / inspected later
    ("this video was made from N begrepp at level X"). The links are best-effort
    soft refs (no FK), so storing before the job/library rows exist is fine — they
    are filled in by :func:`render_lesson_job` once known.
    """
    row_id = new_id()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO lessons
                (id, kind, subject, area, level, title, begrepp_count,
                 duration_target_s, manuscript_json, narration, job_id, library_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id, lesson.kind, lesson.subject, lesson.area, lesson.level,
                lesson.display_title(), lesson.begrepp_count,
                lesson.duration_target_s, json.dumps(lesson.as_dict()),
                lesson.narration_voice, job_id, library_id,
            ),
        )
    return row_id


def _link_manuscript(lesson_row_id: Optional[str], *, job_id=None, library_id=None) -> None:
    """Best-effort: fill in a lessons row's job_id / library_id once known."""
    if not lesson_row_id:
        return
    sets, params = [], []
    if job_id is not None:
        sets.append("job_id = ?")
        params.append(job_id)
    if library_id is not None:
        sets.append("library_id = ?")
        params.append(library_id)
    if not sets:
        return
    params.append(lesson_row_id)
    try:
        with connect() as conn:
            conn.execute(f"UPDATE lessons SET {', '.join(sets)} WHERE id = ?", params)
    except Exception as e:  # noqa: BLE001 — soft link, never fatal
        log.warning("_link_manuscript failed for %s: %s", lesson_row_id, e)


# ── the assembler (one lesson/short) ─────────────────────────────────────────


def _storage_dir() -> Path:
    base = Path(current_app.config["STORAGE_DIR"])
    base.mkdir(parents=True, exist_ok=True)
    return base


def assemble_lesson(
    lesson: "lesson_mod.Lesson",
    *,
    keyframe_renderer: Optional["film.KeyframeRenderer"] = None,
    overlay_fn: Optional[caption_mod.OverlayFn] = None,
    tts_client: Optional[tts.PiperClient] = None,
    mux_run: Optional[mux.RunFn] = None,
    keyframe_paths: Optional[list[Path]] = None,
    job_id: Optional[str] = None,
    lora: Optional[dict] = None,
    on_step: Optional[Callable[[int, int, str], None]] = None,
    out_name: Optional[str] = None,
) -> AssembledLesson:
    """Assemble ONE lesson/short manuscript into a merged mp4 + library row.

    The end-to-end producer for a single level-version (or a short): render each
    beat's keyframe (or reuse ``keyframe_paths`` — the render-once-reuse-across-
    levels optimisation), overlay each beat's level-scaled bildtext, voice each
    beat's narration (timing the hold to the voice), merge the captioned beat-
    stills + the continuous narration into an mp4 with crossfades
    (:func:`app.mux.mux_lesson`), and auto-sort it as a level-tagged VIDEO library
    row.

    Collaborators are injected with production defaults — the keyframe renderer
    reuses the still pipeline (live GPU, deferred), the caption overlay + TTS +
    mux are real; tests stub them so the whole flow runs with NO GPU. ``on_step
    (step, total, label)`` publishes progress for the SSE. Returns an
    :class:`AssembledLesson`. Raises :class:`AssemblyError` on any failure.

    ``keyframe_paths`` (optional) supplies pre-rendered keyframes (one per beat,
    in order) so :func:`assemble_levels` renders the area ONCE and reuses the
    images across the three level-videos. ``out_name`` overrides the output mp4
    stem (default a fresh uuid); ``job_id`` links the produced rows to a render
    job; ``lora`` is the resolved (name, strength) for the library row's
    provenance (the SFW LoRA the keyframes used).
    """
    if not isinstance(lesson, lesson_mod.Lesson):
        raise AssemblyError("assemble_lesson requires a parsed app.lesson.Lesson")
    if not lesson.beats:
        raise AssemblyError("manuscript has no beats")

    if keyframe_renderer is None:  # pragma: no cover - production default (live GPU)
        keyframe_renderer = film.BridgeKeyframeRenderer(timeout_s=KEYFRAME_TIMEOUT_S)
    overlay_fn = overlay_fn or caption_mod.overlay_caption
    lora = lora or {}

    storage = _storage_dir()
    rel_dir = _lesson_output_dir(lesson.subject, lesson.area, lesson.kind)
    work_dir = storage / "output" / rel_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    n = len(lesson.beats)
    # progress budget: (render? per beat) + (caption+voice per beat) + merge
    total_steps = (0 if keyframe_paths is not None else n) + n + 1
    step = 0

    def _bump(label: str) -> None:
        nonlocal step
        step += 1
        if on_step is not None:
            on_step(step, total_steps, label)

    # 1) keyframes — render once, or reuse the caller's pre-rendered set.
    if keyframe_paths is None:
        def _kf_step(_idx: int, label: str) -> None:
            _bump(label)
        keyframe_paths = _render_keyframes(
            lesson, keyframe_renderer=keyframe_renderer, job_id=job_id or "lesson",
            on_step=_kf_step,
        )
    if len(keyframe_paths) != n:
        raise AssemblyError(
            f"keyframe_paths has {len(keyframe_paths)} images but the manuscript "
            f"has {n} beats"
        )

    # 2) per-beat: caption overlay + narration + timed hold.
    lvl_tag = lesson.level or "x"
    beat_out = work_dir / (out_name or new_id())
    beat_out_caps = Path(f"{beat_out}_{lvl_tag}")
    beat_out_caps.mkdir(parents=True, exist_ok=True)
    assets: list[BeatAsset] = []
    for beat, kf in zip(lesson.beats, keyframe_paths):
        asset = _assemble_beat(
            beat, lesson.level, Path(kf),
            out_dir=beat_out_caps, voice=lesson.narration_voice,
            overlay_fn=overlay_fn, tts_client=tts_client,
        )
        assets.append(asset)
        _bump(f"beat {beat.index + 1}/{n} ({beat.begrepp})")

    # 3) merge: continuous narration + crossfaded captioned stills → the mp4.
    narration_wav = _concat_beat_narration(
        assets, beat_out_caps / "narration.wav"
    )
    shots = [
        mux.Shot(image_path=a.image_path, duration_s=a.duration_s, ken_burns=a.ken_burns)
        for a in assets
    ]
    stem = out_name or beat_out.name
    rel_mp4 = rel_dir / f"{stem}_{lvl_tag}.mp4"
    out_path = storage / "output" / rel_mp4
    try:
        mux.mux_lesson(shots, narration_wav, out_path, run=mux_run)
    except mux.MuxError as e:
        raise AssemblyError(f"lesson merge failed: {e}") from e
    _bump("merge")

    duration_s = mux.lesson_video_duration_s(shots)
    lib_id = _autosort_to_library(
        lesson, rel_path=rel_mp4, duration_s=duration_s, job_id=job_id, lora=lora,
    )
    return AssembledLesson(
        library_id=lib_id, out_path=out_path, duration_s=duration_s,
        beat_count=n, level=lesson.level, kind=lesson.kind,
    )


def assemble_levels(
    lesson: "lesson_mod.Lesson",
    levels: Optional[tuple[str, ...]] = None,
    *,
    keyframe_renderer: Optional["film.KeyframeRenderer"] = None,
    overlay_fn: Optional[caption_mod.OverlayFn] = None,
    tts_client: Optional[tts.PiperClient] = None,
    mux_run: Optional[mux.RunFn] = None,
    job_id: Optional[str] = None,
    lora: Optional[dict] = None,
    on_step: Optional[Callable[[int, int, str], None]] = None,
) -> list[AssembledLesson]:
    """Produce the THREE level-versions of one delområde overview, sharing renders.

    The owner's "3 levels" deliverable: from one manuscript (which carries all
    three caption rungs per beat, or which the Opus step wrote per-level), produce
    an ``enkel`` + ``utvecklad`` + ``välutvecklad`` overview video — captions (and
    the narration the manuscript chose) differ per level. The expensive GPU
    KEYFRAME RENDER runs ONCE here (the *begrepp* image is identical at every
    level); the three level-videos then each overlay their caption rung + voice +
    merge + auto-sort as their own level-tagged library row.

    ``levels`` defaults to all three (:data:`app.lesson.LEVELS`). Only valid for a
    ``kind='lesson'`` overview — a short is single-version (call :func:`assemble_
    lesson`). Returns the list of :class:`AssembledLesson`, one per level, in
    ``levels`` order. ``on_step`` is shared across the whole multi-level run.
    """
    if lesson.kind != "lesson":
        raise AssemblyError(
            "assemble_levels is for a per-delområde overview (kind='lesson'); "
            "a short is single-version — use assemble_lesson"
        )
    levels = levels or lesson_mod.LEVELS

    if keyframe_renderer is None:  # pragma: no cover - production default (live GPU)
        keyframe_renderer = film.BridgeKeyframeRenderer(timeout_s=KEYFRAME_TIMEOUT_S)
    overlay_fn = overlay_fn or caption_mod.overlay_caption

    n = len(lesson.beats)
    # Budget: render n keyframes ONCE + (caption+voice per beat + merge) PER level.
    total_steps = n + len(levels) * (n + 1)
    step = 0

    def _bump(label: str) -> None:
        nonlocal step
        step += 1
        if on_step is not None:
            on_step(step, total_steps, label)

    # 1) render the keyframes ONCE (level-independent imagery), then reuse.
    def _kf_step(_idx: int, label: str) -> None:
        _bump(label)
    keyframe_paths = _render_keyframes(
        lesson, keyframe_renderer=keyframe_renderer, job_id=job_id or "lesson",
        on_step=_kf_step,
    )

    out: list[AssembledLesson] = []
    for lvl in levels:
        pinned = lesson.for_level(lvl)

        def _lvl_step(_s: int, _t: int, label: str) -> None:
            _bump(f"{lvl}: {label}")

        result = assemble_lesson(
            pinned,
            keyframe_renderer=keyframe_renderer,
            overlay_fn=overlay_fn,
            tts_client=tts_client,
            mux_run=mux_run,
            keyframe_paths=keyframe_paths,  # REUSE — no re-render per level
            job_id=job_id,
            lora=lora,
            on_step=_lvl_step,
            out_name=None,
        )
        out.append(result)
    return out


# ── submission (durable job + queue) ─────────────────────────────────────────


def enqueue_lesson(
    manuscript: dict,
    *,
    all_levels: bool = False,
) -> str:
    """Validate a manuscript, persist a ``jobs`` row + the manuscript, queue it.

    Parses + validates the manuscript via :func:`app.lesson.Lesson.parse` (a bad
    manuscript fails BEFORE anything is queued), persists the durable ``jobs`` row
    (``kind='lesson'`` | ``'short'``) with the whole manuscript + the
    ``all_levels`` flag in ``params_json``, persists the manuscript into the
    ``lessons`` table, seeds the live ``queued`` Redis state, and LPUSHes onto the
    LESSON queue. Returns the job id. Raises :class:`AssemblyError` on a bad
    manuscript or :class:`app.jobs.QueueFull`.

    ``all_levels`` (overview only): when true the render produces all three level-
    versions (sharing one keyframe render); else just the single ``level`` the
    manuscript pins (or, for an overview with no level, defaults are applied by
    the route). The taxonomy validation (subject/area/level vs the SoT + the
    narration voice) is the route's job — this layer is taxonomy-free + unit-
    testable, mirroring :func:`app.lesson.Lesson.parse`.
    """
    try:
        lesson = lesson_mod.Lesson.parse(manuscript)
    except lesson_mod.LessonError as e:
        raise AssemblyError(str(e)) from e

    if all_levels and lesson.kind != "lesson":
        raise AssemblyError("all_levels is only valid for a kind='lesson' overview")

    # Shared depth guard (lessons, films, stills share the cap).
    depth = jobs.queue_depth()
    if depth >= jobs.MAX_DEPTH:
        raise jobs.QueueFull(depth, jobs.MAX_DEPTH)

    spec = {
        "manuscript": lesson.as_dict(),
        "all_levels": bool(all_levels),
        "kind": lesson.kind,
    }
    job_id = new_id()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs
                (id, kind, status, subject, area, video_type, title, prompt,
                 params_json)
            VALUES (?, ?, 'queued', ?, ?, NULL, ?, ?, ?)
            """,
            (job_id, lesson.kind, lesson.subject, lesson.area,
             lesson.display_title(), lesson.narration_script()[:2000],
             json.dumps(spec)),
        )
    # Persist the manuscript record + link it to the job (library_id filled later).
    store_manuscript(lesson, job_id=job_id)

    initial = {"job_id": job_id, "status": "queued", "kind": lesson.kind}
    try:
        r = jobs.get_redis()
        r.set(jobs._state_key(job_id), json.dumps(initial), ex=jobs.JOB_STATE_TTL_S)
        r.lpush(LESSON_QUEUE_KEY, job_id)
    except Exception:
        log.exception("enqueue_lesson: redis push failed for %s", job_id)
        raise
    log.info("enqueued %s job %s %s/%s (all_levels=%s, %d beats)",
             lesson.kind, job_id, lesson.subject, lesson.area, all_levels,
             lesson.begrepp_count)
    return job_id


# ── the producer (one queued lesson job) ─────────────────────────────────────


def render_lesson_job(
    job_id: str,
    *,
    keyframe_renderer: Optional["film.KeyframeRenderer"] = None,
    overlay_fn: Optional[caption_mod.OverlayFn] = None,
    tts_client: Optional[tts.PiperClient] = None,
    mux_run: Optional[mux.RunFn] = None,
) -> list[str]:
    """Produce one queued lesson/short job end-to-end; return the library row id(s).

    Loads the job, rebuilds the manuscript, and assembles it — all three level-
    versions when the job's ``all_levels`` is set (an overview; one shared keyframe
    render), else the single pinned version (or a short). Publishes progress for
    the shared ``/api/jobs/<id>/events`` SSE (a lesson is a ``jobs`` row), links
    the produced library video(s) back onto the job + the manuscript record, and
    marks the job ``done``. Returns the produced ``library`` row id(s) (a list:
    three for an all-levels overview, one otherwise).

    On failure writes a ``failed`` state + re-raises :class:`AssemblyError`.
    Collaborators are injected with production defaults; only the keyframe render
    is deferred-until-GPU (the narration + caption + mux are real). The first
    produced library id is mirrored as the job's ``library_id`` so the existing
    SSE / reconcile / library-link plumbing (built for one artefact per job) keeps
    working; all ids are recorded in the terminal state's ``library_ids``.
    """
    job = jobs.get_job_row(job_id)
    if job is None:
        raise AssemblyError(f"lesson job {job_id} not found")
    if job["kind"] not in ("lesson", "short"):
        raise AssemblyError(
            f"render_lesson_job handles kind in (lesson, short); got {job['kind']!r}"
        )

    spec = json.loads(job["params_json"]) if job["params_json"] else {}
    manuscript = spec.get("manuscript")
    if not manuscript:
        raise AssemblyError(f"lesson job {job_id} has no manuscript in params")
    all_levels = bool(spec.get("all_levels"))

    try:
        lesson = lesson_mod.Lesson.parse(manuscript)
    except lesson_mod.LessonError as e:
        raise AssemblyError(f"stored manuscript invalid: {e}") from e

    # Resolve the SFW LoRA backend-side (provenance for the library rows). Never
    # from the manuscript — the SFW guard. Resolved from (subject, the first
    # beat's image_type) the same way a still render resolves it.
    lora = _resolve_lora_for(lesson)

    # Locate the manuscript record so we can fill its library link on success.
    lesson_row_id = _lessons_row_for_job(job_id)

    def _publish(step: int, total: int, label: str) -> None:
        jobs.write_job_state(job_id, {
            "job_id": job_id, "status": "running", "kind": lesson.kind,
            "progress_step": step, "progress_total": total, "stage": label,
        })

    try:
        jobs.write_job_state(job_id, {
            "job_id": job_id, "status": "running", "kind": lesson.kind,
            "progress_step": 0, "progress_total": 1,
        })

        if all_levels and lesson.kind == "lesson":
            results = assemble_levels(
                lesson,
                keyframe_renderer=keyframe_renderer, overlay_fn=overlay_fn,
                tts_client=tts_client, mux_run=mux_run, job_id=job_id, lora=lora,
                on_step=_publish,
            )
        else:
            results = [assemble_lesson(
                lesson,
                keyframe_renderer=keyframe_renderer, overlay_fn=overlay_fn,
                tts_client=tts_client, mux_run=mux_run, job_id=job_id, lora=lora,
                on_step=_publish,
            )]

        library_ids = [r.library_id for r in results]
        primary = library_ids[0]
        total_dur = max((r.duration_s for r in results), default=0.0)
        # Link the manuscript record to the PRIMARY produced video (the first
        # level for an all-levels run; the only one otherwise).
        _link_manuscript(lesson_row_id, library_id=primary)

        jobs.write_job_state(job_id, {
            "job_id": job_id, "status": "done", "kind": lesson.kind,
            "library_id": primary, "library_ids": library_ids,
            "duration_s": int(round(total_dur)),
            "levels": [r.level for r in results],
        })
        log.info("lesson job %s done: %d video(s) %s", job_id, len(library_ids),
                 library_ids)
        return library_ids
    except (AssemblyError, tts.TTSError, mux.MuxError) as e:
        jobs.write_job_state(job_id, {"job_id": job_id, "status": "failed",
                                      "kind": lesson.kind, "error": str(e)})
        raise AssemblyError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        jobs.write_job_state(job_id, {"job_id": job_id, "status": "failed",
                                      "kind": lesson.kind, "error": str(e)})
        raise AssemblyError(str(e)) from e


def _resolve_lora_for(lesson: "lesson_mod.Lesson") -> dict:
    """The SFW LoRA (name, strength) for a lesson's keyframes — backend-resolved.

    Resolved from (subject, the first beat's image_type) via the taxonomy SoT,
    exactly the resolution a still render uses (subject-specific matrix cell → the
    base LoRA for the image type). Never read from the manuscript (the SFW guard).
    Best-effort: a resolution miss yields an empty dict (the row just records no
    LoRA) rather than failing the assembly.
    """
    try:
        from . import taxonomy
        image_type = lesson.beats[0].image_type if lesson.beats else "illustration"
        resolved = taxonomy.resolve_lora(lesson.subject, image_type)
        if isinstance(resolved, dict):
            return {"name": resolved.get("name") or resolved.get("lora"),
                    "strength": resolved.get("strength")}
    except Exception as e:  # noqa: BLE001 — provenance only
        log.warning("_resolve_lora_for failed for %s: %s", lesson.subject, e)
    return {}


def _lessons_row_for_job(job_id: str) -> Optional[str]:
    """The ``lessons`` row id linked to a render job, or None."""
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT id FROM lessons WHERE job_id = ? ORDER BY created_at LIMIT 1",
                (job_id,),
            ).fetchone()
        return row["id"] if row else None
    except Exception as e:  # noqa: BLE001
        log.warning("_lessons_row_for_job failed for %s: %s", job_id, e)
        return None


def run_lesson_worker_loop(stop: Optional[Callable[[], bool]] = None) -> None:  # pragma: no cover
    """Production BRPOP loop for the lesson queue. NOT run during this bead (no GPU).

    Pulls lesson/short job ids off :data:`LESSON_QUEUE_KEY` and produces each. Each
    lesson's keyframes go through the still pipeline under the shared gpu_lock, so
    lessons + films + stills + imagegen coexist on the GPUs. A separate queue key
    keeps the lesson worker's BRPOP loop off the stills lane (a lesson fans out
    into many keyframe renders).
    """
    log.info("lesson worker loop starting. %s", DEFERRED_LIVE_RENDER)
    r = jobs.get_redis()
    while stop is None or not stop():
        popped = r.brpop(LESSON_QUEUE_KEY, timeout=5)
        if popped is None:
            continue
        _key, job_id = popped
        try:
            render_lesson_job(job_id)
        except AssemblyError as e:
            log.error("lesson job %s failed: %s", job_id, e)
