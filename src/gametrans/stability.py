"""Decides *when* an OCR read is finished changing and worth translating.

Subtitles fade in, type out character by character, or flicker for a frame while
the engine re-renders them. Translating every intermediate state would triple the
API calls and put half-sentences on screen. This tracker waits for the text to
settle - but never longer than `max_wait_ms`, so the latency cost is bounded and
predictable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .config import StabilityConfig
from .textnorm import cache_key, is_extension_of, is_noise, similarity


@dataclass
class StabilityDecision:
    ready: bool = False
    text: str = ""
    reason: str = ""
    waited_ms: float = 0.0


class StabilityTracker:
    def __init__(self, cfg: StabilityConfig) -> None:
        self.cfg = cfg
        self._candidate = ""
        self._count = 0
        self._first_seen = 0.0
        self._last_emitted_key = ""

    def reset(self) -> None:
        self._candidate = ""
        self._count = 0
        self._first_seen = 0.0
        self._last_emitted_key = ""

    def clear_current(self) -> None:
        """Called when the region goes empty - the next line starts fresh."""
        self._candidate = ""
        self._count = 0
        self._first_seen = 0.0

    def feed(self, text: str, now: Optional[float] = None) -> StabilityDecision:
        now = now if now is not None else time.monotonic()
        text = text.strip()

        if not text or is_noise(text, min_chars=self.cfg.min_chars):
            return StabilityDecision(reason="noise")

        key = cache_key(text)
        if key and key == self._last_emitted_key:
            # Same line still on screen; already translated.
            return StabilityDecision(reason="already-emitted")

        if not self._candidate:
            self._candidate = text
            self._count = 1
            self._first_seen = now
            return self._maybe_emit(now, "first-read")

        if is_extension_of(self._candidate, text):
            # Text is still being typed out. Take the longer version and restart
            # the stability count, but keep the original clock so `max_wait_ms`
            # still bounds how long a slow typewriter can stall us.
            self._candidate = text
            self._count = 1
            return self._maybe_emit(now, "still-typing")

        if similarity(self._candidate, text) >= self.cfg.similarity_threshold:
            self._candidate = text
            self._count += 1
            return self._maybe_emit(now, "stable")

        # A genuinely different line replaced the old one before it settled.
        self._candidate = text
        self._count = 1
        self._first_seen = now
        return self._maybe_emit(now, "replaced")

    def _maybe_emit(self, now: float, reason: str) -> StabilityDecision:
        waited_ms = (now - self._first_seen) * 1000.0
        timed_out = waited_ms >= self.cfg.max_wait_ms
        settled = self._count >= self.cfg.frames_required

        if settled or timed_out:
            text = self._candidate
            self._last_emitted_key = cache_key(text)
            self._count = 0
            self._first_seen = now
            return StabilityDecision(
                ready=True,
                text=text,
                reason=reason if settled else "timeout",
                waited_ms=waited_ms,
            )

        return StabilityDecision(reason=reason, waited_ms=waited_ms)
