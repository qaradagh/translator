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
import re
from typing import Any, Dict, Iterator, List

import httpx

from ..config import ProviderConfig
from .base import (
    AuthError,
    ModelNotFoundError,
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
            headers={"content-type": "application/json"},
        )
        # Google issues two key formats: the older "AIza..." standard keys and
        # the newer "AQ.Ab..." auth keys, which are bound to a service account.
        # Both are documented for the x-goog-api-key header, but auth keys are
        # rejected on some paths unless sent as a bearer token. Start with the
        # documented header and fall back once on an auth failure rather than
        # guessing from the prefix - the server is the authority, not the shape
        # of the string. Never send both: the API rejects that as an ambiguous
        # credential.
        self._auth_mode = "api-key"
        # Guards the one-shot model switch below, so a genuinely bad model
        # name cannot send us round in circles.
        self._model_switched = False

    # -- request building ----------------------------------------------------

    def _payload(self, request: TranslationRequest) -> Dict[str, Any]:
        generation: Dict[str, Any] = {
            "temperature": self.cfg.temperature,
            "maxOutputTokens": self.cfg.max_output_tokens,
            "responseMimeType": "text/plain",
            # Reasoning tokens are the single largest source of latency, and a
            # one-line subtitle needs none.
            "thinkingConfig": _thinking_config(self.cfg.model),
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

    def _auth_headers(self, mode: str) -> Dict[str, str]:
        if mode == "bearer":
            return {"authorization": f"Bearer {self._api_key}"}
        return {"x-goog-api-key": self._api_key}

    def _auth_modes(self) -> list:
        """The mode that last worked, then the other one as a fallback."""
        return [self._auth_mode, "bearer" if self._auth_mode == "api-key" else "api-key"]

    def _stream_once(self, url: str, payload: Dict[str, Any], mode: str) -> Iterator[str]:
        try:
            with self._client.stream(
                "POST", url, json=payload, headers=self._auth_headers(mode)
            ) as response:
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

    def stream(self, request: TranslationRequest) -> Iterator[str]:
        try:
            for piece in self._stream_with_auth_retry(request):
                yield piece
        except ModelNotFoundError as exc:
            # Google retires models and names the replacement in the error.
            # Switching to it beats failing a translation the user is waiting on.
            replacement = exc.suggested_model
            if not replacement or replacement == self.cfg.model or self._model_switched:
                raise
            log.warning(
                "%s: model %r is unavailable; switching to %r. "
                "Set `model = \"%s\"` in config.toml to make this permanent.",
                self.name, self.cfg.model, replacement, replacement,
            )
            self.cfg.model = replacement
            self._model_switched = True
            for piece in self._stream_with_auth_retry(request):
                yield piece

    def _stream_with_auth_retry(self, request: TranslationRequest) -> Iterator[str]:
        url = f"{self._base_url}/models/{self.cfg.model}:streamGenerateContent?alt=sse"
        payload = self._payload(request)
        modes = self._auth_modes()

        for index, mode in enumerate(modes):
            produced = False
            try:
                for piece in self._stream_once(url, payload, mode):
                    produced = True
                    yield piece
            except AuthError:
                # Only retry when nothing was emitted yet - a mid-stream failure
                # cannot be replayed without duplicating output.
                if produced or index == len(modes) - 1:
                    raise
                log.info(
                    "%s: key rejected as %s, retrying as %s",
                    self.name, mode, modes[index + 1],
                )
                continue
            # Remember what worked so later subtitles pay no retry cost.
            self._auth_mode = mode
            return

    def warmup(self) -> None:
        try:
            self._client.get(
                f"{self._base_url}/models/{self.cfg.model}",
                headers=self._auth_headers(self._auth_mode),
                timeout=4.0,
            )
        except httpx.HTTPError:
            pass

    def list_models(self) -> List[str]:
        response = None
        for index, mode in enumerate(self._auth_modes()):
            try:
                response = self._client.get(
                    f"{self._base_url}/models",
                    headers=self._auth_headers(mode),
                    timeout=10.0,
                )
            except httpx.HTTPError as exc:
                raise ProviderError(f"{self.name}: {exc}") from exc

            if response.status_code in (401, 403) and index == 0:
                continue
            raise_for_status(response, self.name)
            self._auth_mode = mode
            break
        else:  # pragma: no cover - the loop always breaks or raises
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


_GEMINI_VERSION_RE = re.compile(r"gemini-(\d+)")


def _thinking_config(model: str) -> Dict[str, Any]:
    """The right way to ask for no reasoning, for this model generation.

    Gemini 3.x replaced the numeric `thinkingBudget` with a `thinkingLevel`
    enum, and sending both in one request is a 400 - so pick exactly one.
    """
    match = _GEMINI_VERSION_RE.search(model or "")
    major = int(match.group(1)) if match else 0
    if major >= 3:
        return {"thinkingLevel": "minimal"}
    return {"thinkingBudget": 0}


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
