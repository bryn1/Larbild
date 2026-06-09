"""render_lane — the ONE canonical render-residency coordinator (cluster 1, hq-c3ys).

Byte-identical vendored copy across every place that drives a ComfyUI render on the
shared VM100 3090 (imagegen `sibbis-app/` + imagegen `vm100/shared_runtime/` + larbild
`app/`). Like `gpu_lock.py`, each deploy carries a vendored copy refreshed via
`infra/shared/check_vendor_drift.py --vendor` — there is no shared import path across
the apps' separate deploy lineages, so this file MUST stay self-contained (no app
imports) to remain byte-identical everywhere. See `infra/shared/README.md`.

WHAT IT DOES — post-hq-t4y8 the `vm100_dispatcher` is the SOLE 3090 residency authority:
while a render is pending it evicts any LLM/compose and keeps ComfyUI resident
(renders-first). A render driver must therefore NOT evict a model itself (the retired
`vram_pause` did, which races the dispatcher). `render_residency` makes a render safe in
three ordered steps:

  1. SIGNAL — LPUSH an inert ``{_signal: render_pressure}`` marker onto
     ``RENDER_PRESSURE_KEY`` (counted in the dispatcher's RENDER_QUEUES; nothing drains
     it). render_pressure()>0 keeps the dispatcher evicting LLMs + ComfyUI resident
     THROUGHOUT the render — including after a worker BRPOPs its job off its queue.
     Ignored by the dispatcher's LLM-pressure + idle math (hq-lpyb).
  2. WAIT — poll ComfyUI :8188 until it answers, WITHOUT holding the gpu_lock, so the
     dispatcher's ``ensure_resident(comfyui)`` can take the lock to do the swap (else it
     defers on the SAME lock and never restores ComfyUI → the render hits a dead ComfyUI).
  3. HOLD — acquire the SHARED gpu_lock (``lock:vm100-3090``) for the render so a model
     swap can't co-reside with it.

Crash-safe: a driver that dies mid-render leaks its marker — a SAFE-FAIL (a stale marker
only keeps ComfyUI resident, never an OOM); a boot reconcile clears the key. Fall-forward:
a Redis hiccup / unreachable ComfyUI never REFUSES a render — log loud, run the body anyway
(same contract as gpu_lock's degrade-don't-block).

SELF-CONTAINED: redis comes from the vendored ``gpu_lock.make_redis_client`` (env-resolved,
same VM400 server every participant uses); ComfyUI URL + pressure key come from env. The
gpu_lock import tries the flat layout first (sibbis-app / scripts) then the package-relative
layout (larbild `app`, vm100 `shared_runtime`) so the one file works in every vendored tree.
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

try:  # flat layout: sibbis-app/, polecat scripts/, shared_runtime on sys.path
    from gpu_lock import gpu_lock, make_redis_client
except ImportError:  # package-relative layout: larbild app/, imagegen vm100/shared_runtime/
    from .gpu_lock import gpu_lock, make_redis_client

log = logging.getLogger("sibbis.render_lane")

# MUST match vm100_dispatcher.py RENDER_QUEUES — render-pressure ONLY (nothing BRPOPs it).
RENDER_PRESSURE_KEY = os.environ.get("RENDER_PRESSURE_KEY", "vm100:render_pressure:external")

# The shared VM100 3090 ComfyUI. Resolve each app's env prefix in order (imagegen then
# larbild) so one canonical file points every deploy at the right endpoint.
COMFYUI_URL = (
    os.environ.get("IMAGE_GEN_COMFYUI_URL")
    or os.environ.get("LARBILD_COMFYUI_URL")
    or "http://192.168.5.230:8188"
)

# How long to wait for the dispatcher to evict an LLM + restore ComfyUI (~15-20s for a
# swap; generous headroom). On timeout we proceed and let the render's own submit error.
DEFAULT_READY_TIMEOUT_S = float(os.environ.get("RENDER_READY_TIMEOUT_S", "180"))
_READY_POLL_INTERVAL_S = 2.0
_READY_HTTP_TIMEOUT_S = 4.0


def _safe_redis() -> Optional[Any]:
    """Best-effort redis handle (the shared VM400 server via gpu_lock's env-resolved
    builder); None if unavailable — the render still proceeds (fall-forward)."""
    try:
        return make_redis_client()
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
    """Context manager for a ComfyUI render on the shared 3090.

    Usage (drop-in for the old ``with vram_pause(reason=...):``)::

        with render_residency(reason=f"face-swap {job_id}"):
            prompt_id = client.queue_prompt(workflow)
            ...  # poll history, read output

    Yields the gpu_lock ``held`` flag (False on Redis-down fallback). Always run the
    body — a False just means the cross-host swap-coordination was skipped.
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
                # driver's in-flight markers intact.
                r.lrem(RENDER_PRESSURE_KEY, 1, marker)
            except Exception as e:
                log.warning(
                    "render_lane: could not clear render-pressure marker (%s) — "
                    "dispatcher keeps ComfyUI resident until boot reconcile", e
                )
