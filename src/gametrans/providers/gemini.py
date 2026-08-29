"""Google Gemini provider.

Default backend: the free tier is the best Persian quality available for free,
and `gemini-2.5-flash-lite` is the lowest-latency model in the family.

Two settings matter for latency:
* `thinkingBudget: 0` - the 2.5 family will otherwise spend hundreds of
  milliseconds reasoning before emitting a single token. For a one-line subtitle
  that is pure overhead.
* A persistent HTTP/2 client - keeps the TLS session alive between subtitles.
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

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(Provider):
    kind = "gemini"

    def __init__(self, cfg: ProviderConfig) -> None:
        super().__init__(cfg)
        self._base_url = (cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = resolve_api_key(cfg)
        if not self._api_key:
            raise AuthError(
                f"{self.name}: set {cfg.api_key_env or 'GEMINI_API_KEY'} "
                "(free key: https://aistudio.google.com/apikey)"
            )
        self._client = httpx.Client(
            http2=False,
            timeout=httpx.Timeout(cfg.timeout_s, connect=min(cfg.timeout_s, 5.0)),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            headers={
                "x-goog-api-key": self._api_key,
                "content-type": "application/json",
            },
        )

    # -- request building ----------------------------------------------------

    def _payload(self, request: TranslationRequest) -> Dict[str, Any]:
        generation: Dict[str, Any] = {
            "temperature": self.cfg.temperature,
            "maxOutputTokens": self.cfg.max_output_tokens,
            "responseMimeType": "text/plain",
            # Disable reasoning tokens; a subtitle line needs none and they are
            # the single largest source of latency on the 2.5 models.
            "thinkingConfig": {"thinkingBudget": 0},
        }
        payload: Dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": build_system_prompt(request)}]},
            "contents": [{"role": "user", "parts": [{"text": request.text}]}],
            "generationConfig": generation,
            # Subtitles in violent games trip the default safety thresholds and
            # come back empty; relax them so dialogue still gets translated.
            "safetySettings": [
                {"category": category, "threshold": "BLOCK_NONE"}
                for category in (
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                )
            ],
        }
        if self.cfg.extra:
            _deep_merge(payload, self.cfg.extra)
        return payload

    # -- streaming -----------------------------------------------------------

    def stream(self, request: TranslationRequest) -> Iterator[str]:
        url = f"{self._base_url}/models/{self.cfg.model}:streamGenerateContent?alt=sse"
        payload = self._payload(request)

        try:
            with self._client.stream("POST", url, json=payload) as response:
                raise_for_status(response, self.name)
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for piece in _extract_text(chunk):
                        yield piece
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name}: timed out after {self.cfg.timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc

    def warmup(self) -> None:
        try:
            self._client.get(f"{self._base_url}/models/{self.cfg.model}", timeout=4.0)
        except httpx.HTTPError:
            pass

    def list_models(self) -> List[str]:
        try:
            response = self._client.get(f"{self._base_url}/models", timeout=10.0)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc
        raise_for_status(response, self.name)

        names = []
        for model in response.json().get("models", []):
            methods = model.get("supportedGenerationMethods") or []
            # Only models we can actually stream a translation from.
            if methods and "generateContent" not in methods:
                continue
            name = model.get("name", "")
            names.append(name.split("/", 1)[1] if name.startswith("models/") else name)
        return sorted(names)

    def close(self) -> None:
        self._client.close()


def _extract_text(chunk: Dict[str, Any]) -> Iterator[str]:
    for candidate in chunk.get("candidates", []) or []:
        content = candidate.get("content") or {}
        for part in content.get("parts", []) or []:
            text = part.get("text")
            if text:
                yield text


def _deep_merge(target: Dict[str, Any], overrides: Dict[str, Any]) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
