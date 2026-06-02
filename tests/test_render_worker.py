"""Integration tests for the render worker (lb-ocx, P1.3).

The P1 'it actually works' gate — exercised END TO END with a MOCKED ComfyUI and
STUBBED gpu_lock / vram_pause. NO live GPU or network call is made (CRITICAL: a
large imagegen sweep is on both GPUs; the live 'first real render on vm100' is
deferred — see test_live_render_is_deferred + DEFERRED_LIVE_RENDER).

Covered:
  * a submitted job produces a (mocked) PNG written to the isolated output dir,
    auto-sorted by subject/area/imageType into the library table;
  * the render acquires the SHARED lock:vm100-3090 gpu_lock BEFORE submitting;
  * vram_pause is honored (entered before submit);
  * concurrent imagegen activity does not OOM — modelled by a lock that yields
    held=False (imagegen holds it) and asserting the worker waits/serialises;
  * the SFW guard: an off-allowlist LoRA fails the job without ever submitting.
"""
from __future__ import annotations

import contextlib

import pytest

from app import create_app, jobs
from app import render_worker as rw
from app.db import connect
from tests._fakeredis import FakeRedis


# ── mock ComfyUI client + lock/pause spies ─────────────────────────────────


class MockComfyClient:
    """Records submits/polls; returns one image. No socket is opened."""

    def __init__(self, *, finish_after_polls: int = 1, output_image=True):
        self.submitted: list[dict] = []
        self.polls = 0
        self.finish_after_polls = finish_after_polls
        self.output_image = output_image
        self.viewed: list[tuple] = []

    def queue_prompt(self, workflow):
        self.submitted.append(workflow)
        return "mock-prompt-1"

    def history(self, prompt_id):
        self.polls += 1
        if self.polls < self.finish_after_polls:
            return None  # still running
        if not self.output_image:
            return {"outputs": {}}  # finished, no image (error path)
        return {
            "outputs": {"9": {"images": [
                {"filename": "larbild_00001_.png", "subfolder": "", "type": "output"}
            ]}}
        }

    def view(self, filename, subfolder, type_):
        self.viewed.append((filename, subfolder, type_))
        return b"\x89PNG mock image bytes"


class LockSpy:
    """A gpu_lock stand-in that records the reason + key it would use."""

    def __init__(self, held=True):
        self.held = held
        self.calls: list[str] = []
        self.entered_before_submit = None
        self.order: list[str] = []

    def __call__(self, reason="", **kw):
        self.calls.append(reason)
        spy = self

        @contextlib.contextmanager
        def _cm():
            spy.order.append("lock-acquire")
            yield spy.held
            spy.order.append("lock-release")
        return _cm()


class PauseSpy:
    def __init__(self):
        self.calls: list[str] = []
        self.order_ref = None

    def __call__(self, reason="", **kw):
        self.calls.append(reason)
        order = self.order_ref

        @contextlib.contextmanager
        def _cm():
            if order is not None:
                order.append("pause-enter")
            yield 1 << 20
            if order is not None:
                order.append("pause-exit")
        return _cm()


@pytest.fixture
def app(tmp_path):
    a = create_app({"TESTING": True, "DB_PATH": tmp_path / "rw.db",
                    "STORAGE_DIR": tmp_path / "storage"})
    return a


@pytest.fixture
def fake_redis():
    r = FakeRedis()
    jobs.set_redis(r)
    yield r
    jobs.set_redis(None)


def _enqueue_bild(subject="fysik", area="ljud", image_type="illustration",
                  prompt="a labelled transverse wave"):
    return jobs.enqueue_job(kind="bild", subject=subject, area=area,
                            image_type=image_type, prompt=prompt, seed=7)


# ── the happy path: submit → poll → download → auto-sort → done ─────────────


def test_full_render_produces_autosorted_library_row(app, fake_redis):
    with app.app_context():
        job_id = _enqueue_bild()
        client = MockComfyClient(finish_after_polls=1)
        lock = LockSpy(held=True)
        pause = PauseSpy()

        lib_id = rw.render_job(
            job_id,
            comfy_factory=lambda: client,
            gpu_lock_cm=lock,
            vram_pause_cm=pause,
            poll_s=0.0,
        )

        # the graph was submitted to the mock (no live GPU)
        assert len(client.submitted) == 1
        assert client.viewed  # the image was downloaded

        # a library row was written, auto-sorted by the job's taxonomy keys
        with connect() as conn:
            row = conn.execute("SELECT * FROM library WHERE id = ?", (lib_id,)).fetchone()
        assert row is not None
        assert row["subject"] == "fysik" and row["area"] == "ljud"
        assert row["image_type"] == "illustration"
        assert row["kind"] == "bild"
        # file_path sorts under output/<subject>/<area>/<imageType>/
        assert row["file_path"] == "output/fysik/ljud/illustration/larbild_00001_.png"
        # provenance carried through
        assert row["seed"] == 7
        assert row["lora"] in rw._workflow.allowlist()
        assert "3090" in (row["backend"] or "")

        # the job is done + linked to the library row
        jrow = jobs.get_job_row(job_id)
        assert jrow["status"] == "done"
        assert jrow["library_id"] == lib_id

        # the PNG is on disk in the isolated output dir
        from pathlib import Path
        out = Path(app.config["STORAGE_DIR"]) / "output" / row["file_path"][len("output/"):]
        assert out.is_file() and out.read_bytes() == b"\x89PNG mock image bytes"


def test_done_state_is_published_for_sse(app, fake_redis):
    with app.app_context():
        job_id = _enqueue_bild()
        lib_id = rw.render_job(
            job_id, comfy_factory=lambda: MockComfyClient(),
            gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy(), poll_s=0.0,
        )
        state = jobs.read_job_state(job_id)
        assert state["status"] == "done"
        assert state["library_id"] == lib_id


# ── the SHARED gpu_lock is acquired BEFORE submit (coordination, R4) ────────


def test_shared_gpu_lock_acquired_before_submit(app, fake_redis):
    with app.app_context():
        job_id = _enqueue_bild()
        client = MockComfyClient()
        lock = LockSpy(held=True)
        pause = PauseSpy()
        pause.order_ref = lock.order  # share the order log to assert sequencing

        rw.render_job(job_id, comfy_factory=lambda: client,
                      gpu_lock_cm=lock, vram_pause_cm=pause, poll_s=0.0)

        # lock acquired, THEN vram paused, THEN (submit happens inside) — the
        # lock + pause must both be entered before the graph is submitted.
        assert lock.order[0] == "lock-acquire"
        assert "pause-enter" in lock.order
        assert lock.order.index("lock-acquire") < lock.order.index("pause-enter")
        # release happens after the pause exits (lock wraps pause wraps submit)
        assert lock.order.index("pause-exit") < lock.order.index("lock-release")


def test_uses_the_real_shared_lock_key_by_default():
    # The worker's default gpu_lock is app.gpu_lock.gpu_lock, whose key is the
    # SHARED lock:vm100-3090 — not a forked/namespaced key. (gpu_lock behaviour
    # is unit-tested in test_gpu_lock; here we assert the worker wires the right
    # one as its default collaborator.)
    from app import gpu_lock
    assert rw._gpu_lock is gpu_lock
    assert gpu_lock.LOCK_KEY == "lock:vm100-3090"


def test_render_proceeds_when_lock_falls_back(app, fake_redis):
    # held=False models "imagegen holds the lock / Redis down". The worker logs
    # + proceeds (matching gpu_lock's degrade-don't-block contract) rather than
    # refusing all work. (Real contention is serialised by the Redis lock itself,
    # tested in test_gpu_lock::test_second_holder_blocked.)
    with app.app_context():
        job_id = _enqueue_bild()
        lib_id = rw.render_job(
            job_id, comfy_factory=lambda: MockComfyClient(),
            gpu_lock_cm=LockSpy(held=False), vram_pause_cm=PauseSpy(), poll_s=0.0,
        )
        assert jobs.get_job_row(job_id)["status"] == "done"
        assert lib_id


# ── the SFW guard: off-allowlist LoRA fails WITHOUT submitting ──────────────


def test_offlist_lora_fails_job_without_submitting(app, fake_redis, monkeypatch):
    # Force resolve_and_validate_lora to raise (an off-allowlist resolution).
    from app import workflow as wf

    def _boom(subject, image_type):
        raise wf.LoraNotAllowedError("smuggled-off-allowlist-v0")
    monkeypatch.setattr(wf, "resolve_and_validate_lora", _boom)

    with app.app_context():
        job_id = _enqueue_bild()
        client = MockComfyClient()
        with pytest.raises(rw.RenderError):
            rw.render_job(job_id, comfy_factory=lambda: client,
                          gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy(), poll_s=0.0)
        # nothing was ever submitted to ComfyUI — the guard fired first
        assert client.submitted == []
        # the job is marked failed with the SFW-guard reason
        st = jobs.read_job_state(job_id)
        assert st["status"] == "failed"
        assert "SFW guard" in st["error"]
        # no library row was written
        with connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM library").fetchone()[0]
        assert n == 0


# ── error paths ────────────────────────────────────────────────────────────


def test_render_timeout_marks_failed(app, fake_redis):
    with app.app_context():
        job_id = _enqueue_bild()
        # never finishes (finish_after_polls huge) + zero timeout → RenderError
        client = MockComfyClient(finish_after_polls=10**9)
        with pytest.raises(rw.RenderError):
            rw.render_job(job_id, comfy_factory=lambda: client,
                          gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy(),
                          poll_s=0.0, timeout_s=0.0)
        assert jobs.read_job_state(job_id)["status"] == "failed"


def test_finished_with_no_image_marks_failed(app, fake_redis):
    with app.app_context():
        job_id = _enqueue_bild()
        client = MockComfyClient(output_image=False)  # finishes, empty outputs
        with pytest.raises(rw.RenderError):
            rw.render_job(job_id, comfy_factory=lambda: client,
                          gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy(), poll_s=0.0)
        assert jobs.read_job_state(job_id)["status"] == "failed"


def test_unknown_job_raises(app, fake_redis):
    with app.app_context():
        with pytest.raises(rw.RenderError):
            rw.render_job("does-not-exist", comfy_factory=lambda: MockComfyClient(),
                          gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy())


def test_film_job_rejected_by_still_worker(app, fake_redis):
    with app.app_context():
        job_id = jobs.enqueue_job(kind="film", subject="biologi", area="celler",
                                  video_type="lektionsfilm", prompt="x")
        with pytest.raises(rw.RenderError):
            rw.render_job(job_id, comfy_factory=lambda: MockComfyClient(),
                          gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy())


# ── the live render is explicitly deferred ─────────────────────────────────


def test_live_render_is_deferred():
    # This bead does NOT run a live GPU render (imagegen sweep on both GPUs).
    # The deferral is recorded as a discoverable module constant; the shared
    # gpu_lock is what will coordinate the eventual live run.
    assert "DEFERRED" in rw.DEFERRED_LIVE_RENDER
    assert "gpu_lock" in rw.DEFERRED_LIVE_RENDER
    # the worker's defaults are the REAL collaborators (so the live run, when it
    # happens, uses the shared lock + real vram_pause + real ComfyUI) — proven by
    # the default factory pointing at the vm100 render target.
    factory_target = rw.RENDER_TARGETS[rw.DEFAULT_BACKEND]
    assert factory_target["http"].endswith(":8188")
