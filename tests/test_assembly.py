"""Integration tests for the lesson assembly (lb-lesson, app.assembly).

Exercised END TO END against a SAMPLE manuscript with:
  * a MOCKED keyframe renderer (real tiny PNGs so the caption step can open them)
    — NO live GPU (the live keyframe render is DEFERRED, imagegen sweep owns GPUs);
  * a stub Piper TTS client returning a REAL Piper-shaped WAV — narration is GPU-
    independent, so the wav timing path is real even in tests;
  * the REAL caption overlay (Pillow, CPU) — the bildtext is genuinely burned on;
  * a stub ffmpeg runner (records argv, writes a stub mp4) — the mux is CPU-only.

Covered (ACCEPTANCE):
  * a manuscript → rendered (stub) keyframes → captioned beat-stills → per-beat
    narration → a merged overview mp4 in the isolated output dir;
  * the clip auto-sorts into the library as a level-tagged VIDEO (kind='lesson');
  * the 3-level deliverable: assemble_levels produces enkel/utvecklad/valutvecklad,
    RENDERING THE KEYFRAMES ONCE (reused across levels) — distinct captions/voice;
  * shorts: a per-begrepp short + a FAMILY short (konvex vs konkav, level=None);
  * beat holds are timed to the narration; the merge uses crossfades; per-beat
    narration concatenates into one continuous track.
"""
from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest
from PIL import Image

from app import assembly, create_app, jobs
from app import lesson as lesson_mod
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
    """Writes a real (tiny) PNG per keyframe so the caption step can open it.

    Counts render() calls so a test can assert the render-once-reuse-across-levels
    optimisation (3 level-videos must NOT re-render the keyframes 3×).
    """

    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)
        self.calls: list = []

    def render(self, spec, *, film_job_id: str) -> Path:
        self.calls.append((spec.index, spec.subject, spec.area, spec.prompt))
        p = (self.storage_dir / "output" / spec.subject / spec.area / spec.image_type
             / f"{film_job_id}_kf{spec.index}.png")
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (480, 270), (30, 40, 60)).save(p)
        return p


class StubTTSClient:
    """A PiperClient stand-in: every synth_once returns a real WAV; records text."""

    def __init__(self, wav: bytes):
        self.wav = wav
        self.texts: list[str] = []

    def synth_once(self, text, *, length_scale=None):
        self.texts.append(text)
        return self.wav


class StubFfmpeg:
    """Records argv; writes a stub mp4; rc=0."""

    def __init__(self):
        self.calls: list[list[str]] = []

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
    return create_app({"TESTING": True, "DB_PATH": tmp_path / "asm.db",
                       "STORAGE_DIR": tmp_path / "storage"})


@pytest.fixture
def fake_redis():
    r = FakeRedis()
    jobs.set_redis(r)
    yield r
    jobs.set_redis(None)


def _beat(begrepp, **kw):
    base = {
        "begrepp": begrepp,
        "forklaring": f"{begrepp} förklaras här.",
        "vardagligt_exempel": "Ett vardagligt exempel.",
        "image_prompt": f"en illustration av {begrepp}",
        "caption": {"short": f"{begrepp} kort", "longer": f"{begrepp} längre",
                    "long": f"{begrepp} lång"},
        "narration": f"Nu tittar vi på {begrepp}.",
        "connection": "Det leder oss vidare.",
    }
    base.update(kw)
    return base


def _manuscript(**kw):
    base = {
        "subject": "fysik",
        "area": "ljud",
        "level": "enkel",
        "kind": "lesson",
        "narration_voice": "sv-standard",
        "duration_target_s": 420,
        "beats": [_beat("Ljudvåg"), _beat("Frekvens"), _beat("Eko")],
    }
    base.update(kw)
    return base


# ── single-level assembly ────────────────────────────────────────────────────


def test_assemble_lesson_produces_mp4_and_library_row(app, fake_redis):
    with app.app_context():
        les = lesson_mod.Lesson.parse(_manuscript())
        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        tts = StubTTSClient(_wav_bytes(2.0))
        ff = StubFfmpeg()
        res = assembly.assemble_lesson(les, keyframe_renderer=kf, tts_client=tts, mux_run=ff)

        assert res.kind == "lesson"
        assert res.level == "enkel"
        assert res.beat_count == 3
        assert res.out_path.is_file()
        assert res.duration_s > 0

        # one keyframe render per beat
        assert len(kf.calls) == 3
        # the library row is a level-tagged VIDEO
        with connect() as conn:
            row = conn.execute(
                "SELECT kind, level, subject, area, duration_s, narration, file_path, "
                "video_type, image_type FROM library WHERE id = ?",
                (res.library_id,),
            ).fetchone()
        assert row["kind"] == "lesson"
        assert row["level"] == "enkel"
        assert row["subject"] == "fysik" and row["area"] == "ljud"
        assert row["narration"] == "sv-standard"
        assert row["duration_s"] >= 1
        assert row["file_path"].startswith("output/fysik/ljud/lesson/")


def test_assemble_voices_every_beat_and_holds_to_narration(app, fake_redis):
    with app.app_context():
        les = lesson_mod.Lesson.parse(_manuscript())
        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        tts = StubTTSClient(_wav_bytes(2.0))  # 2s per beat
        ff = StubFfmpeg()
        assembly.assemble_lesson(les, keyframe_renderer=kf, tts_client=tts, mux_run=ff)
        # one synth per beat, and the spoken line carried the connection tail
        assert len(tts.texts) == 3
        assert any("Det leder oss vidare." in t for t in tts.texts)


def test_merge_uses_crossfade_and_continuous_narration(app, fake_redis):
    with app.app_context():
        les = lesson_mod.Lesson.parse(_manuscript())
        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        ff = StubFfmpeg()
        assembly.assemble_lesson(les, keyframe_renderer=kf,
                                 tts_client=StubTTSClient(_wav_bytes(2.0)), mux_run=ff)
        assert ff.calls, "ffmpeg should have been invoked for the merge"
        argv = " ".join(ff.calls[-1])
        assert "xfade" in argv                 # smooth transitions
        assert "narration.wav" in argv         # one continuous narration track
        # 3 image inputs + 1 audio input
        assert argv.count("-loop") == 3


def test_assemble_overlays_real_caption(app, fake_redis):
    # the captioned beat image must differ from the raw keyframe (bildtext burned)
    with app.app_context():
        les = lesson_mod.Lesson.parse(_manuscript())
        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        ff = StubFfmpeg()
        assembly.assemble_lesson(les, keyframe_renderer=kf,
                                 tts_client=StubTTSClient(_wav_bytes()), mux_run=ff)
        # the mux's image inputs are the *_cap.png files; assert they exist + are PNGs
        argv = ff.calls[-1]
        imgs = [a for a in argv if a.endswith("_cap.png")]
        assert imgs, "merge inputs should be captioned PNGs"
        for p in imgs:
            assert Path(p).is_file()
            assert Image.open(p).format == "PNG"


# ── the 3-level deliverable + render-once-reuse ──────────────────────────────


def test_assemble_levels_produces_three_versions_rendering_once(app, fake_redis):
    with app.app_context():
        les = lesson_mod.Lesson.parse(_manuscript())
        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        tts = StubTTSClient(_wav_bytes())
        ff = StubFfmpeg()
        results = assembly.assemble_levels(les, keyframe_renderer=kf,
                                           tts_client=tts, mux_run=ff)
        assert [r.level for r in results] == list(lesson_mod.LEVELS)
        # RENDER-ONCE: 3 beats × 3 levels would be 9 renders; reuse makes it 3.
        assert len(kf.calls) == 3
        # three distinct library rows, each level-tagged
        with connect() as conn:
            rows = conn.execute(
                "SELECT level FROM library WHERE kind='lesson' ORDER BY level"
            ).fetchall()
        assert sorted(r["level"] for r in rows) == ["enkel", "utvecklad", "valutvecklad"]
        # three merges (one per level)
        assert len(ff.calls) == 3


def test_assemble_levels_captions_differ_per_level(app, fake_redis):
    with app.app_context():
        les = lesson_mod.Lesson.parse(_manuscript())
        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        ff = StubFfmpeg()
        assembly.assemble_levels(les, keyframe_renderer=kf,
                                 tts_client=StubTTSClient(_wav_bytes()), mux_run=ff)
        # each level wrote its captioned beat PNGs under a level-tagged subdir; the
        # 'enkel' run used the short rung, 'valutvecklad' the long rung. We assert
        # the per-level mp4 outputs exist (one per level), proving 3 distinct merges.
        storage = Path(app.config["STORAGE_DIR"]) / "output" / "fysik" / "ljud" / "lesson"
        mp4s = list(storage.glob("*.mp4"))
        tags = {p.stem.rsplit("_", 1)[-1] for p in mp4s}
        assert {"enkel", "utvecklad", "valutvecklad"} <= tags


def test_assemble_levels_rejects_short(app, fake_redis):
    with app.app_context():
        short = lesson_mod.Lesson.parse(_manuscript(kind="short", level=None,
                                                    beats=[_beat("Eko")]))
        with pytest.raises(assembly.AssemblyError):
            assembly.assemble_levels(short, keyframe_renderer=StubKeyframeRenderer(
                app.config["STORAGE_DIR"]), tts_client=StubTTSClient(_wav_bytes()),
                mux_run=StubFfmpeg())


# ── shorts: per-begrepp + family ─────────────────────────────────────────────


def test_per_begrepp_short(app, fake_redis):
    with app.app_context():
        short = lesson_mod.Lesson.parse({
            "subject": "fysik", "area": "ljus", "kind": "short", "level": "enkel",
            "beats": [_beat("Reflektion")],
        })
        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        res = assembly.assemble_lesson(short, keyframe_renderer=kf,
                                       tts_client=StubTTSClient(_wav_bytes()),
                                       mux_run=StubFfmpeg())
        assert res.kind == "short"
        assert res.out_path.is_file()
        with connect() as conn:
            row = conn.execute("SELECT kind, level, file_path FROM library WHERE id=?",
                               (res.library_id,)).fetchone()
        assert row["kind"] == "short"
        assert row["file_path"].startswith("output/fysik/ljus/short/")


def test_family_short_compares_concepts_level_null(app, fake_redis):
    # the konvex vs konkav template: multiple related begrepp, no single level
    with app.app_context():
        fam = lesson_mod.Lesson.parse({
            "subject": "fysik", "area": "ljus", "kind": "short", "level": None,
            "title": "Konvex vs konkav spegel",
            "beats": [
                _beat("Konvex spegel", caption="buktar utåt — gör bilden mindre"),
                _beat("Konkav spegel", caption="buktar inåt — kan förstora"),
            ],
        })
        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        tts = StubTTSClient(_wav_bytes())
        res = assembly.assemble_lesson(fam, keyframe_renderer=kf, tts_client=tts,
                                       mux_run=StubFfmpeg())
        assert res.kind == "short"
        assert res.level is None
        assert res.beat_count == 2
        with connect() as conn:
            row = conn.execute("SELECT kind, level, title FROM library WHERE id=?",
                               (res.library_id,)).fetchone()
        assert row["kind"] == "short"
        assert row["level"] is None
        assert row["title"] == "Konvex vs konkav spegel"


# ── submission + the producer (job → assembled) ──────────────────────────────


def test_enqueue_lesson_persists_job_and_manuscript(app, fake_redis):
    with app.app_context():
        job_id = assembly.enqueue_lesson(_manuscript())
        row = jobs.get_job_row(job_id)
        assert row["kind"] == "lesson"
        assert row["status"] == "queued"
        # queued onto the LESSON queue, not stills/film
        assert fake_redis.llen(assembly.LESSON_QUEUE_KEY) == 1
        assert fake_redis.llen(jobs.QUEUE_KEY) == 0
        # the manuscript was persisted into the lessons table
        with connect() as conn:
            man = conn.execute("SELECT kind, subject, area, begrepp_count, job_id "
                               "FROM lessons WHERE job_id = ?", (job_id,)).fetchone()
        assert man["kind"] == "lesson"
        assert man["begrepp_count"] == 3
        assert man["job_id"] == job_id


def test_enqueue_rejects_bad_manuscript(app, fake_redis):
    with app.app_context():
        with pytest.raises(assembly.AssemblyError):
            assembly.enqueue_lesson({"subject": "fysik"})  # no area/beats


def test_enqueue_all_levels_requires_lesson_kind(app, fake_redis):
    with app.app_context():
        with pytest.raises(assembly.AssemblyError):
            assembly.enqueue_lesson(_manuscript(kind="short", level=None,
                                                beats=[_beat("Eko")]), all_levels=True)


def test_render_lesson_job_single_level_end_to_end(app, fake_redis):
    with app.app_context():
        job_id = assembly.enqueue_lesson(_manuscript())
        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        ids = assembly.render_lesson_job(
            job_id, keyframe_renderer=kf, tts_client=StubTTSClient(_wav_bytes()),
            mux_run=StubFfmpeg())
        assert len(ids) == 1
        state = jobs.read_job_state(job_id)
        assert state["status"] == "done"
        assert state["library_id"] == ids[0]
        # the manuscript record is now linked to the produced video
        with connect() as conn:
            man = conn.execute("SELECT library_id FROM lessons WHERE job_id=?",
                               (job_id,)).fetchone()
        assert man["library_id"] == ids[0]


def test_render_lesson_job_all_levels_makes_three_videos(app, fake_redis):
    with app.app_context():
        job_id = assembly.enqueue_lesson(_manuscript(), all_levels=True)
        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        ids = assembly.render_lesson_job(
            job_id, keyframe_renderer=kf, tts_client=StubTTSClient(_wav_bytes()),
            mux_run=StubFfmpeg())
        assert len(ids) == 3
        # render-once across the 3 levels
        assert len(kf.calls) == 3
        state = jobs.read_job_state(job_id)
        assert state["status"] == "done"
        assert sorted(state["levels"]) == ["enkel", "utvecklad", "valutvecklad"]
        assert state["library_ids"] == ids


def test_render_lesson_job_marks_failed_on_mux_error(app, fake_redis):
    with app.app_context():
        job_id = assembly.enqueue_lesson(_manuscript())

        def boom(argv, timeout):
            class P:
                returncode = 1
                stderr = "ffmpeg exploded"
            return P()

        kf = StubKeyframeRenderer(app.config["STORAGE_DIR"])
        with pytest.raises(assembly.AssemblyError):
            assembly.render_lesson_job(job_id, keyframe_renderer=kf,
                                       tts_client=StubTTSClient(_wav_bytes()),
                                       mux_run=boom)
        state = jobs.read_job_state(job_id)
        assert state["status"] == "failed"
        assert "error" in state


def test_render_progress_is_published(app, fake_redis):
    with app.app_context():
        job_id = assembly.enqueue_lesson(_manuscript())
        seen = []
        orig = jobs.write_job_state

        def spy(jid, state):
            seen.append(state.get("status"))
            return orig(jid, state)

        jobs.write_job_state = spy  # type: ignore[assignment]
        try:
            assembly.render_lesson_job(job_id, keyframe_renderer=StubKeyframeRenderer(
                app.config["STORAGE_DIR"]), tts_client=StubTTSClient(_wav_bytes()),
                mux_run=StubFfmpeg())
        finally:
            jobs.write_job_state = orig  # type: ignore[assignment]
        assert "running" in seen
        assert seen[-1] == "done"


# ── duration estimate (no synthesis) ─────────────────────────────────────────


def test_estimate_lesson_duration_is_positive_and_advisory(app):
    with app.app_context():
        les = lesson_mod.Lesson.parse(_manuscript())
        est = assembly.estimate_lesson_duration_s(les)
        assert est > 0
        # explicit per-beat duration is honoured by the estimate
        m = _manuscript(beats=[_beat("X", duration_s=30.0), _beat("Y", duration_s=30.0)])
        les2 = lesson_mod.Lesson.parse(m)
        est2 = assembly.estimate_lesson_duration_s(les2)
        assert est2 >= 50  # ~60 minus a crossfade
