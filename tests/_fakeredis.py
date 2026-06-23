"""A minimal in-memory Redis fake for Lärbild queue/state tests.

``fakeredis`` is not installed, so we hand-roll the small surface the queue uses:
string GET/SET (with ``ex`` TTL ignored for test purposes), list LPUSH/RPUSH/
BRPOP/LLEN, and DEL. Keyspace is a plain dict — enough to assert namespace
isolation (a larbild key never collides with an imagegen key) and FIFO ordering.
"""
from __future__ import annotations

from typing import Any, Optional


class FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.ttl: dict[str, int] = {}

    # ── strings ──
    def set(self, key: str, value: str, ex: Optional[int] = None, nx: bool = False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    def get(self, key: str) -> Optional[str]:
        return self.kv.get(key)

    def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.kv:
                del self.kv[k]
                n += 1
            if k in self.lists:
                del self.lists[k]
                n += 1
        return n

    # ── lists (FIFO queue: LPUSH onto head, BRPOP from tail) ──
    def lpush(self, key: str, *values: str) -> int:
        lst = self.lists.setdefault(key, [])
        for v in values:
            lst.insert(0, v)
        return len(lst)

    def rpush(self, key: str, *values: str) -> int:
        lst = self.lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    def brpop(self, key: str, timeout: int = 0):
        lst = self.lists.get(key)
        if lst:
            return (key, lst.pop())
        return None

    def rpop(self, key: str):
        lst = self.lists.get(key)
        if lst:
            return lst.pop()
        return None

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        lst = self.lists.get(key, [])
        # Redis LRANGE is inclusive of end; -1 means the last element.
        if end == -1:
            return list(lst[start:])
        return list(lst[start:end + 1])

    def lrem(self, key: str, count: int, value: str) -> int:
        """Remove occurrences of value. count==0 removes all (the queue cancel uses 0)."""
        lst = self.lists.get(key)
        if not lst:
            return 0
        removed = 0
        if count == 0:
            new = [v for v in lst if v != value]
            removed = len(lst) - len(new)
            self.lists[key] = new
        else:
            # positive: head→tail; negative: tail→head (we only use 0, but be correct)
            indices = range(len(lst)) if count > 0 else range(len(lst) - 1, -1, -1)
            to_remove = []
            for i in indices:
                if lst[i] == value:
                    to_remove.append(i)
                    if len(to_remove) >= abs(count):
                        break
            for i in sorted(to_remove, reverse=True):
                del lst[i]
            removed = len(to_remove)
        return removed

    # introspection helper for tests
    def all_keys(self) -> set[str]:
        return set(self.kv) | set(self.lists)
