"""Shared HTTP helpers for the streaming providers.

Both the Gemini and the OpenAI-compatible backends need the same mapping from
an HTTP status to one of the router's failover signals, so it lives here rather
than in either provider.
"""

from __future__ import annotations

import re
from typing import Optional

import httpx

from .base import AuthError, ModelNotFoundError, ProviderError, RateLimitError

DEFAULT_RETRY_AFTER_S = 20.0

# Providers retire models constantly. Google's 404 body names the replacement,
# e.g. "Please update your code to use models/gemini-3.5-flash-lite" - worth
# capturing so the app can recover instead of just reporting a dead end.
_SUGGESTED_MODEL_RE = re.compile(r"use\s+models/([A-Za-z0-9._\-]+)")


def raise_for_status(response: httpx.Response, provider_name: str) -> None:
    """Map an error response onto the exception the router knows how to act on.

    * 429            -> RateLimitError, and the router moves to the next provider
    * 401 / 403      -> AuthError, and the provider is shelved for the session
    * 5xx and others -> ProviderError, a one-off failover
    """
    if response.status_code < 400:
        return

    # The body has not been consumed yet on a streaming response.
    response.read()
    body = response.text[:400]
    status = response.status_code

    if status == 429:
        raise RateLimitError(
            f"{provider_name}: rate limited - {body}", retry_after_seconds(response)
        )
    if status in (401, 403):
        raise AuthError(f"{provider_name}: auth rejected ({status}) - {body}")
    if 500 <= status < 600:
        raise ProviderError(f"{provider_name}: server error {status} - {body}")
    if status == 404:
        raise ModelNotFoundError(
            f"{provider_name}: model not available - {body}",
            suggested_model=suggested_model(body),
        )
    raise ProviderError(f"{provider_name}: HTTP {status} - {body}")


def suggested_model(body: str) -> Optional[str]:
    """Pull a replacement model name out of a provider's error message."""
    match = _SUGGESTED_MODEL_RE.search(body or "")
    return match.group(1) if match else None


def retry_after_seconds(
    response: httpx.Response, default: float = DEFAULT_RETRY_AFTER_S
) -> float:
    """Seconds to shelve a provider for, from its Retry-After header."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return default
