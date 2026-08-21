"""Small bounded in-process caches for local RAG queries and embeddings."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


def normalize_query(value: str) -> str:
    """Normalize harmless whitespace/case differences for cache keys."""
    return " ".join(value.strip().casefold().split())


@dataclass(frozen=True)
class CacheStats:
    size: int
    max_entries: int
    hits: int
    misses: int
    evictions: int


class BoundedCache(Generic[K, V]):
    """Thread-safe LRU cache with explicit copy-on-read semantics at callers."""

    def __init__(self, max_entries: int = 256):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._items: OrderedDict[K, V] = OrderedDict()
        self._lock = RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: K) -> V | None:
        with self._lock:
            if key not in self._items:
                self._misses += 1
                return None
            self._hits += 1
            value = self._items.pop(key)
            self._items[key] = value
            return value

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = value
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
                self._evictions += 1

    def set(self, key: K, value: V) -> None:
        """Store a value using cache terminology that is safe in tool audits."""
        self.put(key, value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                size=len(self._items),
                max_entries=self.max_entries,
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
            )
