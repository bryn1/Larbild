"""ffmpeg mux — keyframes + Ken-Burns motion + narration → an mp4 (lb-4vw, P6.3).

Turns a rendered storyboard into a playable educational clip:

  * each keyframe still is held on screen for its ``duration_s`` with a
    **Ken-Burns** pan/zoom (the ``zoompan`` filter), the shots are concatenated
    into one silent video track, then
  * the **Swedish narration WAV** (Piper, lb-ayi) is laid over as the audio
    track, and the whole thing is encoded to H.264 + AAC ``.mp4``.

ffmpeg itself is invoked through an **injectable runner** (``run``), so the
command construction + the orchestration are unit-testable with NO ffmpeg / GPU
on the box: tests pass a stub runner that records the argv and writes a stub
mp4. Production uses :func:`_subprocess_run` (the real ``ffmpeg`` binary). The
mux is CPU-only — no GPU — so it is safe to run while the imagegen sweep owns the
GPUs (only the *keyframe render* needs a GPU, and that is mocked/deferred in this
bead, lb-4vw).

The Ken-Burns ``zoompan`` expression is built per keyframe from the template's
:class:`app.film_templates.KenBurns` preset (zoom_from→zoom_to over the shot,
panned in the chosen direction). The video is rendered at a fixed 1280×720 (an
educational 16:9 the still keyframes letterbox into) and ``FILM_FPS`` fps.

SFW-only: ffmpeg assembly of SFW keyframes + SFW narration; no restricted
import, model, or feature.

Env overrides:
  LARBILD_FFMPEG_BIN      default 'ffmpeg'
  LARBILD_FILM_FPS        default 25
  LARBILD_FILM_WIDTH      default 1280
  LARBILD_FILM_HEIGHT     default 720
  LARBILD_FFMPEG_TIMEOUT_S default 600
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

__all__ = [
    "MuxError",
    "FfmpegMissing",
    "Shot",
    "mux_film",
    "build_ffmpeg_command",
    "ffmpeg_available",
]

FFMPEG_BIN = os.environ.get("LARBILD_FFMPEG_BIN", "ffmpeg")
FILM_FPS = int(os.environ.get("LARBILD_FILM_FPS", "25"))
FILM_WIDTH = int(os.environ.get("LARBILD_FILM_WIDTH", "1280"))
FILM_HEIGHT = int(os.environ.get("LARBILD_FILM_HEIGHT", "720"))
FFMPEG_TIMEOUT_S = float(os.environ.get("LARBILD_FFMPEG_TIMEOUT_S", "600"))


class MuxError(RuntimeError):
    """ffmpeg failed, or the inputs to the mux were invalid."""


class FfmpegMissing(MuxError):
    """The ffmpeg binary is not installed/findable.

    Distinct so the pipeline can surface "install ffmpeg" rather than a generic
    mux failure (the live film validation is deferred until both the GPU AND a
    production ffmpeg are available — lb-4vw notes).
    """


@dataclass(frozen=True)
class Shot:
    """One rendered keyframe ready to mux: its image file + how to present it.

    ``image_path`` is an existing still on disk. ``duration_s`` is the hold time.
    ``ken_burns`` is the motion dict ({zoom_from, zoom_to, pan}) from the
    storyboard. Built by the pipeline (lb-4vw) once the keyframe has rendered.
    """

    image_path: Path
    duration_s: float
    ken_burns: dict


# Injected runner signature: (argv, timeout) -> CompletedProcess-ish.
RunFn = Callable[[Sequence[str], float], "subprocess.CompletedProcess"]


def _subprocess_run(argv: Sequence[str], timeout: float):  # pragma: no cover - real ffmpeg
    """Production runner: invoke the real ffmpeg binary."""
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, check=False,
    )


def ffmpeg_available(run: Optional[RunFn] = None) -> bool:
    """True iff ``ffmpeg -version`` runs (the binary is installed)."""
    run = run or _subprocess_run
    try:
        proc = run([FFMPEG_BIN, "-version"], 10.0)
        return getattr(proc, "returncode", 1) == 0
    except Exception:
        return False


def _zoompan_filter(kb: dict, duration_s: float, fps: int,
                    w: int, h: int) -> str:
    """Build the ffmpeg ``zoompan`` expression for one keyframe's Ken-Burns move.

    Linearly interpolates zoom from ``zoom_from`` to ``zoom_to`` over the shot's
    frames and offsets the crop window per the pan direction. ``d`` is the
    number of output frames (duration × fps). A 1.0→1.0 zoom with pan "none" is
    a still hold. The keyframe is first scaled up so the zoom has pixels to work
    with (zoompan upscales from the source), then output at w×h.
    """
    frames = max(int(round(duration_s * fps)), 1)
    zf = float(kb.get("zoom_from", 1.0))
    zt = float(kb.get("zoom_to", 1.0))
    pan = kb.get("pan", "none")

    # z grows/shrinks linearly across `on` (the current output frame, 0..d-1).
    if frames > 1 and zt != zf:
        z_expr = f"{zf}+({zt - zf})*on/{frames - 1}"
    else:
        z_expr = f"{max(zf, zt)}"  # constant zoom (still or pure pan)

    # Pan: move the crop centre. Defaults centre the window; a direction drifts
    # it across the shot. Expressions are in zoompan's iw/ih (input) space.
    x_expr = "iw/2-(iw/zoom/2)"
    y_expr = "ih/2-(ih/zoom/2)"
    drift = "on/" + str(frames - 1 if frames > 1 else 1)
    if pan == "right":
        x_expr = f"(iw-iw/zoom)*{drift}"
    elif pan == "left":
        x_expr = f"(iw-iw/zoom)*(1-{drift})"
    elif pan == "down":
        y_expr = f"(ih-ih/zoom)*{drift}"
    elif pan == "up":
        y_expr = f"(ih-ih/zoom)*(1-{drift})"
    # "in"/"out"/"none" keep the window centred; the zoom term does the motion.

    # Upscale source 2x before zoompan so the pan/zoom samples a higher-res
    # frame (avoids shimmering), then constrain output to w×h @ fps.
    return (
        f"scale={w * 2}:{h * 2},"
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':"
        f"d={frames}:s={w}x{h}:fps={fps},"
        f"setsar=1"
    )


def build_ffmpeg_command(
    shots: Sequence[Shot],
    narration_wav: Optional[Path],
    out_path: Path,
    *,
    fps: int = FILM_FPS,
    width: int = FILM_WIDTH,
    height: int = FILM_HEIGHT,
) -> list[str]:
    """Construct the ffmpeg argv that muxes ``shots`` (+ narration) into an mp4.

    One ``-loop 1 -t <dur> -i <image>`` input per keyframe, a per-shot Ken-Burns
    ``zoompan`` in the filter graph, ``concat`` into one video stream, then (if
    present) the narration WAV as the audio input mapped to the output. H.264
    (``yuv420p`` for broad playback) + AAC, ``-shortest`` so a slightly-long
    audio track doesn't extend past the visuals. Pure command construction — no
    process is started here (that's :func:`mux_film`), so it is unit-testable.
    """
    if not shots:
        raise MuxError("no keyframes to mux")

    argv: list[str] = [FFMPEG_BIN, "-y"]
    for shot in shots:
        argv += ["-loop", "1", "-t", f"{shot.duration_s:.3f}", "-i", str(shot.image_path)]

    audio_input_index: Optional[int] = None
    if narration_wav is not None:
        audio_input_index = len(shots)
        argv += ["-i", str(narration_wav)]

    # Per-shot Ken-Burns filter, labelled [v0]..[vN], then concat.
    filters = []
    for i, shot in enumerate(shots):
        kb = _zoompan_filter(shot.ken_burns, shot.duration_s, fps, width, height)
        filters.append(f"[{i}:v]{kb}[v{i}]")
    concat_inputs = "".join(f"[v{i}]" for i in range(len(shots)))
    filters.append(f"{concat_inputs}concat=n={len(shots)}:v=1:a=0[vout]")
    argv += ["-filter_complex", ";".join(filters), "-map", "[vout]"]

    if audio_input_index is not None:
        argv += ["-map", f"{audio_input_index}:a"]

    argv += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-profile:v", "high", "-preset", "medium",
    ]
    if audio_input_index is not None:
        argv += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
    argv += [str(out_path)]
    return argv


def mux_film(
    shots: Sequence[Shot],
    narration_wav: Optional[Path],
    out_path: Path,
    *,
    run: Optional[RunFn] = None,
    fps: int = FILM_FPS,
    width: int = FILM_WIDTH,
    height: int = FILM_HEIGHT,
    timeout_s: float = FFMPEG_TIMEOUT_S,
) -> Path:
    """Mux the rendered storyboard into an mp4 at ``out_path``; return that path.

    Builds the ffmpeg command (:func:`build_ffmpeg_command`) and runs it via the
    injected ``run`` (production = the real ffmpeg subprocess; tests = a stub
    that records argv + writes a stub mp4). Validates that every keyframe image
    exists first, mkdirs the output parent, and raises :class:`MuxError` /
    :class:`FfmpegMissing` on a non-zero exit or a missing binary. On success the
    mp4 exists on disk.
    """
    run = run or _subprocess_run
    if not shots:
        raise MuxError("no keyframes to mux")
    for shot in shots:
        if not Path(shot.image_path).is_file():
            raise MuxError(f"keyframe image not found: {shot.image_path}")
    if narration_wav is not None and not Path(narration_wav).is_file():
        raise MuxError(f"narration wav not found: {narration_wav}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    argv = build_ffmpeg_command(shots, narration_wav, out_path,
                                fps=fps, width=width, height=height)
    try:
        proc = run(argv, timeout_s)
    except FileNotFoundError as e:
        raise FfmpegMissing(f"ffmpeg binary {FFMPEG_BIN!r} not found: {e}") from e

    rc = getattr(proc, "returncode", 1)
    if rc != 0:
        err = (getattr(proc, "stderr", "") or "")[-500:]
        raise MuxError(f"ffmpeg exited {rc}: {err}")
    if not out_path.is_file():
        raise MuxError(f"ffmpeg reported success but no output at {out_path}")
    return out_path
