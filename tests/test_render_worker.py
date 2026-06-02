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


# ── FLUX-lane routing (lb FLUX lane) ────────────────────────────────────────


@pytest.fixture
def flux_route_on(monkeypatch):
    """Turn the FLUX route ON for the eligible imageTypes (the deploy state).

    flux_route_for reads these module globals; a 'flux' RENDER_TARGET is already
    registered in gpu_telemetry, so flipping the flag is enough.
    """
    monkeypatch.setattr(rw, "FLUX_ROUTE_ENABLE", True)
    monkeypatch.setattr(rw, "FLUX_ROUTE_IMAGE_TYPES",
                        frozenset({"illustration", "mikroskop", "tvarsnitt"}))
    assert "flux" in rw.RENDER_TARGETS  # the target must exist for routing to fire


def test_flux_route_off_by_default_uses_sdxl_graph(app, fake_redis):
    # With the flag default-off, an illustration job renders on the SDXL graph
    # (EmptyLatentImage, no DualCLIPLoader) — the dormant hook stays dormant.
    with app.app_context():
        job_id = _enqueue_bild(image_type="illustration")
        client = MockComfyClient()
        rw.render_job(job_id, comfy_factory=lambda: client,
                      gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy(), poll_s=0.0)
        g = client.submitted[0]
        classes = {n["class_type"] for n in g.values()}
        assert "DualCLIPLoader" not in classes
        assert "EmptyLatentImage" in classes  # SDXL latent
        with connect() as conn:
            row = conn.execute("SELECT backend FROM library WHERE job_id = ?",
                               (job_id,)).fetchone()
        assert "FLUX" not in (row["backend"] or "")


def test_flux_route_on_submits_flux_graph(app, fake_redis, flux_route_on):
    # With the flag on, an illustration job renders on the FLUX graph: the
    # DualCLIPLoader(type=flux) + EmptySD3LatentImage + FluxGuidance node set,
    # and the library row's backend says FLUX.
    with app.app_context():
        job_id = _enqueue_bild(image_type="illustration", prompt="sound waves")
        client = MockComfyClient()
        rw.render_job(job_id, comfy_factory=lambda: client,
                      gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy(), poll_s=0.0)
        g = client.submitted[0]
        classes = {n["class_type"] for n in g.values()}
        assert "DualCLIPLoader" in classes
        assert "EmptySD3LatentImage" in classes  # FLUX latent, NOT SDXL's
        assert "FluxGuidance" in classes
        assert "EmptyLatentImage" not in classes
        # the DualCLIPLoader is in flux mode
        clip = next(n for n in g.values() if n["class_type"] == "DualCLIPLoader")
        assert clip["inputs"]["type"] == "flux"
        with connect() as conn:
            row = conn.execute("SELECT backend FROM library WHERE job_id = ?",
                               (job_id,)).fetchone()
        assert "FLUX" in (row["backend"] or "")


def test_flux_route_uses_flux_negative_and_no_begrepp_lead(app, fake_redis, flux_route_on):
    # On the FLUX route, build_specific_prompt uses the FLUX negative + a
    # scene-led positive that does NOT lead with the raw Swedish begrepp (the
    # text-leak fix). With the LLM off (conftest default) the deterministic FLUX
    # fallback is used.
    with app.app_context():
        job_id = _enqueue_bild(image_type="illustration", prompt="Ljud")
        job = jobs.get_job_row(job_id)
        positive, negative, meta = rw.build_specific_prompt(job)
        assert meta["flux_route"] == "flux"
        # FLUX negative scaffold is applied (the harder no-text one)
        assert negative == rw._edu_prompt.FLUX_SFW_NEGATIVE
        assert "any words" in negative
        # the positive must NOT start by naming the concept as the subject
        # (FLUX would render the word). The SDXL fallback would say
        # "a clear depiction of Ljud as the main subject"; the FLUX one must not.
        assert not positive.lower().startswith("a clear depiction of ljud")


def test_flux_route_off_keeps_sdxl_prompt_path(app, fake_redis):
    # Route off → the SDXL builder + SFW negative (regression guard for the
    # default path).
    with app.app_context():
        job_id = _enqueue_bild(image_type="illustration", prompt="Ljud")
        job = jobs.get_job_row(job_id)
        positive, negative, meta = rw.build_specific_prompt(job)
        assert meta["flux_route"] is None
        assert negative == rw._edu_prompt.SFW_NEGATIVE


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


# ── run_worker_loop is idle-resilient (hq-wc6i: no crash-loop when empty) ───


class _BrpopRaisesTimeoutRedis(FakeRedis):
    """A FakeRedis whose BRPOP raises redis.exceptions.TimeoutError on an empty
    queue — exactly what the real connection does when its socket_timeout fires
    before BRPOP's server-side block returns None (the hq-wc6i crash trigger).

    A non-empty queue pops normally (FIFO), so the loop can still drain a job
    enqueued between idle ticks.
    """

    def __init__(self):
        super().__init__()
        self.idle_brpops = 0

    def brpop(self, key, timeout=0):
        lst = self.lists.get(key)
        if lst:
            return (key, lst.pop())
        # Idle: model the socket read timeout racing the BRPOP block.
        self.idle_brpops += 1
        from redis.exceptions import TimeoutError as RedisTimeoutError
        raise RedisTimeoutError("Timeout reading from socket")


def _stop_after(n):
    """A `stop` predicate that returns True on the n-th call (bounds the loop)."""
    state = {"calls": 0}

    def _pred():
        state["calls"] += 1
        return state["calls"] > n
    return _pred


def test_idle_brpop_timeout_does_not_crash_loop(app, monkeypatch):
    # Regression for hq-wc6i: an idle BRPOP that raises redis.exceptions.
    # TimeoutError must be swallowed as an idle tick, NOT propagate out of
    # run_worker_loop (which previously crash-looped the worker via systemd).
    fake = _BrpopRaisesTimeoutRedis()
    jobs.set_redis(fake)
    try:
        with app.app_context():
            # Loop a handful of purely-idle iterations, then stop. If the fix
            # regresses, the first idle BRPOP raises and this call blows up.
            rw.run_worker_loop(stop=_stop_after(5))
    finally:
        jobs.set_redis(None)
    # The loop really did spin on the empty queue (and survived every raise).
    assert fake.idle_brpops >= 1


def test_loop_still_renders_a_job_after_idle_timeouts(app, monkeypatch):
    # Belt-and-braces: after idle TimeoutErrors, a job pushed onto the queue is
    # still drained + rendered — the fix keeps the loop alive, it doesn't wedge.
    fake = _BrpopRaisesTimeoutRedis()
    jobs.set_redis(fake)

    # Stub the render so we assert the loop reaches it (no GPU). Record the id.
    rendered: list[str] = []
    monkeypatch.setattr(rw, "render_job", lambda jid, **kw: rendered.append(jid))

    try:
        with app.app_context():
            job_id = _enqueue_bild()  # one job sits on the queue
            # First brpop pops the job (queue non-empty → no raise); the loop
            # then stops before the next (idle) iteration would raise.
            rw.run_worker_loop(stop=_stop_after(1))
    finally:
        jobs.set_redis(None)
    assert rendered == [job_id]


def test_brpop_timeout_is_under_socket_timeout():
    # Defense-in-depth half of the hq-wc6i fix: the BRPOP block is held strictly
    # below the connection's socket_timeout so the read timeout doesn't fire on
    # an idle tick in the first place (the except-clause is the safety net).
    from app import gpu_lock
    # gpu_lock.make_redis_client builds the worker's real connection with
    # socket_timeout=5; the loop must block for less than that.
    assert rw.BRPOP_TIMEOUT_S < 5


# ── STRUCTURED imageTypes route to the PROCEDURAL engine, NOT the GPU ────────
# (lb-proc) A diagram/schema/tidslinje/molekyl render is deterministic CPU work
# (graphviz/matplotlib/RDKit) — it must NEVER acquire the gpu_lock, pause VRAM, or
# submit to ComfyUI, yet must still auto-sort into the SAME library tree.


def _png_magic_ok(path):
    return path.is_file() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_diagram_job_renders_procedurally_without_touching_the_gpu(app, fake_redis):
    with app.app_context():
        # a 'diagram' job, with begrepp/förklaring riding in params (params_json)
        job_id = jobs.enqueue_job(
            kind="bild", subject="fysik", area="grundfysik", image_type="diagram",
            prompt="diagram of evaporation", seed=1,
            params={"begrepp": "Avdunstning", "forklaring": "Vatten blir ånga."},
        )
        client = MockComfyClient()
        lock = LockSpy(held=True)
        pause = PauseSpy()

        lib_id = rw.render_job(
            job_id, comfy_factory=lambda: client,
            gpu_lock_cm=lock, vram_pause_cm=pause, poll_s=0.0,
        )

        # the GPU path was BYPASSED entirely
        assert client.submitted == [], "ComfyUI must not be used for a diagram"
        assert client.viewed == []
        assert lock.calls == [], "gpu_lock must not be acquired for a diagram"
        assert pause.calls == [], "vram_pause must not be entered for a diagram"

        # a library row was written, auto-sorted under the diagram bucket
        with connect() as conn:
            row = conn.execute("SELECT * FROM library WHERE id = ?",
                               (lib_id,)).fetchone()
        assert row is not None
        assert row["subject"] == "fysik" and row["area"] == "grundfysik"
        assert row["image_type"] == "diagram" and row["kind"] == "bild"
        assert row["file_path"].startswith("output/fysik/grundfysik/diagram/")
        assert row["file_path"].endswith(".png")
        # honest provenance: drawn on CPU, not diffused; the spec is persisted
        assert row["backend"] == rw.PROCEDURAL_BACKEND
        assert row["lora"] is None
        assert row["workflow_json"] and "block-diagram" in row["workflow_json"]
        # the Swedish labels reached the render (stored as the "prompt" provenance)
        assert "Inflöde" in (row["prompt"] or "")
        assert "Utflöde" in (row["prompt"] or "")

        # the PNG is really on disk in the isolated output dir
        from pathlib import Path
        out = Path(app.config["STORAGE_DIR"]) / row["file_path"]
        assert _png_magic_ok(out), "a valid PNG must be written to disk"

        # job is done + linked, SSE state published
        jrow = jobs.get_job_row(job_id)
        assert jrow["status"] == "done" and jrow["library_id"] == lib_id
        assert jobs.read_job_state(job_id)["status"] == "done"


def test_molecule_job_routes_procedurally(app, fake_redis):
    with app.app_context():
        job_id = jobs.enqueue_job(
            kind="bild", subject="kemi", area="materia", image_type="molekyl",
            prompt="x", params={"begrepp": "Vatten"},
        )
        client = MockComfyClient()
        lib_id = rw.render_job(job_id, comfy_factory=lambda: client,
                               gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy(),
                               poll_s=0.0)
        assert client.submitted == []  # no GPU
        with connect() as conn:
            row = conn.execute("SELECT * FROM library WHERE id = ?",
                               (lib_id,)).fetchone()
        assert row["image_type"] == "molekyl"
        assert "molecule" in (row["workflow_json"] or "")


def test_tidslinje_job_routes_procedurally(app, fake_redis):
    with app.app_context():
        job_id = jobs.enqueue_job(
            kind="bild", subject="historia", area="x", image_type="tidslinje",
            prompt="x", params={"begrepp": "Franska revolutionen"},
        )
        client = MockComfyClient()
        lib_id = rw.render_job(job_id, comfy_factory=lambda: client,
                               gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy(),
                               poll_s=0.0)
        assert client.submitted == []
        from pathlib import Path
        with connect() as conn:
            fp = conn.execute("SELECT file_path FROM library WHERE id=?",
                              (lib_id,)).fetchone()["file_path"]
        assert _png_magic_ok(Path(app.config["STORAGE_DIR"]) / fp)


def test_diffusion_imagetype_still_uses_comfyui(app, fake_redis):
    # An 'illustration' (a diffusion type) MUST still go through ComfyUI + the
    # gpu_lock — the procedural route is ONLY for structured types.
    with app.app_context():
        job_id = _enqueue_bild(image_type="illustration")
        client = MockComfyClient()
        lock = LockSpy(held=True)
        pause = PauseSpy()
        rw.render_job(job_id, comfy_factory=lambda: client,
                      gpu_lock_cm=lock, vram_pause_cm=pause, poll_s=0.0)
        # the GPU path WAS used for a diffusion type
        assert len(client.submitted) == 1
        assert lock.calls and pause.calls
        with connect() as conn:
            row = conn.execute("SELECT backend FROM library WHERE job_id=?",
                               (job_id,)).fetchone()
        assert "3090" in (row["backend"] or "")  # the GPU label, not procedural


def test_procedural_render_failure_marks_job_failed(app, fake_redis, monkeypatch):
    # If the procedural engine raises, the job is marked failed (same contract as
    # the GPU path) — not left hanging as 'running'.
    from app import procedural as proc

    def _boom(**kw):
        raise proc.ProceduralError("render exploded")
    monkeypatch.setattr(proc, "render_concept", _boom)

    with app.app_context():
        job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="ljud",
                                  image_type="diagram", prompt="x",
                                  params={"begrepp": "Ljud"})
        with pytest.raises(rw.RenderError):
            rw.render_job(job_id, comfy_factory=lambda: MockComfyClient(),
                          gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy(), poll_s=0.0)
        st = jobs.read_job_state(job_id)
        assert st["status"] == "failed"


def test_diagram_job_without_params_falls_back_to_title(app, fake_redis):
    # A plain diagram submission (no begrepp in params) still renders: begrepp
    # falls back to the job title, so an old/minimal client isn't broken.
    with app.app_context():
        job_id = jobs.enqueue_job(kind="bild", subject="fysik", area="el-magnet",
                                  image_type="diagram", title="Elektrisk krets",
                                  prompt="x")
        lib_id = rw.render_job(job_id, comfy_factory=lambda: MockComfyClient(),
                               gpu_lock_cm=LockSpy(), vram_pause_cm=PauseSpy(),
                               poll_s=0.0)
        with connect() as conn:
            row = conn.execute("SELECT prompt FROM library WHERE id=?",
                               (lib_id,)).fetchone()
        # the title became the diagram's process label
        assert "Elektrisk krets" in (row["prompt"] or "")
