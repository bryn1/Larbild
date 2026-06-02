"""Tests for the ffmpeg mux (lb-4vw, P6.3).

NO real ffmpeg / GPU: the runner is injected. The command construction is
asserted directly, and the orchestration (input validation, output write) is
driven with a stub runner that records argv + writes a stub mp4. Stub PNG
keyframes + a real Piper-shaped WAV stand in for the inputs (per the bead: mux
is tested with stub frames + the narration wav, no GPU).
"""
from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from app import mux


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # a minimal but valid-enough stub; the mux only checks the file exists
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return path


def _wav(path: Path, seconds: float = 1.0, rate: int = 22050) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    path.write_bytes(buf.getvalue())
    return path


def _shots(tmp_path, n=3):
    out = []
    for i in range(n):
        out.append(mux.Shot(
            image_path=_png(tmp_path / f"kf{i}.png"),
            duration_s=4.0 + i,
            ken_burns={"zoom_from": 1.0, "zoom_to": 1.1,
                       "pan": ["in", "right", "left"][i % 3]},
        ))
    return out


class StubRunner:
    """Records argv; writes a stub mp4 to the last arg; returns rc=0."""

    def __init__(self, returncode=0, stderr=""):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        out = Path(argv[-1])
        if self.returncode == 0:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00\x00\x00\x18ftypmp42stub")

        class P:
            returncode = self.returncode
            stderr = self.stderr
        return P()


# ── command construction ────────────────────────────────────────────────────


def test_command_has_one_input_per_keyframe_plus_audio(tmp_path):
    shots = _shots(tmp_path, 3)
    wav = _wav(tmp_path / "narr.wav")
    argv = mux.build_ffmpeg_command(shots, wav, tmp_path / "out.mp4")
    # 3 image inputs (-loop 1 ... -i) + 1 audio input
    assert argv.count("-loop") == 3
    assert argv.count("-i") == 4  # 3 images + 1 wav
    assert str(wav) in argv
    assert argv[-1] == str(tmp_path / "out.mp4")


def test_command_uses_zoompan_per_shot_and_concat(tmp_path):
    shots = _shots(tmp_path, 3)
    argv = mux.build_ffmpeg_command(shots, None, tmp_path / "out.mp4")
    fc = argv[argv.index("-filter_complex") + 1]
    assert fc.count("zoompan") == 3  # one Ken-Burns per keyframe
    assert "concat=n=3" in fc
    assert "[vout]" in fc


def test_command_pan_directions_drift_the_window(tmp_path):
    # right pan moves x positively across the shot; left pan inverts it
    right = mux.Shot(_png(tmp_path / "r.png"), 4.0, {"zoom_from": 1.05, "zoom_to": 1.05, "pan": "right"})
    left = mux.Shot(_png(tmp_path / "l.png"), 4.0, {"zoom_from": 1.05, "zoom_to": 1.05, "pan": "left"})
    fc_r = mux.build_ffmpeg_command([right], None, tmp_path / "o.mp4")
    fc_l = mux.build_ffmpeg_command([left], None, tmp_path / "o.mp4")
    sr = fc_r[fc_r.index("-filter_complex") + 1]
    sl = fc_l[fc_l.index("-filter_complex") + 1]
    assert "(iw-iw/zoom)*on/" in sr      # rightward drift
    assert "(iw-iw/zoom)*(1-" in sl      # leftward drift


def test_command_omits_audio_map_when_no_narration(tmp_path):
    shots = _shots(tmp_path, 2)
    argv = mux.build_ffmpeg_command(shots, None, tmp_path / "out.mp4")
    assert "-c:a" not in argv          # no audio codec
    assert argv.count("-i") == 2       # only the images


def test_command_encodes_h264_yuv420p_aac(tmp_path):
    shots = _shots(tmp_path, 1)
    wav = _wav(tmp_path / "n.wav")
    argv = mux.build_ffmpeg_command(shots, wav, tmp_path / "out.mp4")
    assert "libx264" in argv
    assert "yuv420p" in argv
    assert "aac" in argv
    assert "-shortest" in argv


def test_empty_shots_raises(tmp_path):
    with pytest.raises(mux.MuxError):
        mux.build_ffmpeg_command([], None, tmp_path / "out.mp4")


# ── mux_film orchestration (stub runner) ────────────────────────────────────


def test_mux_film_writes_output_via_runner(tmp_path):
    shots = _shots(tmp_path, 3)
    wav = _wav(tmp_path / "narr.wav")
    out = tmp_path / "films" / "clip.mp4"
    runner = StubRunner()
    result = mux.mux_film(shots, wav, out, run=runner)
    assert result == out
    assert out.is_file()                 # the stub mp4 was written
    assert len(runner.calls) == 1
    assert runner.calls[0][0] == mux.FFMPEG_BIN


def test_mux_film_missing_keyframe_raises(tmp_path):
    shots = [mux.Shot(tmp_path / "does-not-exist.png", 4.0, {"pan": "none"})]
    with pytest.raises(mux.MuxError):
        mux.mux_film(shots, None, tmp_path / "out.mp4", run=StubRunner())


def test_mux_film_missing_narration_raises(tmp_path):
    shots = _shots(tmp_path, 1)
    with pytest.raises(mux.MuxError):
        mux.mux_film(shots, tmp_path / "nope.wav", tmp_path / "out.mp4", run=StubRunner())


def test_mux_film_nonzero_exit_raises(tmp_path):
    shots = _shots(tmp_path, 1)
    runner = StubRunner(returncode=1, stderr="boom: invalid codec")
    with pytest.raises(mux.MuxError) as ei:
        mux.mux_film(shots, None, tmp_path / "out.mp4", run=runner)
    assert "boom" in str(ei.value)


def test_mux_film_missing_binary_raises_ffmpegmissing(tmp_path):
    shots = _shots(tmp_path, 1)

    def _missing(argv, timeout):
        raise FileNotFoundError("ffmpeg: command not found")
    with pytest.raises(mux.FfmpegMissing):
        mux.mux_film(shots, None, tmp_path / "out.mp4", run=_missing)


def test_mux_film_no_output_file_raises(tmp_path):
    # runner returns rc=0 but writes nothing → MuxError (success-but-no-output)
    shots = _shots(tmp_path, 1)

    def _silent(argv, timeout):
        class P:
            returncode = 0
            stderr = ""
        return P()
    with pytest.raises(mux.MuxError):
        mux.mux_film(shots, None, tmp_path / "out.mp4", run=_silent)


def test_ffmpeg_available_reflects_runner(tmp_path):
    class OK:
        returncode = 0
    assert mux.ffmpeg_available(run=lambda a, t: OK()) is True

    def _boom(a, t):
        raise FileNotFoundError()
    assert mux.ffmpeg_available(run=_boom) is False
