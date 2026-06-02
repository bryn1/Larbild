"""Tests for the SHARED VM100 3090 gpu_lock fork (lb-c2r, P1.1).

The single most important assertion: the lock key is ``lock:vm100-3090`` —
shared VERBATIM with imagegen — because mutual exclusion is the whole point
(BUILD_PLAN R4: larbild + imagegen + chat contend for one 24 GB card).

No real Redis is used (and no live GPU): a hand-rolled FakeRedis emulates the
SETNX-with-TTL + Lua compare-and-delete surface the lock relies on.
"""
from __future__ import annotations

import time

from app import gpu_lock


class FakeRedis:
    """Minimal Redis emulation: SET nx/ex, GET, and the release/refresh Lua.

    Enough surface for gpu_lock: ``set(key, val, nx, ex)``, ``get``, and
    ``eval`` of the two Lua scripts (compare-and-delete, compare-and-pexpire).
    TTL is tracked but not actively expired (tests don't need wall-clock expiry).
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl_ms: dict[str, int] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttl_ms[key] = ex * 1000
        return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, *args):
        key = args[0]
        token = args[1]
        if "del" in script:  # release
            if self.store.get(key) == token:
                self.store.pop(key, None)
                self.ttl_ms.pop(key, None)
                return 1
            return 0
        if "pexpire" in script:  # refresh
            if self.store.get(key) == token:
                self.ttl_ms[key] = int(args[2])
                return 1
            return 0
        return 0


def _reset_thread_state():
    # gpu_lock keeps a per-thread refcount; clear it between tests on this thread.
    if hasattr(gpu_lock._thread_local, "state"):
        delattr(gpu_lock._thread_local, "state")


def test_lock_key_is_shared_with_imagegen():
    # THE invariant: never namespaced to larbild:. Hard-coded so a careless
    # refactor that namespaces it (and silently breaks cross-app exclusion) fails
    # this test loudly.
    assert gpu_lock.LOCK_KEY == "lock:vm100-3090"


def test_acquire_and_release_round_trip():
    _reset_thread_state()
    r = FakeRedis()
    with gpu_lock.gpu_lock(reason="t", client=r, refresh_s=999) as held:
        assert held is True
        assert r.get("lock:vm100-3090") is not None  # the key is set while held
    # released → key gone
    assert r.get("lock:vm100-3090") is None


def test_second_holder_blocked_until_release_then_times_out():
    _reset_thread_state()
    r = FakeRedis()
    # Pre-occupy the key with a DIFFERENT holder's token (simulates imagegen
    # holding the lock). A larbild acquire should NOT win; with a tiny timeout it
    # falls back to held=False (yield-without-lock), never stealing the key.
    r.set("lock:vm100-3090", "imagegen:1:deadbeef", nx=True, ex=300)
    with gpu_lock.gpu_lock(reason="t", client=r, timeout_s=0.2, refresh_s=999) as held:
        assert held is False  # could not acquire — imagegen still holds it
    # the imagegen holder's key is untouched
    assert r.get("lock:vm100-3090") == "imagegen:1:deadbeef"


def test_disable_flag_yields_false(monkeypatch):
    _reset_thread_state()
    monkeypatch.setattr(gpu_lock, "DISABLE", True)
    with gpu_lock.gpu_lock(reason="t", client=FakeRedis()) as held:
        assert held is False


def test_no_redis_client_falls_back_to_false():
    _reset_thread_state()
    # client=None + make_redis_client returns None (simulate redis missing)
    import unittest.mock as mock
    with mock.patch.object(gpu_lock, "make_redis_client", return_value=None):
        with gpu_lock.gpu_lock(reason="t") as held:
            assert held is False


def test_reentrant_acquire_single_redis_key():
    _reset_thread_state()
    r = FakeRedis()
    with gpu_lock.gpu_lock(reason="outer", client=r, refresh_s=999) as outer:
        assert outer is True
        token = r.get("lock:vm100-3090")
        with gpu_lock.gpu_lock(reason="inner", client=r, refresh_s=999) as inner:
            assert inner is True
            # re-entrant: same key, same token — no second acquire
            assert r.get("lock:vm100-3090") == token
        # inner exit did NOT release (refcount still > 0)
        assert r.get("lock:vm100-3090") == token
    # outer exit releases
    assert r.get("lock:vm100-3090") is None


def test_release_only_drops_own_token():
    _reset_thread_state()
    r = FakeRedis()
    with gpu_lock.gpu_lock(reason="t", client=r, refresh_s=999):
        # simulate TTL expiry + another holder grabbing the key mid-hold
        r.store["lock:vm100-3090"] = "someone-else"
    # our release must NOT have dropped the other holder's key
    assert r.get("lock:vm100-3090") == "someone-else"


def test_make_redis_client_prefers_larbild_env(monkeypatch):
    monkeypatch.setenv("LARBILD_REDIS_URL", "redis://larbild-host:6379/0")
    monkeypatch.delenv("IMAGE_GEN_REDIS_URL", raising=False)
    captured = {}

    class _FakeRedisModule:
        class Redis:
            @staticmethod
            def from_url(url, decode_responses=False):
                captured["url"] = url
                return object()

    import sys
    monkeypatch.setitem(sys.modules, "redis", _FakeRedisModule)
    gpu_lock.make_redis_client()
    assert captured["url"] == "redis://larbild-host:6379/0"


def test_make_redis_client_falls_back_to_imagegen_env(monkeypatch):
    # If only the imagegen prefix is set, we still resolve the SAME server (so
    # the shared lock key lives in one Redis, not two).
    monkeypatch.delenv("LARBILD_REDIS_URL", raising=False)
    monkeypatch.setenv("IMAGE_GEN_REDIS_URL", "redis://shared-vm400:6379/0")
    captured = {}

    class _FakeRedisModule:
        class Redis:
            @staticmethod
            def from_url(url, decode_responses=False):
                captured["url"] = url
                return object()

    import sys
    monkeypatch.setitem(sys.modules, "redis", _FakeRedisModule)
    gpu_lock.make_redis_client()
    assert captured["url"] == "redis://shared-vm400:6379/0"
