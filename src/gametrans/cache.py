"""Translation cache: in-memory LRU in front of an optional SQLite store.

Games repeat text constantly - menu strings, quest names, item descriptions, a
line you walk past twice. A cache hit costs ~0.05 ms against ~300 ms for an API
round trip, and it does not consume free-tier quota. In practice this is what
makes the free tiers viable at all.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from .textnorm import cache_key

log = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    translation: str
    provider: str
    created_at: float


class TranslationCache:
    """Thread-safe two-tier cache.

    Tier 1 is an in-process LRU (sub-microsecond). Tier 2 is a SQLite file that
    survives restarts, so the second session in the same game starts warm.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        memory_size: int = 2048,
        target_language: str = "fa",
    ) -> None:
        self._memory: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._memory_size = max(memory_size, 0)
        self._lock = threading.RLock()
        self._target = target_language
        self._conn: Optional[sqlite3.Connection] = None
        self.hits = 0
        self.misses = 0

        if path:
            try:
                self._conn = sqlite3.connect(path, check_same_thread=False)
                # WAL keeps reads from blocking behind the writer thread.
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS translations (
                        key        TEXT NOT NULL,
                        target     TEXT NOT NULL,
                        source     TEXT NOT NULL,
                        translation TEXT NOT NULL,
                        provider   TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        PRIMARY KEY (key, target)
                    )
                    """
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                log.warning("Disk cache unavailable (%s); using memory only", exc)
                self._conn = None

    # -- lookup --------------------------------------------------------------

    def get(self, source_text: str) -> Optional[CacheEntry]:
        key = cache_key(source_text)
        if not key:
            return None

        with self._lock:
            entry = self._memory.get(key)
            if entry is not None:
                self._memory.move_to_end(key)
                self.hits += 1
                return entry

        entry = self._get_from_disk(key)
        if entry is not None:
            self._store_memory(key, entry)
            with self._lock:
                self.hits += 1
            return entry

        with self._lock:
            self.misses += 1
        return None

    def _get_from_disk(self, key: str) -> Optional[CacheEntry]:
        if self._conn is None:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT translation, provider, created_at FROM translations "
                    "WHERE key = ? AND target = ?",
                    (key, self._target),
                ).fetchone()
        except sqlite3.Error as exc:
            log.debug("cache read failed: %s", exc)
            return None
        if row is None:
            return None
        return CacheEntry(translation=row[0], provider=row[1], created_at=row[2])

    # -- storage -------------------------------------------------------------

    def put(self, source_text: str, translation: str, provider: str = "") -> None:
        key = cache_key(source_text)
        if not key or not translation.strip():
            return
        entry = CacheEntry(translation=translation, provider=provider, created_at=time.time())
        self._store_memory(key, entry)
        self._store_disk(key, source_text, entry)

    def _store_memory(self, key: str, entry: CacheEntry) -> None:
        with self._lock:
            self._memory[key] = entry
            self._memory.move_to_end(key)
            while self._memory_size and len(self._memory) > self._memory_size:
                self._memory.popitem(last=False)

    def _store_disk(self, key: str, source_text: str, entry: CacheEntry) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO translations "
                    "(key, target, source, translation, provider, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        self._target,
                        source_text,
                        entry.translation,
                        entry.provider,
                        entry.created_at,
                    ),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            log.debug("cache write failed: %s", exc)

    # -- housekeeping --------------------------------------------------------

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def size(self) -> int:
        with self._lock:
            return len(self._memory)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - shutdown best effort
                pass
            self._conn = None
