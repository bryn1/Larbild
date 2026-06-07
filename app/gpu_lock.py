"""Cross-host Redis mutex around the VM100 3090 — the ONE canonical implementation.

This is the single source of truth for the GPU lock. Three diverged copies used
to exist (imagegen ``sibbis-app/`` + imagegen ``vm100/shared_runtime/`` + larbild
``app/``); they differed only by logger name and a Redis env-var prefix order.
They are now collapsed here (CODEBASE_AUDIT_2026-06-07 cluster 1). Each app gets a
byte-identical *vendored copy* of this file at deploy time and a CI drift-check
keeps the copies in sync — apps deploy independently (imagegen rsync→Sibbis,
larbild git-pull→Sibbis, vm100 host-local), so there is no shared import path. See
``infra/shared/README.md`` for the deploy-vendor pattern.

Why the lock exists
-------------------
Larbild renders, imagegen renders, and the resident chat/compose LLM all run on
the same VM100 3090. They must take **the same mutex** or they OOM the 24 GB card.
The lock is a Redis ``SETNX``-with-TTL on a key SHARED VERBATIM across every
participant — a winner blocks the others until it releases. That mutual exclusion
is the whole point; never namespace the key per-app.

    LOCK_KEY = "lock:vm100-3090"   # SHARED verbatim; do NOT namespace this

Configuration (zero code change required — defaults preserve every app's behavior)
---------------------------------------------------------------------------------
The only two things that differed between the old copies are env-configurable, so
the vendored file is byte-identical everywhere:

    GPU_LOCK_LOGGER          logging.getLogger() name      (default "gpu_lock")
    GPU_LOCK_ENV_PREFIXES    comma-sep Redis env prefixes  (default "LARBILD,IMAGE_GEN")

The default prefix order resolves ``LARBILD_REDIS_*`` first then
``IMAGE_GEN_REDIS_*`` — for imagegen the LARBILD_* names are simply unset, so it
falls through to IMAGE_GEN_* (identical to its old behavior); for larbild the
LARBILD_* names win first (identical to its old behavior). A single default thus
preserves both apps. Apps may also call ``configure()`` instead of using env vars.

Redis connection env vars (resolved against each prefix in GPU_LOCK_ENV_PREFIXES):
    <PREFIX>_REDIS_URL        full redis:// URL — wins if set
    <PREFIX>_REDIS_HOST       default 192.168.5.77 (VM400)
    <PREFIX>_REDIS_PORT       default 6379
    <PREFIX>_REDIS_PASSWORD   optional

Lock-behavior env vars:
    GPU_LOCK_TTL_S        default 300 — crash safety; lock evaporates if a holder dies
    GPU_LOCK_TIMEOUT_S    default 600 — covers a long render queued behind another
    GPU_LOCK_REFRESH_S    default 60  — TTL refresh cadence while the holder lives
    GPU_LOCK_DISABLE      if "1", skip the lock entirely (debug/fallback)

Public API: ``gpu_lock`` (context manager), ``LOCK_KEY``, ``make_redis_client``,
``configure``.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence

# ── Configurable module state (the only per-app differences) ──────────────────
log = logging.getLogger(os.environ.get("GPU_LOCK_LOGGER", "gpu_lock"))

_ENV_PREFIXES: tuple[str, ...] = tuple(
    p.strip()
    for p in os.environ.get("GPU_LOCK_ENV_PREFIXES", "LARBILD,IMAGE_GEN").split(",")
    if p.strip()
)


def configure(
    *,
    logger_name: Optional[str] = None,
    env_prefixes: Optional[Sequence[str]] = None,
) -> None:
    """Override the logger name and/or Redis env-var prefixes programmatically.

    An alternative to the GPU_LOCK_LOGGER / GPU_LOCK_ENV_PREFIXES env vars for
    apps that prefer wiring this in code. Call once at import time, before the
    first ``gpu_lock()``.
    """
    global log, _ENV_PREFIXES
    if logger_name:
        log = logging.getLogger(logger_name)
    if env_prefixes:
        _ENV_PREFIXES = tuple(env_prefixes)


# SHARED verbatim with every participant — the mutual-exclusion contract on the
# VM100 3090. Never namespace this per-app; that would let two apps render
# concurrently and OOM the card (the exact bug this lock prevents).
LOCK_KEY = "lock:vm100-3090"

DEFAULT_TTL_S = int(os.environ.get("GPU_LOCK_TTL_S", "300"))
DEFAULT_TIMEOUT_S = float(os.environ.get("GPU_LOCK_TIMEOUT_S", "600"))
DEFAULT_REFRESH_S = float(os.environ.get("GPU_LOCK_REFRESH_S", "60"))
DISABLE = os.environ.get("GPU_LOCK_DISABLE") == "1"

__all__ = ["gpu_lock", "LOCK_KEY", "make_redis_client", "configure"]

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


def _env(suffix: str, default: Optional[str] = None) -> Optional[str]:
    """First set ``<PREFIX>_REDIS_<suffix>`` across GPU_LOCK_ENV_PREFIXES order."""
    for prefix in _ENV_PREFIXES:
        v = os.environ.get(f"{prefix}_REDIS_{suffix}")
        if v:
            return v
    return default


def make_redis_client() -> Optional[Any]:
    """Build a Redis client pointed at the SAME server every participant uses.

    Resolves the Redis env vars against GPU_LOCK_ENV_PREFIXES in order so a
    deploy that only sets one app's prefix still points every app at the shared
    VM400 Redis (otherwise the lock key would live in two servers and stop
    coordinating). Returns ``None`` on import failure so callers fall through to
    the lock-disabled path rather than crashing.
    """
    try:
        import redis  # type: ignore
    except ImportError:
        log.warning("redis package not installed; gpu_lock disabled")
        return None

    url = _env("URL")
    if url:
        return redis.Redis.from_url(url, decode_responses=True)
    return redis.Redis(
        host=_env("HOST", default="192.168.5.77"),
        port=int(_env("PORT", default="6379")),
        password=_env("PASSWORD"),
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


# Back-compat alias: imagegen's old copy exposed this name privately.
_make_redis_client = make_redis_client


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

        with gpu_lock(reason="render abc") as held:
            if not held:
                log.warning("rendering without cross-host coordination")
            ...  # always run the work — the fallback is already logged

    Crash semantics: the refresh thread is a daemon, so a dead holder's lock
    expires at the natural TTL (default 300s). Re-entrancy: nested calls on one
    thread share a single acquire. Fallback (Redis unreachable or
    ``GPU_LOCK_DISABLE=1``): yield False without acquiring — a Redis outage must
    not refuse all render work, so we accept the (already-tolerated) original
    race over a hard failure.
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
