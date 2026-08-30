"""Translation router: cache, provider chain, failover, streaming.

Order of operations for one subtitle:

1. Cache lookup - a hit returns in microseconds and costs no quota.
2. First provider whose client-side rate limiter says it has headroom.
3. Stream tokens back through `on_chunk` so the overlay fills in progressively
   rather than waiting for the full line.
4. On a rate limit or transient failure, move to the next provider immediately.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .cache import TranslationCache
from .config import TranslateConfig
from .metrics import Metrics
from .providers.base import (
    AuthError,
    Provider,
    ProviderError,
    RateLimitError,
    TranslationRequest,
    build_provider,
)
from .textnorm import looks_like_persian, sanitize_translation

log = logging.getLogger(__name__)

ChunkCallback = Callable[[str, str], None]
"""Called as (accumulated_text, new_chunk) while a translation streams in."""


@dataclass
class TranslationOutcome:
    source: str
    text: str = ""
    provider: str = ""
    cached: bool = False
    first_token_ms: float = 0.0
    total_ms: float = 0.0
    error: Optional[str] = None
    attempts: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.error is None


class Translator:
    """Owns the provider chain and the cache."""

    def __init__(
        self,
        cfg: TranslateConfig,
        metrics: Optional[Metrics] = None,
        providers: Optional[List[Provider]] = None,
    ) -> None:
        self.cfg = cfg
        self.metrics = metrics or Metrics()
        self.cache = TranslationCache(
            path=cfg.cache_path or None,
            memory_size=cfg.cache_memory_size,
            target_language=cfg.target_language,
        )
        self._providers: List[Provider] = []
        self._lock = threading.Lock()
        if providers is not None:
            # Injected chain - used by the tests, and by any embedder that wants
            # to supply its own backends rather than build them from config.
            self._providers = list(providers)
        else:
            self._build_providers()

    def _build_providers(self) -> None:
        for entry in self.cfg.providers:
            if not entry.enabled:
                log.debug("provider %s disabled in config", entry.name)
                continue
            try:
                provider = build_provider(entry)
            except AuthError as exc:
                # Expected when the user only configured some of the chain.
                log.info("provider %s unavailable: %s", entry.name, exc)
                continue
            except Exception as exc:
                log.warning("provider %s failed to initialise: %s", entry.name, exc)
                continue
            self._providers.append(provider)

        if not self._providers:
            raise RuntimeError(
                "No translation provider is configured.\n"
                "Set at least one API key, for example:\n"
                "  Windows : setx GEMINI_API_KEY \"your-key\"\n"
                "  bash    : export GEMINI_API_KEY=your-key\n"
                "Free keys: https://aistudio.google.com/apikey (Gemini) "
                "or https://console.groq.com/keys (Groq)."
            )
        log.info("translation chain: %s", " -> ".join(p.name for p in self._providers))

    @property
    def providers(self) -> List[Provider]:
        return list(self._providers)

    def warmup(self) -> None:
        """Open connections up front so the first subtitle is not the slowest."""
        for provider in self._providers:
            try:
                provider.warmup()
            except Exception as exc:  # pragma: no cover - best effort
                log.debug("warmup failed for %s: %s", provider.name, exc)

    # -- main entry point ----------------------------------------------------

    def translate(
        self,
        text: str,
        on_chunk: Optional[ChunkCallback] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> TranslationOutcome:
        started = time.perf_counter()
        outcome = TranslationOutcome(source=text)

        if not text.strip():
            return outcome

        cached = self.cache.get(text)
        if cached is not None:
            outcome.text = cached.translation
            outcome.provider = cached.provider
            outcome.cached = True
            outcome.total_ms = (time.perf_counter() - started) * 1000.0
            outcome.first_token_ms = outcome.total_ms
            self.metrics.increment("cache_hit")
            self.metrics.record("translate_cached", outcome.total_ms)
            if on_chunk:
                on_chunk(outcome.text, outcome.text)
            return outcome

        self.metrics.increment("cache_miss")
        request = TranslationRequest(
            text=text,
            target_language=self.cfg.target_language,
            source_language=self.cfg.source_language,
            context_hint=self.cfg.context_hint,
            glossary=self.cfg.glossary,
        )

        # Every provider rate limited is the normal state on a free tier during
        # a talkative scene, not an error. Dropping the line loses it forever;
        # waiting a beat costs a little latency and keeps the subtitle. Only
        # give up when the wait would outlive the line itself.
        self._wait_for_a_slot(is_cancelled)

        last_error: Optional[str] = None
        for provider in self._ordered_providers():
            if is_cancelled and is_cancelled():
                outcome.error = "cancelled"
                return outcome

            if not provider.limiter.available():
                log.debug("skipping %s: local rate limit", provider.name)
                continue

            outcome.attempts.append(provider.name)
            provider.limiter.record()

            request.compact_prompt = provider.cfg.compact_prompt
            try:
                text_out, first_token_ms = self._stream_one(
                    provider, request, on_chunk, is_cancelled
                )
            except RateLimitError as exc:
                log.info("%s rate limited; failing over", provider.name)
                provider.limiter.block_for(exc.retry_after or 20.0)
                self.metrics.increment("provider_rate_limited")
                last_error = str(exc)
                continue
            except AuthError as exc:
                log.warning("%s auth error; disabling for this session: %s", provider.name, exc)
                provider.limiter.block_for(3600.0)
                last_error = str(exc)
                continue
            except ProviderError as exc:
                log.info("%s failed (%s); failing over", provider.name, exc)
                self.metrics.increment("provider_error")
                last_error = str(exc)
                continue
            except Exception as exc:  # defensive: never kill the pipeline thread
                log.exception("%s raised unexpectedly", provider.name)
                last_error = str(exc)
                continue

            if is_cancelled and is_cancelled():
                outcome.error = "cancelled"
                return outcome

            cleaned = sanitize_translation(text_out, persian=self.targets_persian())
            if not cleaned:
                # An empty answer means the model judged the input to be noise.
                log.debug("%s returned empty output for %r", provider.name, text[:60])
                last_error = "empty response"
                continue

            # A model that answers in the wrong language has not translated
            # anything - it is reasoning out loud, refusing, or apologising.
            # Putting that on screen mid-game is worse than showing nothing, so
            # treat it as a failed provider and try the next one.
            if self.targets_persian() and not looks_like_persian(cleaned):
                log.warning(
                    "%s answered in the wrong language (%r...); failing over",
                    provider.name, cleaned[:60],
                )
                self.metrics.increment("wrong_language")
                last_error = f"{provider.name} did not answer in the target language"
                continue

            outcome.text = cleaned
            outcome.provider = provider.name
            outcome.first_token_ms = first_token_ms
            outcome.total_ms = (time.perf_counter() - started) * 1000.0
            self.metrics.record("translate_first_token", first_token_ms)
            self.metrics.record("translate_total", outcome.total_ms)
            self.cache.put(text, cleaned, provider.name)
            return outcome

        if last_error is None:
            soonest = min(
                (p.limiter.seconds_until_available() for p in self._providers),
                default=0.0,
            )
            last_error = (
                f"all providers rate limited for another {soonest:.0f}s - "
                "add a second provider (gametrans setkey groq) or a local model"
            )
        outcome.error = last_error
        outcome.total_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.increment("translate_failed")
        return outcome

    def _stream_one(
        self,
        provider: Provider,
        request: TranslationRequest,
        on_chunk: Optional[ChunkCallback],
        is_cancelled: Optional[Callable[[], bool]],
    ) -> tuple:
        started = time.perf_counter()
        first_token_ms = 0.0
        buffer: List[str] = []

        for chunk in provider.stream(request):
            if not chunk:
                continue
            if not buffer:
                first_token_ms = (time.perf_counter() - started) * 1000.0
            buffer.append(chunk)
            if on_chunk and self.cfg.stream:
                # Sanitising per chunk would fight the streaming (a leading quote
                # is only identifiable once closed), so only fold letter forms.
                on_chunk("".join(buffer), chunk)
            if is_cancelled and is_cancelled():
                break

        return "".join(buffer), first_token_ms

    def _wait_for_a_slot(self, is_cancelled: Optional[Callable[[], bool]]) -> None:
        """Block briefly when every provider is rate limited.

        Sleeps in short steps so a cancellation - the subtitle leaving the
        screen - is noticed promptly rather than after the full wait.
        """
        budget = self.cfg.max_wait_for_slot_ms / 1000.0
        if budget <= 0:
            return

        with self._lock:
            providers = list(self._providers)
        if any(p.limiter.available() for p in providers):
            return

        soonest = min((p.limiter.seconds_until_available() for p in providers), default=0.0)
        if soonest <= 0 or soonest > budget:
            if soonest > budget:
                log.debug("all providers busy for %.1fs, longer than the budget", soonest)
            return

        self.metrics.increment("waited_for_rate_limit")
        deadline = time.monotonic() + soonest
        while time.monotonic() < deadline:
            if is_cancelled and is_cancelled():
                return
            time.sleep(min(0.05, deadline - time.monotonic()))

    def _ordered_providers(self) -> List[Provider]:
        """Chain order, but skip providers currently blocked by a 429."""
        with self._lock:
            ready = [p for p in self._providers if p.limiter.available()]
            blocked = [p for p in self._providers if not p.limiter.available()]
        # Blocked providers stay at the end as a last resort rather than being
        # dropped: a stale block is better than no translation at all.
        return ready + blocked

    def targets_persian(self) -> bool:
        """Whether output needs Persian letter-form normalisation."""
        target = self.cfg.target_language.lower()
        return "persian" in target or "farsi" in target or target == "fa"

    def close(self) -> None:
        for provider in self._providers:
            try:
                provider.close()
            except Exception:  # pragma: no cover - shutdown best effort
                pass
        self.cache.close()
