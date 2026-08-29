"""Anthropic Claude provider (optional, paid).

Not part of the default free chain - included because Claude's Persian output is
the most idiomatic of any model here, which matters for story-heavy games.

`claude-haiku-4-5` is the default: it is the lowest-latency Claude model, and
subtitle translation is a short, well-specified task that does not benefit from
a larger model's reasoning. Set `model = "claude-opus-5"` in the config for
noticeably better prose at noticeably higher latency and cost.

Thinking is deliberately left off (the parameter is simply omitted) - on a
one-line subtitle it would add hundreds of milliseconds for no quality gain.
"""

from __future__ import annotations

import logging
from typing import Iterator

from ..config import ProviderConfig
from .base import (
    AuthError,
    Provider,
    ProviderError,
    RateLimitError,
    TranslationRequest,
    build_system_prompt,
    resolve_api_key,
)

log = logging.getLogger(__name__)


class AnthropicProvider(Provider):
    kind = "anthropic"

    def __init__(self, cfg: ProviderConfig) -> None:
        super().__init__(cfg)
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ProviderError(
                f"{self.name}: install the SDK with `pip install gametrans[anthropic]`"
            ) from exc

        self._anthropic = anthropic
        api_key = resolve_api_key(cfg)
        client_kwargs = {
            "timeout": cfg.timeout_s,
            "max_retries": 0,  # the router handles failover; retries add latency
        }
        if api_key:
            client_kwargs["api_key"] = api_key
        if cfg.base_url:
            client_kwargs["base_url"] = cfg.base_url

        try:
            self._client = anthropic.Anthropic(**client_kwargs)
        except Exception as exc:
            raise AuthError(f"{self.name}: {exc}") from exc

        self._model = cfg.model or "claude-haiku-4-5"

    def stream(self, request: TranslationRequest) -> Iterator[str]:
        anthropic = self._anthropic
        kwargs = {
            "model": self._model,
            "max_tokens": self.cfg.max_output_tokens,
            "temperature": self.cfg.temperature,
            "system": build_system_prompt(request),
            "messages": [{"role": "user", "content": request.text}],
        }
        if self.cfg.extra:
            kwargs.update(self.cfg.extra)

        try:
            with self._client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    if text:
                        yield text
        except anthropic.RateLimitError as exc:
            raise RateLimitError(f"{self.name}: rate limited - {exc}", 20.0) from exc
        except anthropic.AuthenticationError as exc:
            raise AuthError(f"{self.name}: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"{self.name}: HTTP {exc.status_code} - {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"{self.name}: connection failed - {exc}") from exc

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - shutdown best effort
            pass
