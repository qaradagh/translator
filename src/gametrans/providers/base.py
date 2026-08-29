"""Provider abstraction, prompt construction and client-side rate limiting."""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterator, Optional

from ..config import ProviderConfig

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """A provider failed in a way the router should fail over from."""


class RateLimitError(ProviderError):
    """Provider is rate limited. The router should move to the next provider."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AuthError(ProviderError):
    """Missing or rejected credentials - not worth retrying this session."""


class ModelNotFoundError(ProviderError):
    """The configured model does not exist, or is closed to new users.

    `suggested_model` carries the replacement when the provider named one, which
    lets the caller recover automatically rather than failing the translation.
    """

    def __init__(self, message: str, suggested_model: Optional[str] = None) -> None:
        super().__init__(message)
        self.suggested_model = suggested_model


@dataclass
class TranslationRequest:
    text: str
    target_language: str = "Persian (Farsi)"
    source_language: str = "auto"
    context_hint: str = ""
    glossary: Dict[str, str] = field(default_factory=dict)
    # Local models pay for every prompt token on every request; see
    # build_compact_system_prompt.
    compact_prompt: bool = False


# -- prompt -----------------------------------------------------------------

_BASE_RULES = """You translate on-screen video game text in real time.

Output rules (strict):
- Output ONLY the translation. No preamble, no notes, no quotes, no transliteration.
- Preserve the original line breaks exactly, one output line per input line.
- Translate naturally, the way a native player would say it - not word for word.
- Keep numbers, percentages, key bindings and button prompts unchanged.
- Keep character, place and item names recognisable; do not invent new names.
- If the input is already in the target language, repeat it unchanged.
- If the input is meaningless OCR noise, output nothing at all."""

_PERSIAN_RULES = """
Persian specifics:
- Write modern, conversational Persian, not formal literary or Arabic-flavoured prose.
- Use the Persian characters (ک, ی), never the Arabic ones (ك, ي).
- Use a zero-width non-joiner for prefixes and plurals where standard: می‌روم, کتاب‌ها.
- Use Persian punctuation: ، for comma and ؟ for question mark.
- Keep Latin proper nouns in Latin script when that is how players write them."""


_COMPACT_RULES = """Translate video game text into {target}.
Output only the translation: no notes, no quotes, no explanations.
Keep line breaks, numbers and key names exactly as they are.
Write natural spoken {target}, not formal or literary."""

_COMPACT_PERSIAN = """Use ک and ی, never ك and ي. Use ، and ؟."""


def build_compact_system_prompt(request: TranslationRequest) -> str:
    """A much shorter prompt, for models running on the user's own hardware.

    A local model re-reads the system prompt on every request, and prefill is
    real wall-clock time on a consumer GPU - the full prompt is around 260
    tokens against roughly 45 here. Large hosted models need the detailed rules
    to behave; a small local one mostly needs to be told the job and left alone,
    and a shorter prompt leaves more of its attention on the actual line.
    """
    parts = [_COMPACT_RULES.format(target=request.target_language)]

    target_lower = request.target_language.lower()
    if "persian" in target_lower or "farsi" in target_lower or target_lower == "fa":
        parts.append(_COMPACT_PERSIAN)

    if request.context_hint:
        parts.append(request.context_hint)

    if request.glossary:
        pairs = "; ".join(f"{src} = {dst}" for src, dst in sorted(request.glossary.items()))
        parts.append(f"Always: {pairs}")

    return "\n".join(parts)


def build_system_prompt(request: TranslationRequest) -> str:
    """Assemble the system prompt.

    Deliberately stable across calls: identical bytes let providers that support
    prompt caching reuse the prefix, and keeping it short keeps the
    time-to-first-token down.
    """
    if request.compact_prompt:
        return build_compact_system_prompt(request)

    parts = [_BASE_RULES]

    target_lower = request.target_language.lower()
    if "persian" in target_lower or "farsi" in target_lower or target_lower == "fa":
        parts.append(_PERSIAN_RULES)

    parts.append(f"\nTranslate into: {request.target_language}")
    if request.source_language and request.source_language != "auto":
        parts.append(f"Source language: {request.source_language}")

    if request.context_hint:
        parts.append(f"\nContext: {request.context_hint}")

    if request.glossary:
        pairs = "\n".join(f"- {src} -> {dst}" for src, dst in sorted(request.glossary.items()))
        parts.append(f"\nAlways translate these exactly this way:\n{pairs}")

    return "\n".join(parts)


# -- client-side rate limiting ----------------------------------------------


class RateLimiter:
    """Sliding-window limiter.

    Used to fail over to the next provider *before* the server returns a 429 -
    a rejected request still costs a full round trip, which is exactly the
    latency we are trying to avoid.
    """

    def __init__(self, rpm_limit: int) -> None:
        self.rpm_limit = rpm_limit
        self._events: deque = deque()
        self._lock = threading.Lock()
        self._blocked_until = 0.0

    def block_for(self, seconds: float) -> None:
        """Mark the provider unusable for a while (after a server-side 429)."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    def available(self) -> bool:
        if self.rpm_limit <= 0:
            with self._lock:
                return time.monotonic() >= self._blocked_until

        now = time.monotonic()
        with self._lock:
            if now < self._blocked_until:
                return False
            while self._events and now - self._events[0] > 60.0:
                self._events.popleft()
            return len(self._events) < self.rpm_limit

    def record(self) -> None:
        with self._lock:
            self._events.append(time.monotonic())

    def seconds_until_available(self) -> float:
        """How long until this provider can be used again. 0.0 means now."""
        now = time.monotonic()
        with self._lock:
            blocked_for = max(self._blocked_until - now, 0.0)
            if self.rpm_limit <= 0:
                return blocked_for
            while self._events and now - self._events[0] > 60.0:
                self._events.popleft()
            if len(self._events) < self.rpm_limit:
                return blocked_for
            # The oldest request in the window has to age out first.
            return max(blocked_for, 60.0 - (now - self._events[0]))

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._blocked_until = 0.0


# -- provider ---------------------------------------------------------------


class Provider(ABC):
    """One translation backend."""

    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg
        self.name = cfg.name or cfg.model or cfg.kind
        self.limiter = RateLimiter(cfg.rpm_limit)

    @abstractmethod
    def stream(self, request: TranslationRequest) -> Iterator[str]:
        """Yield translation text chunks as they arrive."""

    def translate(self, request: TranslationRequest) -> str:
        """Non-streaming convenience wrapper."""
        return "".join(self.stream(request))

    def warmup(self) -> None:
        """Open the connection early so the first real request skips the
        TLS handshake - worth 80-150 ms on the very first subtitle."""

    def list_models(self) -> List[str]:
        """Model IDs this provider currently offers.

        Providers retire models on their own schedule, so a hard-coded name in a
        config file goes stale without warning. `gametrans models` calls this so
        the correct name can always be discovered from the service itself.
        """
        raise NotImplementedError(f"{self.name}: listing models is not supported")

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<{type(self).__name__} {self.name} model={self.cfg.model}>"


def build_provider(cfg: ProviderConfig) -> Provider:
    """Instantiate a provider from its config entry."""
    kind = (cfg.kind or "openai").lower()

    if kind == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(cfg)
    if kind == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(cfg)
    if kind in {"openai", "openai_compat", "groq", "cerebras", "openrouter", "lmstudio"}:
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(cfg)
    if kind == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)

    raise ValueError(f"Unknown provider kind: {cfg.kind!r}")


def resolve_api_key(cfg: ProviderConfig) -> Optional[str]:
    """Read the provider's key from its configured environment variable."""
    import os

    if not cfg.api_key_env:
        return None
    key = os.environ.get(cfg.api_key_env, "").strip()
    return key or None
