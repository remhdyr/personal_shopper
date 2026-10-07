"""Small async HTTP retry helper shared by source adapters.

Marketplace APIs occasionally hiccup with a timeout, a dropped connection, or a
transient 429/5xx. A single retry with backoff turns most of those into a
successful poll instead of a skipped source, without adding a dependency.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from ..logging_setup import get_logger

log = get_logger(__name__)

# Statuses worth retrying: rate limiting and transient server-side errors.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RateLimitError(httpx.HTTPStatusError):
    """HTTP 429 with the provider's requested retry delay, when available."""

    def __init__(
        self, response: httpx.Response, request: httpx.Request, retry_after: float
    ) -> None:
        super().__init__("Too many requests", request=request, response=response)
        self.retry_after = retry_after


class RateLimitGate:
    """Short per-source cooldown after a provider returns HTTP 429."""

    def __init__(self, default_seconds: float = 60.0) -> None:
        self._default_seconds = default_seconds
        self._blocked_until = 0.0

    def blocked(self) -> bool:
        return time.monotonic() < self._blocked_until

    def trip(self, seconds: float | None = None) -> None:
        self._blocked_until = max(
            self._blocked_until,
            time.monotonic() + (seconds if seconds is not None else self._default_seconds),
        )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    **kwargs: Any,
) -> httpx.Response:
    """Perform an HTTP request, retrying transient failures with backoff.

    Retries on connection/timeout errors and on 429/5xx responses using
    exponential backoff with jitter. Non-retryable HTTP errors (e.g. 4xx other
    than 429) propagate immediately; the last error is re-raised if every
    attempt fails.
    """
    last_exc: Exception
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raw_retry_after = exc.response.headers.get("Retry-After", "")
                try:
                    retry_after = max(float(raw_retry_after), 1.0)
                except ValueError:
                    retry_after = 60.0
                raise RateLimitError(
                    exc.response, exc.request, retry_after
                ) from exc
            if exc.response.status_code not in _RETRYABLE_STATUS or attempt == attempts:
                raise
            last_exc = exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt == attempts:
                raise
            last_exc = exc

        delay = min(max_delay, base_delay * 2 ** (attempt - 1))
        delay += random.uniform(0, delay / 2)
        log.debug(
            "Retrying %s %s (attempt %d/%d) after %s in %.2fs",
            method,
            url,
            attempt,
            attempts,
            last_exc,
            delay,
        )
        await asyncio.sleep(delay)

    raise last_exc
