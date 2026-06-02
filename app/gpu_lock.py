"""Cross-host Redis lock around the VM100 3090 — forked for Lärbild (lb-c2r).

A SFW-stripped fork of imagegen's ``sibbis-app/gpu_lock.py`` (ig-3drb). The
*coordination contract is shared, the data is not*: Lärbild renders run on the
same VM100 3090 as imagegen's renders and as the resident chat/compose LLM, so
all three must take **the same mutex** or they OOM the 24 GB card. This module
therefore SETNX-with-TTL on the **exact same Redis key** imagegen uses —

    LOCK_KEY = "lock:vm100-3090"   # SHARED verbatim; do NOT namespace this

— against the **same VM400 Redis** (192.168.5.77:6379) imagegen's worker +
Sibbis pipelines use. A larbild render that wins the key blocks an imagegen
render (and vice-versa) until release; that mutual exclusion is the whole point
(BUILD_PLAN risk R4, "3-way 3090 contention — SHARE gpu_lock, don't fork").

What *is* forked (not shared): the env-var prefix for the Redis connection is
``LARBILD_REDIS_*`` (with the imagegen ``IMAGE_GEN_REDIS_*`` names honored as a
fallback so a single deploy that only sets one prefix still points both apps at
the same server). The job queue keys (app/jobs.py) ARE namespaced ``larbild:``
— only this lock key is shared, because a lock is mutual exclusion, not data.

SFW-only: no restricted import, model, or reference. The only process this
module is aware of is ComfyUI; it never names a restricted feature.

Env vars:
    LARBILD_REDIS_URL / IMAGE_GEN_REDIS_URL        full redis:// URL — wins if set
    LARBILD_REDIS_HOST / IMAGE_GEN_REDIS_HOST      default 192.168.5.77 (VM400)
    LARBILD_REDIS_PORT / IMAGE_GEN_REDIS_PORT      default 6379
    LARBILD_REDIS_PASSWORD / IMAGE_GEN_REDIS_PASSWORD   optional
    GPU_LOCK_TTL_S        default 300 — crash safety; lock evaporates if a holder dies
    GPU_LOCK_TIMEOUT_S    default 600 — covers a long render queued behind another
    GPU_LOCK_REFRESH_S    default 60  — TTL refresh cadence while the holder lives
    GPU_LOCK_DISABLE      if "1", skip the lock entirely (debug/fallback)
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

log = logging.getLogger("larbild.gpu_lock")

# SHARED verbatim with imagegen — the mutual-exclusion contract on the VM100
# 3090. Never namespace this to 'larbild:'; that would let larbild + imagegen
# render concurrently and OOM the card (the exact bug this lock prevents).
LOCK_KEY = "lock:vm100-3090"

DEFAULT_TTL_S = int(os.environ.get("GPU_LOCK_TTL_S", "300"))
DEFAULT_TIMEOUT_S = float(os.environ.get("GPU_LOCK_TIMEOUT_S", "600"))
DEFAULT_REFRESH_S = float(os.environ.get("GPU_LOCK_REFRESH_S", "60"))
DISABLE = os.environ.get("GPU_LOCK_DISABLE") == "1"

__all__ = ["gpu_lock", "LOCK_KEY", "make_redis_client"]

# Atomic compare-and-delete on release. A plain DEL would race TTL expiry + a
# new holder taking the key — we'd drop someone else's lock. KEYS[1] = lock
# key, ARGV[1] = our holder token.
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Atomic refresh: only PEXPIRE while we still own the key, so a stale refresher
# can't resurrect a lock another holder now owns.
_REFRESH_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""

# Per-thread refcount for re-entrancy. Nested gpu_lock() calls on the same
# thread share one Redis acquire (outermost locks, innermost releases) so a
# worker that pauses VRAM inside an already-locked render doesn't self-deadlock.
_thread_local = threading.local()


def _holder_state() -> dict[str, Any]:
    state = getattr(_thread_local, "state", None)
    if state is None:
        state = {"token": None, "refcount": 0, "stop_event": None, "refresher": None}
        _thread_local.state = state
    return state


def _new_holder_token() -> str:
    """``hostname:pid:uuid`` — unique per acquire so the release Lua never drops
    a lock another holder grabbed after our TTL expired."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    """First set env var among *names* (Lärbild prefix first, imagegen fallback)."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def make_redis_client() -> Optional[Any]:
    """Build a Redis client pointed at the SAME server imagegen uses.

    Resolves ``LARBILD_REDIS_*`` first, then the imagegen ``IMAGE_GEN_REDIS_*``
    names, so a deploy that only sets one prefix still points both apps at the
    shared VM400 Redis (otherwise the lock key would live in two servers and
    stop coordinating). Returns ``None`` on import failure so callers fall
    through to the lock-disabled path rather than crashing.
    """
    try:
        import redis  # type: ignore
    except ImportError:
        log.warning("redis package not installed; gpu_lock disabled")
        return None

    url = _env("LARBILD_REDIS_URL", "IMAGE_GEN_REDIS_URL")
    if url:
        return redis.Redis.from_url(url, decode_responses=True)
    return redis.Redis(
        host=_env("LARBILD_REDIS_HOST", "IMAGE_GEN_REDIS_HOST", default="192.168.5.77"),
        port=int(_env("LARBILD_REDIS_PORT", "IMAGE_GEN_REDIS_PORT", default="6379")),
        password=_env("LARBILD_REDIS_PASSWORD", "IMAGE_GEN_REDIS_PASSWORD"),
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


def _try_acquire(client: Any, token: str, ttl_s: int) -> bool:
    """Atomic SETNX with TTL. True if the lock is now held by us."""
    return bool(client.set(LOCK_KEY, token, nx=True, ex=ttl_s))


def _release(client: Any, token: str) -> None:
    """Atomic compare-and-delete via Lua. No-ops if the lock isn't ours."""
    try:
        client.eval(_RELEASE_LUA, 1, LOCK_KEY, token)
    except Exception as e:  # noqa: BLE001 — log + continue; never crash on release
        log.warning("gpu_lock release failed: %s", e)


def _refresh_loop(
    client: Any,
    token: str,
    ttl_s: int,
    refresh_s: float,
    stop_event: threading.Event,
) -> None:
    """Extend the lock TTL on a fixed cadence until ``stop_event`` is set.

    Daemon thread, so an abandoned/crashed holder doesn't pin the lock past the
    natural TTL. A failed refresh is logged and retried — the next success
    re-establishes ownership.
    """
    pexpire_ms = ttl_s * 1000
    while not stop_event.wait(refresh_s):
        try:
            ok = client.eval(_REFRESH_LUA, 1, LOCK_KEY, token, pexpire_ms)
            if not ok:
                log.warning(
                    "gpu_lock refresh: key %s no longer owned by token=%s "
                    "(TTL expired or another holder claimed it)",
                    LOCK_KEY, token,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("gpu_lock refresh transient error: %s", e)


@contextmanager
def gpu_lock(
    reason: str = "",
    *,
    ttl_s: int = DEFAULT_TTL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    refresh_s: float = DEFAULT_REFRESH_S,
    client: Optional[Any] = None,
) -> Iterator[bool]:
    """Acquire the SHARED VM100 3090 lock; yield True if held, False on fallback.

    Usage::

        with gpu_lock(reason="larbild render abc") as held:
            if not held:
                log.warning("rendering without cross-host coordination")
            ...  # always run the work — the fallback is already logged

    Crash semantics: the refresh thread is a daemon, so a dead holder's lock
    expires at the natural TTL (default 300s). Re-entrancy: nested calls on one
    thread share a single acquire. Fallback (Redis unreachable or
    ``GPU_LOCK_DISABLE=1``): yield False without acquiring — a Redis outage must
    not refuse all render work, so we accept the (already-tolerated) original
    race over a hard failure, exactly like imagegen.
    """
    if DISABLE:
        log.warning("gpu_lock DISABLED via GPU_LOCK_DISABLE=1; reason=%s", reason)
        yield False
        return

    state = _holder_state()

    # Re-entrant: nested calls on the same thread are no-ops at the Redis layer.
    if state["refcount"] > 0:
        state["refcount"] += 1
        log.info(
            "gpu_lock re-entrant acquire reason=%s refcount=%d",
            reason, state["refcount"],
        )
        try:
            yield True
        finally:
            state["refcount"] -= 1
        return

    if client is None:
        client = make_redis_client()

    if client is None:
        log.error(
            "gpu_lock FALLBACK: redis client unavailable; reason=%s — "
            "running without cross-host coordination",
            reason,
        )
        yield False
        return

    token = _new_holder_token()
    log.info(
        "gpu_lock acquire begin reason=%s token=%s ttl=%ds timeout=%.1fs",
        reason, token, ttl_s, timeout_s,
    )

    deadline = time.monotonic() + timeout_s
    backoff = 0.5
    acquired = False
    while True:
        try:
            if _try_acquire(client, token, ttl_s):
                acquired = True
                break
        except Exception as e:  # noqa: BLE001
            # Redis went away mid-acquire. Preserve current-state behaviour
            # ("log loud, yield without lock") rather than blocking renders.
            log.error(
                "gpu_lock FALLBACK: redis error during acquire reason=%s: %s",
                reason, e,
            )
            yield False
            return
        if time.monotonic() >= deadline:
            log.error(
                "gpu_lock TIMEOUT after %.1fs reason=%s — proceeding without lock "
                "(prior holder likely stuck; TTL %ds will eventually clear)",
                timeout_s, reason, ttl_s,
            )
            yield False
            return
        time.sleep(min(backoff, max(0.5, deadline - time.monotonic())))
        backoff = min(backoff * 1.5, 5.0)

    log.info("gpu_lock acquired reason=%s token=%s", reason, token)

    stop_event = threading.Event()
    refresher = threading.Thread(
        target=_refresh_loop,
        args=(client, token, ttl_s, refresh_s, stop_event),
        name=f"gpu_lock_refresh_{token[-8:]}",
        daemon=True,
    )
    refresher.start()

    state["token"] = token
    state["refcount"] = 1
    state["stop_event"] = stop_event
    state["refresher"] = refresher

    try:
        yield True
    finally:
        # Outermost release: tear down the refresher and drop the key. We don't
        # join() for long — the daemon exits on its own once stop_event is set.
        stop_event.set()
        refresher.join(timeout=2.0)
        if acquired:
            _release(client, token)
        log.info("gpu_lock released reason=%s token=%s", reason, token)
        state["token"] = None
        state["refcount"] = 0
        state["stop_event"] = None
        state["refresher"] = None
