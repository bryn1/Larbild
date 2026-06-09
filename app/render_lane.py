"""Render-residency coordination for larbild's GPU render path (hq-jx6g).

larbild's render_worker drains ``larbild:render:task_queue`` and drives ComfyUI
on VM100 :8188 — the SAME shared 3090 imagegen renders on. Post-hq-t4y8 the vm100
dispatcher is the SOLE 3090 residency authority: while a render is pending it
evicts any LLM/compose and keeps ComfyUI resident (renders-first). So a worker
must NOT evict the LLM itself (the old ``vram_pause`` did, which now races the
dispatcher). It signals render pressure, waits for ComfyUI, then holds the shared
gpu_lock for the render — and lets the dispatcher do the model swap.

This mirrors imagegen's ``sibbis-app/render_lane.py`` (the proven hq-gmjb helper);
the logic is identical, wired to larbild's own ``gpu_lock`` / ``jobs.get_redis`` /
``default_comfyui_url``. The two copies should be canonicalised into
``infra/shared/render_lane.py`` + byte-vendored (same as the gpu_lock cluster-1
work) — tracked as a follow-up.

Three ordered steps:
  1. SIGNAL — LPUSH an inert ``{_signal: render_pressure}`` marker onto
     ``RENDER_PRESSURE_KEY`` (the dispatcher counts it in RENDER_QUEUES; nothing
     drains it). render_pressure()>0 keeps the dispatcher evicting LLMs + ComfyUI
     resident THROUGHOUT the render — including AFTER the worker BRPOPs the job
     off ``larbild:render:task_queue`` (so the queue going empty mid-render can't
     let an LLM swap back in). Ignored by the dispatcher's LLM-pressure + idle
     math (hq-lpyb).
  2. WAIT — poll ComfyUI :8188 until it answers, WITHOUT holding the gpu_lock, so
     the dispatcher's ``ensure_resident(comfyui)`` can take the lock to do the
     swap (else it defers on the SAME lock and never restores ComfyUI, and the
     render hits a dead ComfyUI).
  3. HOLD — acquire the shared gpu_lock (``lock:vm100-3090``) for the render so a
     swap can't co-reside with it.

Crash semantics: a worker that dies mid-render leaks its marker — a SAFE-FAIL (a
stale marker only keeps ComfyUI resident, never an OOM); a boot reconcile clears
the key when no render thread can still be running. Fall-forward: a Redis hiccup
or unreachable ComfyUI never REFUSES a render — log loud and run the body anyway
(same contract as gpu_lock's degrade-don't-block).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import requests

from .gpu_lock import gpu_lock
from .jobs import get_redis

log = logging.getLogger("larbild.render_lane")

# MUST match vm100_dispatcher.py RENDER_QUEUES (which already lists this key +
# larbild:render:task_queue + larbild:lesson:task_queue). Render-pressure ONLY —
# nothing BRPOP/BLPOPs it, so markers are never consumed as tasks.
RENDER_PRESSURE_KEY = os.environ.get("RENDER_PRESSURE_KEY", "vm100:render_pressure:external")

# The shared VM100 3090 ComfyUI — one box, one card. Default matches larbild's
# default_comfyui_url; the IMAGE_GEN_COMFYUI_URL override mirrors imagegen's helper
# so a single env can repoint both apps.
COMFYUI_URL = os.environ.get(
    "IMAGE_GEN_COMFYUI_URL",
    os.environ.get("LARBILD_COMFYUI_URL", "http://192.168.5.230:8188"),
)

# How long to wait for the dispatcher to evict an LLM + restore ComfyUI (~15-20s
# for a swap; generous headroom). On timeout we proceed and let the render's own
# submit surface a clear error.
DEFAULT_READY_TIMEOUT_S = float(os.environ.get("RENDER_READY_TIMEOUT_S", "180"))
_READY_POLL_INTERVAL_S = 2.0
_READY_HTTP_TIMEOUT_S = 4.0


def _safe_redis() -> Optional[Any]:
    """Best-effort redis handle; None if unavailable (render still proceeds)."""
    try:
        return get_redis()
    except Exception as e:  # pragma: no cover - connect/config failure path
        log.warning("render_lane: redis unavailable (%s); render pressure not signalled", e)
        return None


def _comfyui_ready(url: str) -> bool:
    """True if ComfyUI answers /system_stats (i.e. is resident + serving)."""
    try:
        r = requests.get(f"{url.rstrip('/')}/system_stats", timeout=_READY_HTTP_TIMEOUT_S)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _wait_for_comfyui(url: str, timeout_s: float) -> bool:
    """Poll ComfyUI until ready or timeout. Returns readiness. Never raises."""
    deadline = time.monotonic() + timeout_s
    waited = False
    while time.monotonic() < deadline:
        if _comfyui_ready(url):
            if waited:
                log.info("render_lane: ComfyUI resident — proceeding with render")
            return True
        waited = True
        log.info("render_lane: waiting for dispatcher to restore ComfyUI (%s)…", url)
        time.sleep(_READY_POLL_INTERVAL_S)
    log.warning(
        "render_lane: ComfyUI not ready after %.0fs — proceeding anyway "
        "(render submit will surface the error if still down)", timeout_s
    )
    return False


@contextmanager
def render_residency(
    reason: str = "",
    *,
    client: Optional[Any] = None,
    comfyui_url: Optional[str] = None,
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
) -> Iterator[bool]:
    """Context manager for a larbild ComfyUI render on the shared 3090.

    Drop-in for the old nested ``with gpu_lock(): with vram_pause():``. Yields the
    gpu_lock ``held`` flag (False on a Redis-down fallback — still run the body; a
    False just means the cross-host swap-coordination was skipped)::

        with render_residency(reason=f"larbild render {job_id}") as held:
            results = _submit_and_wait(comfy, workflow, ...)
    """
    r = client if client is not None else _safe_redis()
    url = comfyui_url or COMFYUI_URL
    marker = json.dumps({"_signal": "render_pressure", "reason": reason,
                         "token": uuid.uuid4().hex})
    pushed = False

    if r is not None:
        try:
            r.lpush(RENDER_PRESSURE_KEY, marker)
            pushed = True
            log.info("render_lane: signalled render pressure (%s)", reason)
        except Exception as e:
            log.warning("render_lane: could not signal render pressure (%s); proceeding", e)

    try:
        # Wait for ComfyUI BEFORE taking the lock (see module docstring step 2).
        _wait_for_comfyui(url, ready_timeout_s)
        with gpu_lock(reason=reason, client=r) as held:
            yield held
    finally:
        if pushed:
            try:
                # LREM the exact marker we pushed (count=1) — leaves any other
                # in-flight worker's markers intact.
                r.lrem(RENDER_PRESSURE_KEY, 1, marker)
            except Exception as e:
                log.warning(
                    "render_lane: could not clear render-pressure marker (%s) — "
                    "dispatcher keeps ComfyUI resident until boot reconcile", e
                )
