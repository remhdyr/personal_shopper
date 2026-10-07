from __future__ import annotations

import asyncio

import httpx

from shopper.sources import fleasy
from shopper.sources.base import SearchQuery
from shopper.sources.fleasy import FleasySource


def _product(product_id: int, title: str, price: str = "100.00") -> dict:
    return {
        "id": product_id,
        "title": title,
        "body_html": f"<p>{title} description</p>",
        "handle": f"product-{product_id}",
        "tags": [],
        "variants": [{"price": price}],
        "images": [],
    }


async def test_concurrent_searches_share_all_catalog_pages():
    calls: list[str] = []
    first_page = [
        _product(1, "Vintage hammer"),
        _product(2, "Hand saw"),
        *(_product(product_id, f"Filler product {product_id}") for product_id in range(3, 251)),
    ]
    second_page = [_product(251, "Fine saw")]

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        calls.append(page)
        products = first_page if page == "1" else second_page
        return httpx.Response(200, json={"products": products})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = FleasySource(client=client)
    try:
        hammer, saw = await asyncio.gather(
            source.search(SearchQuery(text="hammer")),
            source.search(SearchQuery(text="saw")),
        )
    finally:
        await source.aclose()

    assert calls.count("1") == 1
    assert calls.count("2") == 1
    assert [listing.source_id for listing in hammer] == ["1"]
    assert [listing.source_id for listing in saw] == ["2", "251"]


async def test_expired_catalog_is_refetched(monkeypatch):
    now = 100.0
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"products": [_product(calls, "Hammer")]})

    monkeypatch.setattr(fleasy.time, "monotonic", lambda: now)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = FleasySource(client=client)
    try:
        first = await source.search(SearchQuery(text="hammer"))
        now += fleasy._CATALOG_TTL_SECONDS + 1
        second = await source.search(SearchQuery(text="hammer"))
    finally:
        await source.aclose()

    assert calls == 2
    assert [listing.source_id for listing in first] == ["1"]
    assert [listing.source_id for listing in second] == ["2"]


async def test_failed_fetch_is_not_cached():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(404)
        return httpx.Response(200, json={"products": [_product(1, "Hammer")]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = FleasySource(client=client)
    try:
        first = await source.search(SearchQuery(text="hammer"))
        second = await source.search(SearchQuery(text="hammer"))
    finally:
        await source.aclose()

    assert first == []
    assert [listing.source_id for listing in second] == ["1"]
    assert calls == 2
