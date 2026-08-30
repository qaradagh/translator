"""Ollama via its native API rather than the OpenAI-compatible shim.

The compatibility endpoint is convenient but hides the settings that decide how
fast a local model answers. Going native exposes them:

* `num_ctx` - the context window actually allocated. Ollama's default is sized
  for chat transcripts; a subtitle is a short prompt and a short answer, and a
  smaller window means less KV cache to allocate and stride through.
* `num_predict` - a hard ceiling on generated tokens, so a model that starts
  rambling cannot stall the next line.
* `keep_alive` - how long the model stays resident. Without it an idle model
  unloads mid-game and the next subtitle waits out a full reload.
* `think` - the documented way to switch off a reasoning model's thinking,
  rather than hoping a vendor-specific field is understood.

This matters most for someone running local-only, where every one of those is
the difference between usable and not.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterator, List

import httpx

from ..config import ProviderConfig
from .base import (
    Provider,
    ProviderError,
    TranslationRequest,
    build_system_prompt,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"


class OllamaProvider(Provider):
    kind = "ollama"

    def __init__(self, cfg: ProviderConfig) -> None:
        super().__init__(cfg)
        base = (cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        # Accept an OpenAI-style URL too, so switching `kind` in an existing
        # config does not also require editing the address.
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        self._base_url = base

        self._client = httpx.Client(
            timeout=httpx.Timeout(cfg.timeout_s, connect=min(cfg.timeout_s, 5.0)),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=4),
            headers={"content-type": "application/json"},
        )

    # -- request building ----------------------------------------------------

    def _payload(self, request: TranslationRequest) -> Dict[str, Any]:
        extra = dict(self.cfg.extra or {})
        options = {
            "temperature": self.cfg.temperature,
            "num_predict": self.cfg.max_output_tokens,
        }
        options.update(extra.pop("options", {}) or {})

        system_prompt = build_system_prompt(request)
        if _wants_no_think_token(self.cfg.model):
            # Some builds ignore the `think` flag below and answer with their
            # entire chain of thought. Qwen also honours this literal token,
            # which those builds do respect.
            system_prompt += "\n/no_think"

        payload: Dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.text},
            ],
            "stream": True,
            "options": options,
        }
        # Reasoning models otherwise spend seconds per line on thoughts nobody
        # reads. Harmless on models without a thinking mode.
        payload.setdefault("think", False)
        payload.update(extra)
        return payload

    # -- streaming -----------------------------------------------------------

    def stream(self, request: TranslationRequest) -> Iterator[str]:
        url = f"{self._base_url}/api/chat"
        payload = self._payload(request)

        try:
            with self._client.stream("POST", url, json=payload) as response:
                if response.status_code >= 400:
                    response.read()
                    raise ProviderError(
                        f"{self.name}: HTTP {response.status_code} - {response.text[:300]}"
                    )
                # Native Ollama streams newline-delimited JSON, not SSE.
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise ProviderError(f"{self.name}: {chunk['error']}")
                    piece = (chunk.get("message") or {}).get("content")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name}: timed out after {self.cfg.timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: {exc} - is Ollama running? Start it and try again."
            ) from exc

    # -- housekeeping --------------------------------------------------------

    def warmup(self) -> None:
        """Load the model now, so the first subtitle is not the slow one."""
        try:
            self._client.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self.cfg.model,
                    "messages": [],
                    "stream": False,
                    "keep_alive": (self.cfg.extra or {}).get("keep_alive", "30m"),
                },
                timeout=min(self.cfg.timeout_s, 120.0),
            )
        except httpx.HTTPError as exc:
            log.debug("%s warmup failed: %s", self.name, exc)

    def list_models(self) -> List[str]:
        try:
            response = self._client.get(f"{self._base_url}/api/tags", timeout=10.0)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.name}: {exc} - is Ollama running?"
            ) from exc
        if response.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {response.status_code}")
        return sorted(
            entry.get("name", "") for entry in response.json().get("models", [])
        )

    def close(self) -> None:
        self._client.close()


def _wants_no_think_token(model: str) -> bool:
    """Whether this model family recognises the literal `/no_think` token."""
    return "qwen" in (model or "").lower()
