"""GPU telemetry + render-target registry — forked for Lärbild (lb-c2r).

A SFW-stripped, self-contained fork of imagegen's ``sibbis-app/api_gpus.py``
(ig-bv3). Polls per-GPU stats over SSH on a background daemon thread and caches
the latest snapshot; the HTTP routes are fan-out only, so a slow SSH never
stalls a connected browser tab.

Two render targets are defined here — the registry the P4 ``auto`` router
(lb-hj3) will consume; in P1 we only surface telemetry for them:

  * ``vm100`` — RTX 3090 (CUDA), ws://192.168.5.230:8188, nvidia-smi telemetry.
  * ``vm153`` — Intel B70 (Vulkan/Xe), ws://192.168.5.123:8188, intel_gpu_top
    telemetry (best-effort; the B70 may report ``stale=true`` if intel_gpu_top
    doesn't yet support the Xe driver — that is an expected, documented state).

Differences from the imagegen original (deliberate fork, not a copy):
  * No ``import server`` global coupling — the cached state, locks, and tunables
    live in THIS module and are read directly, so the blueprint is testable
    without a monolith ``server`` module. Tests monkeypatch
    ``app.gpu_telemetry.<attr>`` (the poller fns, the SSE counter/timeout).
  * ``queue_depth`` / ``current_job_id`` overlays are sourced from the Lärbild
    Redis queue (app/jobs.py) when available, but degrade to 0/None if the queue
    module isn't wired yet (P1.2 lands it) — so this module stands alone in P1.1.

SFW-only: pure GPU stat collection + fan-out; no restricted import, model, route.

Env overrides:
  LARBILD_GPU_TELEMETRY_INTERVAL_S      default 1.0
  LARBILD_GPU_TELEMETRY_SSH_TIMEOUT_S   default 3.0
  LARBILD_GPU_VM100_SSH                 default a3090@192.168.5.230
  LARBILD_GPU_VM153_SSH                 default b70@192.168.5.123
  LARBILD_GPU_VM153_VRAM_TOTAL_MB       default 32768
  LARBILD_GPU_STREAM_MAX_CONN           default 10
  LARBILD_GPU_STREAM_IDLE_TIMEOUT_S     default 1800
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Callable, Optional

from flask import Blueprint, Response, jsonify, stream_with_context

gpu_bp = Blueprint("gpu_telemetry", __name__)

__all__ = ["gpu_bp", "snapshot", "start_telemetry_daemon", "RENDER_TARGETS"]

TELEMETRY_INTERVAL_S = float(os.environ.get("LARBILD_GPU_TELEMETRY_INTERVAL_S", "1.0"))
TELEMETRY_SSH_TIMEOUT_S = float(os.environ.get("LARBILD_GPU_TELEMETRY_SSH_TIMEOUT_S", "3.0"))
VM100_SSH = os.environ.get("LARBILD_GPU_VM100_SSH", "a3090@192.168.5.230")
VM153_SSH = os.environ.get("LARBILD_GPU_VM153_SSH", "b70@192.168.5.123")
VM153_VRAM_TOTAL_MB = int(os.environ.get("LARBILD_GPU_VM153_VRAM_TOTAL_MB", str(32 * 1024)))

# The render-target registry the P4 `auto` router (lb-hj3 / lb-bat) consumes.
# `ws` is the ComfyUI WebSocket the bridge connects to; `backend` is the label
# stamped on a library row. In P1 only vm100 actually renders (lb-ocx); vm153 is
# listed so its telemetry pill renders and P4 can flip it on without a schema
# change.
RENDER_TARGETS: dict[str, dict] = {
    "vm100": {"ws": "ws://192.168.5.230:8188", "http": "http://192.168.5.230:8188",
              "label": "vm100 · 3090", "accel": "cuda"},
    "vm153": {"ws": "ws://192.168.5.123:8188", "http": "http://192.168.5.123:8188",
              "label": "vm153 · B70", "accel": "vulkan"},
}

# SSE leak protection: cap concurrent streams + idle-timeout a long-lived tab so
# a lost TCP FIN doesn't pin a slot forever.
STREAM_MAX_CONNECTIONS = int(os.environ.get("LARBILD_GPU_STREAM_MAX_CONN", "10"))
STREAM_IDLE_TIMEOUT_S = float(os.environ.get("LARBILD_GPU_STREAM_IDLE_TIMEOUT_S", str(30 * 60)))
_stream_count_lock = threading.Lock()
_stream_active_count = 0

_state_lock = threading.Lock()
_state: dict[str, dict] = {
    "vm100": {
        "util": 0, "vram_used_mb": 0, "vram_total_mb": 24 * 1024,
        "power_w": 0.0, "temp_c": 0, "queue_depth": 0,
        "current_job_id": None, "stale": True, "last_update": None, "error": None,
    },
    "vm153": {
        "util": 0, "vram_used_mb": 0, "vram_total_mb": VM153_VRAM_TOTAL_MB,
        "power_w": 0.0, "temp_c": 0, "queue_depth": 0,
        "current_job_id": None, "stale": True, "last_update": None, "error": None,
    },
}


def _ssh_cmd(target: str, remote_cmd: str) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(TELEMETRY_SSH_TIMEOUT_S)}",
        "-o", "StrictHostKeyChecking=accept-new",
        target,
        remote_cmd,
    ]


def _poll_vm100_nvidia_smi() -> dict:
    """Run nvidia-smi on VM100. Raises subprocess/ValueError on failure."""
    remote = (
        "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,"
        "power.draw,temperature.gpu --format=csv,noheader,nounits"
    )
    proc = subprocess.run(
        _ssh_cmd(VM100_SSH, remote),
        capture_output=True, text=True, timeout=TELEMETRY_SSH_TIMEOUT_S + 2.0,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"nvidia-smi exit {proc.returncode}: {proc.stderr.strip()[:200]}")
    parts = [p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 5:
        raise ValueError(f"unexpected nvidia-smi output: {proc.stdout!r}")
    return {
        "util": int(float(parts[0])),
        "vram_used_mb": int(float(parts[1])),
        "vram_total_mb": int(float(parts[2])),
        "power_w": float(parts[3]),
        "temp_c": int(float(parts[4])),
    }


def _poll_vm153_intel_gpu_top() -> dict:
    """Sample intel_gpu_top once on vm153. Best-effort — the B70 (Xe driver) may
    not be supported, in which case this raises and the cached snapshot stays
    stale (``stale=true`` is the expected state on B70).
    """
    remote = "timeout 2 intel_gpu_top -J -s 1000 2>/dev/null || true"
    proc = subprocess.run(
        _ssh_cmd(VM153_SSH, remote),
        capture_output=True, text=True, timeout=TELEMETRY_SSH_TIMEOUT_S + 3.0,
    )
    text = (proc.stdout or "").strip()
    if not text:
        raise RuntimeError("intel_gpu_top produced no output (B70/Xe likely unsupported)")
    if text.startswith("["):
        text = text[1:].lstrip()
    samples: list[dict] = []
    depth = 0
    buf: list[str] = []
    for ch in text:
        if ch == "{":
            depth += 1
        if depth > 0:
            buf.append(ch)
        if ch == "}":
            depth -= 1
            if depth == 0 and buf:
                try:
                    samples.append(json.loads("".join(buf)))
                except json.JSONDecodeError:
                    pass
                buf = []
    if not samples:
        raise ValueError("no parseable intel_gpu_top samples")
    sample = samples[-1]
    engines = sample.get("engines", {}) or {}
    busies = [float(v.get("busy", 0)) for v in engines.values() if isinstance(v, dict)]
    util = int(sum(busies) / len(busies)) if busies else 0
    power = sample.get("power", {}) or {}
    power_w = float(power.get("Package", 0) or power.get("GPU", 0) or 0)
    return {
        "util": util,
        "vram_used_mb": 0,
        "vram_total_mb": VM153_VRAM_TOTAL_MB,
        "power_w": power_w,
        "temp_c": 0,
    }


# Poller registry — resolved by name in the refresh cycle so tests can
# monkeypatch ``app.gpu_telemetry._poll_vm100_nvidia_smi`` etc.
_POLLERS: dict[str, str] = {
    "vm100": "_poll_vm100_nvidia_smi",
    "vm153": "_poll_vm153_intel_gpu_top",
}


def refresh_once() -> None:
    """Single poll cycle — updates ``_state`` in place. Never raises."""
    now = time.time()
    for vm_key, poller_name in _POLLERS.items():
        poller: Callable[[], dict] = globals()[poller_name]
        try:
            sample = poller()
        except Exception as exc:  # noqa: BLE001 — a down host = stale, not a crash
            with _state_lock:
                _state[vm_key]["stale"] = True
                _state[vm_key]["error"] = str(exc)[:200]
            continue
        with _state_lock:
            slot = _state[vm_key]
            slot.update(sample)
            slot["stale"] = False
            slot["error"] = None
            slot["last_update"] = now


def start_telemetry_daemon() -> threading.Thread:
    """Start the background SSH poll loop. Returns the daemon thread."""
    def _loop() -> None:
        while True:
            try:
                refresh_once()
            except Exception as exc:  # pragma: no cover — defensive
                print(f"gpu telemetry loop error: {exc}")
            time.sleep(TELEMETRY_INTERVAL_S)

    t = threading.Thread(target=_loop, name="larbild-gpu-telemetry", daemon=True)
    t.start()
    return t


def _queue_depth() -> int:
    """Lärbild render-queue depth from Redis, or 0 if the queue isn't wired.

    Late-imports app/jobs.py (lands in P1.2): until then, or if Redis is down,
    we report 0 so this module stands alone in P1.1.
    """
    try:
        from . import jobs  # noqa: WPS433 — late import; optional dependency
        return int(jobs.queue_depth())
    except Exception:  # noqa: BLE001
        return 0


def snapshot() -> dict:
    """Read the cached telemetry and overlay the live Lärbild queue depth.

    Shape::

        {"vm100": {util, vram_used_mb, vram_total_mb, power_w, temp_c,
                   queue_depth, current_job_id, stale, last_update, error},
         "vm153": {... same shape ...},
         "ts": float}
    """
    with _state_lock:
        vm100 = dict(_state["vm100"])
        vm153 = dict(_state["vm153"])
    depth = _queue_depth()
    # The single Lärbild queue feeds whichever target a render lands on; in P1
    # that is vm100, so surface depth there. P4 can split per-target.
    vm100["queue_depth"] = depth
    return {"vm100": vm100, "vm153": vm153, "ts": time.time()}


@gpu_bp.get("/api/gpus")
def api_gpus_snapshot():
    """One-shot GPU telemetry snapshot (JSON). Backs a non-streaming poll."""
    return jsonify(snapshot()), 200


@gpu_bp.get("/api/gpus/stream")
def api_gpus_stream():
    """Server-Sent Events of per-GPU telemetry — one event per second.

    The background daemon is the only thing that hits SSH; this handler is
    fan-out only. Connection lifecycle: a process-wide cap
    (STREAM_MAX_CONNECTIONS → 503 when reached) and a per-stream idle timeout
    (emits one ``event: timeout`` then exits) keep dead tabs from pinning slots;
    a try/finally decrements the counter even on mid-yield disconnect.

    The counter + cap + timeout are read from THIS module's globals so tests can
    monkeypatch ``app.gpu_telemetry.STREAM_IDLE_TIMEOUT_S = 0.0`` /
    ``app.gpu_telemetry._stream_active_count = 0``.
    """
    global _stream_active_count
    with _stream_count_lock:
        if _stream_active_count >= STREAM_MAX_CONNECTIONS:
            return jsonify({
                "error": "too many active GPU telemetry streams",
                "max_connections": STREAM_MAX_CONNECTIONS,
            }), 503
        _stream_active_count += 1

    deadline = time.monotonic() + STREAM_IDLE_TIMEOUT_S
    interval = TELEMETRY_INTERVAL_S

    @stream_with_context
    def generate():
        global _stream_active_count
        try:
            while time.monotonic() < deadline:
                yield f"data: {json.dumps(snapshot())}\n\n"
                time.sleep(interval)
            # Sentinel so the EventSource client tells a server idle-close apart
            # from a transient network blip (which it would auto-reconnect).
            yield "event: timeout\ndata: {}\n\n"
        finally:
            with _stream_count_lock:
                _stream_active_count -= 1

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )
