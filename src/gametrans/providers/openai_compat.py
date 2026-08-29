"""OpenAI-compatible chat-completions provider.

One class covers every endpoint that speaks the OpenAI wire format:

* Groq       - https://api.groq.com/openai/v1     (fastest free inference)
* Cerebras   - https://api.cerebras.ai/v1
* OpenRouter - https://openrouter.ai/api/v1       (has `:free` models)
* Ollama     - http://127.0.0.1:11434/v1          (fully offline)
* LM Studio  - http://127.0.0.1:1234/v1
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterator, List

import httpx

from ..config import ProviderConfig
from .base import (
    AuthError,
    Provider,
    ProviderError,
    TranslationRequest,
    build_system_prompt,
    resolve_api_key,
)
from .http_util import raise_for_status

log = logging.getLogger(__name__)


class OpenAICompatProvider(Provider):
    kind = "openai"

    def __init__(self, cfg: ProviderConfig) -> None:
        super().__init__(cfg)
        if not cfg.base_url:
            raise ValueError(f"{self.name}: base_url is required for an OpenAI-compatible provider")
        self._base_url = cfg.base_url.rstrip("/")

        api_key = resolve_api_key(cfg)
        # Local servers (Ollama, LM Studio) accept any key, including none.
        is_local = "127.0.0.1" in self._base_url or "localhost" in self._base_url
        if not api_key and not is_local:
            raise AuthError(f"{self.name}: set {cfg.api_key_env or 'the API key env var'}")

        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        elif is_local:
            headers["authorization"] = "Bearer local"

        self._client = httpx.Client(
            timeout=httpx.Timeout(cfg.timeout_s, connect=min(cfg.timeout_s, 5.0)),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            headers=headers,
        )
        # Extra fields this server has rejected; never sent again this session.
        self._blocked_extras: set = set()

    def _payload(self, request: TranslationRequest) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": build_system_prompt(request)},
                {"role": "user", "content": request.text},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_output_tokens,
            "stream": True,
        }
        payload.update(self._live_extras())
        return payload

    def stream(self, request: TranslationRequest) -> Iterator[str]:
        url = f"{self._base_url}/chat/completions"

        # Provider-specific fields such as `reasoning_effort` are accepted by
        # some servers and rejected outright by others, and which is which
        # changes between versions. Rather than maintain a compatibility table,
        # send them and drop what the server objects to. Dropping only the named
        # field matters: the others may be doing real work - `keep_alive` stops
        # a local model unloading mid-game, which costs a ten second reload on
        # the next line.
        for attempt in range(3):
            produced = False
            try:
                for piece in self._stream_once(url, request):
                    produced = True
                    yield piece
            except ProviderError as exc:
                if produced or not _is_bad_request(exc) or not self._blocked_extras_shrinkable():
                    raise
                rejected = _rejected_fields(str(exc), self._live_extras())
                if rejected:
                    log.info("%s rejected %s; retrying without it",
                             self.name, ", ".join(sorted(rejected)))
                    self._blocked_extras |= rejected
                else:
                    log.info("%s rejected the extra request fields; retrying without them",
                             self.name)
                    self._blocked_extras |= set(self.cfg.extra)
                continue
            return

    def _live_extras(self) -> Dict[str, Any]:
        """Extra fields not yet rejected by this server."""
        return {
            key: value
            for key, value in (self.cfg.extra or {}).items()
            if key not in self._blocked_extras
        }

    def _blocked_extras_shrinkable(self) -> bool:
        return bool(self._live_extras())

    def _stream_once(self, url: str, request: TranslationRequest) -> Iterator[str]:
        payload = self._payload(request)

        try:
            with self._client.stream("POST", url, json=payload) as response:
                raise_for_status(response, self.name)
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    text = _delta_text(chunk)
                    if text:
                        yield text
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name}: timed out after {self.cfg.timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc

    def warmup(self) -> None:
        try:
            self._client.get(f"{self._base_url}/models", timeout=4.0)
        except httpx.HTTPError:
            pass

    def list_models(self) -> List[str]:
        try:
            response = self._client.get(f"{self._base_url}/models", timeout=10.0)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc
        raise_for_status(response, self.name)

        payload = response.json()
        entries = payload.get("data") or payload.get("models") or []
        return sorted(
            entry.get("id") or entry.get("name", "")
            for entry in entries
            if isinstance(entry, dict)
        )

    def close(self) -> None:
        self._client.close()


def _delta_text(chunk: Dict[str, Any]) -> str:
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str):
        return content
    # Some gateways send the OpenAI "content parts" shape instead of a string.
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    # Non-streaming fallback, in case a server ignored `stream: true`.
    message = choice.get("message") or {}
    if isinstance(message.get("content"), str):
        return message["content"]
    return ""


def _is_bad_request(error: Exception) -> bool:
    """A 400 means the server disliked the request itself, not the network."""
    message = str(error)
    return "HTTP 400" in message or "400 -" in message


def _rejected_fields(message: str, candidates: Dict[str, Any]) -> set:
    """Which of our extra fields the error names, if any.

    Servers usually quote the offending parameter, which lets the retry keep
    everything else rather than throwing away settings that were working.
    """
    lowered = message.lower()
    return {key for key in candidates if key.lower() in lowered}
