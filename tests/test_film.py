"""Integration tests for the film pipeline (lb-4vw, P6.3).

Exercised END TO END with:
  * a MOCKED keyframe renderer (stub PNGs) — NO live GPU (CRITICAL: imagegen
    sweep owns the GPUs; the live keyframe render is DEFERRED);
  * a stub Piper TTS client returning a REAL (Piper-shaped) WAV — narration is
    GPU-independent, so the wav path is real even in tests;
  * a stub ffmpeg runner (records argv, writes a stub mp4) — the mux is CPU-only.

Covered (ACCEPTANCE): a storyboard produces rendered (stub) shots, sv-SE
narration, and a muxed mp4 in the isolated output dir; the clip auto-sorts into
the library as a VIDEO (kind='film') record; the job is done + linked; progress
is published for the SSE; the live keyframe render is documented as deferred.
"""
from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import pytest

from app import create_app, film, jobs
from app.db import connect
from tests._fakeredis import FakeRedis


# ── stub collaborators (no GPU, no VM600, no ffmpeg) ────────────────────────


def _wav_bytes(seconds=1.5, rate=22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class StubKeyframeRenderer:
    """Writes a stub PNG per keyframe under STORAGE_DIR; records the specs."""

    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)
        self.rendered: list = []

    def render(self, spec, *, film_job_id: str) -> Path:
        self.rendered.append(spec)
        p = (self.storage_dir / "output" / spec.subject / spec.area / spec.image_type
             / f"{film_job_id}_kf{spec.index}.png")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        return p


class StubTTSClient:
    """A PiperClient stand-in: every synth_once returns the same real WAV."""

    def __init__(self, wav: bytes):
        self.wav = wav
        self.texts: list[str] = []

    def synth_once(self, text, *, length_scale=None):
        self.texts.append(text)
        return self.wav


class StubFfmpeg:
    """Records argv; writes a stub mp4; rc=0."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        out = Path(argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42stub")

        class P:
            returncode = 0
            stderr = ""
        return P()


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DB_PATH": tmp_path / "film.db",
                       "STORAGE_DIR": tmp_path / "storage"})


@pytest.fixture
def fake_redis():
    r = FakeRedis()
    jobs.set_redis(r)
    yield r
    jobs.set_redis(None)


def _enqueue(app, **kw):
    kw.setdefault("video_type", "lektionsfilm")
    kw.setdefault("subject", "fysik")
    kw.setdefault("area", "ljud")
    kw.setdefault("narration", "Ljud är vågor. Vågorna rör sig genom luften. "
                               "De når till slut ditt öra.")
    return film.enqueue_film(**kw)


# ── enqueue validation ──────────────────────────────────────────────────────


def test_enqueue_persists_film_job_and_storyboard(app, fake_redis):
    with app.app_context():
        job_id = _enqueue(app)
        row = jobs.get_job_row(job_id)
        assert row["kind"] == "film"
        assert row["video_type"] == "lektionsfilm"
        assert row["status"] == "queued"
        spec = json.loads(row["params_json"])
        assert spec["storyboard"]["video_type"] == "lektionsfilm"
        assert len(spec["storyboard"]["shots"]) == 5  # lektionsfilm has 5 shots
        # queued onto the FILM queue (not the stills queue)
        assert fake_redis.llen(film.FILM_QUEUE_KEY) == 1
        assert fake_redis.llen(jobs.QUEUE_KEY) == 0


def test_enqueue_rejects_empty_narration(app, fake_redis):
    with app.app_context():
        with pytest.raises(film.FilmError):
            _enqueue(app, narration="   ")


def test_enqueue_rejects_bad_videotype(app, fake_redis):
    with app.app_context():
        with pytest.raises(film.FilmError):
            _enqueue(app, video_type="not-a-film")


def test_enqueue_rejects_wrong_shot_prompt_count(app, fake_redis):
    with app.app_context():
        with pytest.raises(film.FilmError):
            _enqueue(app, video_type="kort-forklaring",  # 3 shots
                     shot_prompts=["only", "two"])


# ── the happy path: render → narrate → mux → auto-sort VIDEO ────────────────


def test_full_film_produces_autosorted_video_record(app, fake_redis):
    with app.app_context():
        storage = Path(app.config["STORAGE_DIR"])
        job_id = _enqueue(app)
        kr = StubKeyframeRenderer(storage)
        tts_client = StubTTSClient(_wav_bytes())
        ff = StubFfmpeg()

        lib_id = film.render_film(job_id, keyframe_renderer=kr,
                                  tts_client=tts_client, mux_run=ff)

        # every storyboard shot was rendered (mocked, NO GPU)
        assert len(kr.rendered) == 5
        # ffmpeg was invoked once, output is the mp4
        assert len(ff.calls) == 1
        assert ff.calls[0][-1].endswith(".mp4")

        # a VIDEO library row was written, auto-sorted by the film's keys
        with connect() as conn:
            row = conn.execute("SELECT * FROM library WHERE id = ?", (lib_id,)).fetchone()
        assert row is not None
        assert row["kind"] == "film"
        assert row["subject"] == "fysik" and row["area"] == "ljud"
        assert row["video_type"] == "lektionsfilm"
        assert row["image_type"] is None            # films carry video_type, not image_type
        assert row["narration"] == "sv-standard"    # narrationVoices slug
        assert row["duration_s"] and row["duration_s"] > 0
        # file_path sorts under output/<subject>/<area>/film/
        assert row["file_path"] == f"output/fysik/ljud/film/{job_id}.mp4"

        # the mp4 + the narration wav are on disk in the isolated output dir
        mp4 = storage / row["file_path"]
        wav = storage / "output" / "fysik" / "ljud" / "film" / f"{job_id}.wav"
        assert mp4.is_file()
        assert wav.is_file()
        with wave.open(str(wav), "rb") as w:        # the narration is a real WAV
            assert w.getnframes() > 0

        # the film job is done + linked to the library row
        jrow = jobs.get_job_row(job_id)
        assert jrow["status"] == "done"
        assert jrow["library_id"] == lib_id


def test_done_state_published_for_sse(app, fake_redis):
    with app.app_context():
        job_id = _enqueue(app)
        lib_id = film.render_film(
            job_id, keyframe_renderer=StubKeyframeRenderer(Path(app.config["STORAGE_DIR"])),
            tts_client=StubTTSClient(_wav_bytes()), mux_run=StubFfmpeg())
        state = jobs.read_job_state(job_id)
        assert state["status"] == "done"
        assert state["kind"] == "film"
        assert state["library_id"] == lib_id
        assert state["progress_step"] == state["progress_total"]


def test_progress_advances_through_stages(app, fake_redis):
    # The film publishes running states as it renders keyframes / narrates / muxes.
    # FakeRedis keeps only the latest blob, so assert the terminal progress is the
    # full total (each keyframe + narration + mux counted).
    with app.app_context():
        job_id = _enqueue(app, video_type="kort-forklaring")  # 3 shots
        film.render_film(
            job_id, keyframe_renderer=StubKeyframeRenderer(Path(app.config["STORAGE_DIR"])),
            tts_client=StubTTSClient(_wav_bytes()), mux_run=StubFfmpeg())
        st = jobs.read_job_state(job_id)
        assert st["progress_total"] == 3 + 2  # 3 keyframes + narration + mux


def test_lattlast_voice_used_when_requested(app, fake_redis):
    with app.app_context():
        job_id = _enqueue(app, narration_voice="sv-lattlast")
        tts_client = StubTTSClient(_wav_bytes())
        lib_id = film.render_film(
            job_id, keyframe_renderer=StubKeyframeRenderer(Path(app.config["STORAGE_DIR"])),
            tts_client=tts_client, mux_run=StubFfmpeg())
        # lättläst splits per sentence → multiple synth_once calls
        assert len(tts_client.texts) >= 2
        with connect() as conn:
            narr = conn.execute("SELECT narration FROM library WHERE id=?",
                                (lib_id,)).fetchone()[0]
        assert narr == "sv-lattlast"


def test_shot_prompts_override_is_used(app, fake_redis):
    with app.app_context():
        prompts = [f"custom shot {i}" for i in range(3)]
        job_id = _enqueue(app, video_type="kort-forklaring", shot_prompts=prompts)
        kr = StubKeyframeRenderer(Path(app.config["STORAGE_DIR"]))
        film.render_film(job_id, keyframe_renderer=kr,
                         tts_client=StubTTSClient(_wav_bytes()), mux_run=StubFfmpeg())
        rendered_prompts = [s.prompt for s in kr.rendered]
        assert rendered_prompts == prompts


# ── failure paths ───────────────────────────────────────────────────────────


def test_tts_failure_marks_film_failed(app, fake_redis):
    from app import tts as tts_mod

    class BoomTTS:
        def synth_once(self, text, *, length_scale=None):
            raise tts_mod.TTSUnavailable("voice_service down")

    with app.app_context():
        job_id = _enqueue(app)
        with pytest.raises(film.FilmError):
            film.render_film(job_id,
                             keyframe_renderer=StubKeyframeRenderer(Path(app.config["STORAGE_DIR"])),
                             tts_client=BoomTTS(), mux_run=StubFfmpeg())
        assert jobs.read_job_state(job_id)["status"] == "failed"
        # no library row was written
        with connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM library").fetchone()[0] == 0


def test_mux_failure_marks_film_failed(app, fake_redis):
    def _bad_ffmpeg(argv, timeout):
        class P:
            returncode = 1
            stderr = "ffmpeg error"
        return P()

    with app.app_context():
        job_id = _enqueue(app)
        with pytest.raises(film.FilmError):
            film.render_film(job_id,
                             keyframe_renderer=StubKeyframeRenderer(Path(app.config["STORAGE_DIR"])),
                             tts_client=StubTTSClient(_wav_bytes()), mux_run=_bad_ffmpeg)
        assert jobs.read_job_state(job_id)["status"] == "failed"


def test_keyframe_missing_file_marks_failed(app, fake_redis):
    class BadKF:
        def render(self, spec, *, film_job_id):
            return Path("/nonexistent/frame.png")

    with app.app_context():
        job_id = _enqueue(app)
        with pytest.raises(film.FilmError):
            film.render_film(job_id, keyframe_renderer=BadKF(),
                             tts_client=StubTTSClient(_wav_bytes()), mux_run=StubFfmpeg())
        assert jobs.read_job_state(job_id)["status"] == "failed"


def test_render_film_rejects_non_film_job(app, fake_redis):
    with app.app_context():
        bild = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                                image_type="illustration", prompt="x")
        with pytest.raises(film.FilmError):
            film.render_film(bild, keyframe_renderer=StubKeyframeRenderer(Path(app.config["STORAGE_DIR"])),
                             tts_client=StubTTSClient(_wav_bytes()), mux_run=StubFfmpeg())


def test_unknown_film_job_raises(app, fake_redis):
    with app.app_context():
        with pytest.raises(film.FilmError):
            film.render_film("does-not-exist",
                             keyframe_renderer=StubKeyframeRenderer(Path(app.config["STORAGE_DIR"])),
                             tts_client=StubTTSClient(_wav_bytes()), mux_run=StubFfmpeg())


# ── the live keyframe render is explicitly deferred ─────────────────────────


def test_live_keyframe_render_is_deferred():
    assert "DEFERRED" in film.DEFERRED_LIVE_RENDER
    assert "gpu_lock" in film.DEFERRED_LIVE_RENDER
    # the production default renderer reuses the still pipeline (bridge) — proving
    # the live run, when it happens, goes through the shared gpu_lock path.
    assert film.BridgeKeyframeRenderer is not None
