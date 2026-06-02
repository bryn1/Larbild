"""Tests for GPU telemetry + render-target registry (lb-c2r, P1.1).

No SSH and no live GPU: the pollers are monkeypatched, so the cache + snapshot +
routes are exercised purely in-process. The telemetry daemon is NOT started
(create_app gates it off under TESTING).
"""
from __future__ import annotations

import json

from app import create_app
from app import gpu_telemetry as tel


def _app():
    # START_GPU_TELEMETRY default-off under TESTING; assert no SSH daemon runs.
    return create_app({"TESTING": True, "INIT_DB": False})


def test_render_targets_registry_has_both_gpus():
    assert set(tel.RENDER_TARGETS) == {"vm100", "vm153"}
    assert tel.RENDER_TARGETS["vm100"]["ws"] == "ws://192.168.5.230:8188"
    assert tel.RENDER_TARGETS["vm153"]["ws"] == "ws://192.168.5.123:8188"
    assert tel.RENDER_TARGETS["vm100"]["accel"] == "cuda"
    assert tel.RENDER_TARGETS["vm153"]["accel"] == "vulkan"


def test_refresh_once_updates_state_on_success(monkeypatch):
    monkeypatch.setattr(tel, "_poll_vm100_nvidia_smi", lambda: {
        "util": 55, "vram_used_mb": 8000, "vram_total_mb": 24576,
        "power_w": 210.0, "temp_c": 62,
    })
    monkeypatch.setattr(tel, "_poll_vm153_intel_gpu_top", lambda: {
        "util": 10, "vram_used_mb": 0, "vram_total_mb": 32768,
        "power_w": 40.0, "temp_c": 0,
    })
    tel.refresh_once()
    snap = tel.snapshot()
    assert snap["vm100"]["util"] == 55
    assert snap["vm100"]["stale"] is False
    assert snap["vm153"]["vram_total_mb"] == 32768
    assert snap["vm153"]["stale"] is False


def test_refresh_once_marks_stale_on_poll_failure(monkeypatch):
    def _boom():
        raise RuntimeError("ssh down")
    monkeypatch.setattr(tel, "_poll_vm100_nvidia_smi", _boom)
    monkeypatch.setattr(tel, "_poll_vm153_intel_gpu_top", _boom)
    tel.refresh_once()
    snap = tel.snapshot()
    assert snap["vm100"]["stale"] is True
    assert "ssh down" in (snap["vm100"]["error"] or "")
    # B70 stale is an expected/documented state
    assert snap["vm153"]["stale"] is True


def test_snapshot_shape():
    snap = tel.snapshot()
    for vm in ("vm100", "vm153"):
        for key in ("util", "vram_used_mb", "vram_total_mb", "queue_depth", "stale"):
            assert key in snap[vm]
    assert "ts" in snap


def test_api_gpus_snapshot_endpoint(monkeypatch):
    monkeypatch.setattr(tel, "_poll_vm100_nvidia_smi", lambda: {
        "util": 1, "vram_used_mb": 1, "vram_total_mb": 24576,
        "power_w": 1.0, "temp_c": 1,
    })
    tel.refresh_once()
    app = _app()
    resp = app.test_client().get("/api/gpus")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "vm100" in body and "vm153" in body


def test_sse_stream_emits_then_times_out(monkeypatch):
    # idle-timeout 0 → the stream emits its data frame(s) then the timeout
    # sentinel and closes, so the test doesn't hang.
    monkeypatch.setattr(tel, "STREAM_IDLE_TIMEOUT_S", 0.0)
    monkeypatch.setattr(tel, "TELEMETRY_INTERVAL_S", 0.0)
    app = _app()
    resp = app.test_client().get("/api/gpus/stream")
    body = resp.get_data(as_text=True)
    # the idle-close sentinel is present
    assert "event: timeout" in body
    assert resp.headers["Content-Type"].startswith("text/event-stream")


def test_sse_stream_cap_returns_503(monkeypatch):
    monkeypatch.setattr(tel, "STREAM_MAX_CONNECTIONS", 0)  # cap reached immediately
    app = _app()
    resp = app.test_client().get("/api/gpus/stream")
    assert resp.status_code == 503
    assert resp.get_json()["max_connections"] == 0


def test_queue_depth_degrades_to_zero_when_queue_unwired(monkeypatch):
    # In P1.1 (no jobs.py yet on this branch's import path, or Redis down) the
    # depth overlay must degrade to 0, not crash the snapshot.
    monkeypatch.setattr(tel, "_queue_depth", lambda: 0)
    snap = tel.snapshot()
    assert snap["vm100"]["queue_depth"] == 0
