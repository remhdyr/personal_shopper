"""Tests for the shared HTTP retry helper."""

from __future__ import annotations

import httpx
import pytest

from shopper.sources._http import RateLimitError, request_with_retry


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_returns_on_first_success():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        resp = await request_with_retry(client, "GET", "https://x/", base_delay=0)

    assert resp.json() == {"ok": True}
    assert calls == 1


async def test_retries_on_503_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        resp = await request_with_retry(client, "GET", "https://x/", base_delay=0)

    assert resp.status_code == 200
    assert calls == 3


async def test_does_not_retry_on_404():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await request_with_retry(client, "GET", "https://x/", base_delay=0)

    assert calls == 1


async def test_gives_up_after_attempts_on_persistent_5xx():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await request_with_retry(client, "GET", "https://x/", attempts=3, base_delay=0)

    assert calls == 3


async def test_retries_on_timeout():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200)

    async with _client(handler) as client:
        resp = await request_with_retry(client, "GET", "https://x/", base_delay=0)

    assert resp.status_code == 200
    assert calls == 2


async def test_raises_rate_limit_with_retry_after_without_retrying():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "12"})

    async with _client(handler) as client:
        with pytest.raises(RateLimitError) as exc_info:
            await request_with_retry(client, "GET", "https://x/", base_delay=0)

    assert calls == 1
    assert exc_info.value.retry_after == 12
