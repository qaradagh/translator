"""Shared HTTP helpers for the streaming providers.

Both the Gemini and the OpenAI-compatible backends need the same mapping from
an HTTP status to one of the router's failover signals, so it lives here rather
than in either provider.
"""

from __future__ import annotations

import httpx

from .base import AuthError, ProviderError, RateLimitError

DEFAULT_RETRY_AFTER_S = 20.0


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
    raise ProviderError(f"{provider_name}: HTTP {status} - {body}")


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
