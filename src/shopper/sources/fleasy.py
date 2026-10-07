"""Fleasy / Trollets Loppis adapter (Shopify store).

Fleasy.se hosts Trollets Loppis as a Shopify store. Shopify exposes a public,
unauthenticated ``/collections/<handle>/products.json`` endpoint that returns
all active products in JSON. Since there is no server-side keyword search, we
fetch the full collection and filter client-side. Searches share an immutable
catalog cache refreshed every five minutes, which suits the service's
ten-minute polling interval. This is best-effort: Shopify can remove public
storefront JSON access at any time.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import httpx

from ..logging_setup import get_logger
from ..models import Listing
from ..models import Source as SourceEnum
from ._http import request_with_retry
from .base import SearchQuery, Source

log = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_BASE_URL = "https://fleasy.se"
_PRODUCTS_URL = f"{_BASE_URL}/collections/trollets-loppis/products.json"
# Refresh midway between the service's ten-minute polls.
_CATALOG_TTL_SECONDS = 5 * 60
_Catalog = tuple[Mapping[str, Any], ...]


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


def _matches(product: Mapping[str, Any], query: SearchQuery) -> bool:
    """Return True if the product text is relevant to the search query."""
    needle = query.text.lower()
    title = (product.get("title") or "").lower()
    body = _strip_html(str(product.get("body_html") or "")).lower()
    tags = " ".join(product.get("tags") or []).lower()
    return needle in title or needle in body or needle in tags


class FleasySource(Source):
    """Adapter for the Trollets Loppis shop on Fleasy.se (Shopify)."""

    name = "fleasy"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; shopper/0.1)",
                "Accept": "application/json",
            },
        )
        self._catalog: _Catalog | None = None
        self._catalog_fetched_at: float | None = None
        self._catalog_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: SearchQuery) -> list[Listing]:
        catalog = await self._get_catalog()
        if catalog is None:
            return []

        listings: list[Listing] = []
        for product in catalog:
            if _matches(product, query):
                listing = self._to_listing(product)
                if listing is not None and self._in_price_range(listing, query):
                    listings.append(listing)
                if len(listings) >= query.limit:
                    return listings

        return listings

    async def _get_catalog(self) -> _Catalog | None:
        now = time.monotonic()
        if self._catalog is not None and self._catalog_fetched_at is not None:
            if now - self._catalog_fetched_at < _CATALOG_TTL_SECONDS:
                return self._catalog

        async with self._catalog_lock:
            now = time.monotonic()
            if self._catalog is not None and self._catalog_fetched_at is not None:
                if now - self._catalog_fetched_at < _CATALOG_TTL_SECONDS:
                    return self._catalog

            catalog = await self._fetch_catalog()
            if catalog is not None:
                self._catalog = catalog
                self._catalog_fetched_at = time.monotonic()
            return catalog

    async def _fetch_catalog(self) -> _Catalog | None:
        products: list[dict[str, Any]] = []
        page = 1
        while True:
            params = {"limit": "250", "page": str(page)}
            try:
                resp = await request_with_retry(
                    self._client, "GET", _PRODUCTS_URL, params=params
                )
            except httpx.HTTPError as exc:
                log.error("Fleasy search failed: %s", exc)
                return None

            try:
                data = resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                log.error("Fleasy response parse error: %s", exc)
                return None

            if not isinstance(data, dict):
                log.error("Fleasy response parse error: expected a JSON object")
                return None

            page_products = data.get("products")
            if not isinstance(page_products, list) or not all(
                isinstance(product, dict) for product in page_products
            ):
                log.error("Fleasy response parse error: expected a product list")
                return None

            if not page_products:
                break

            products.extend(page_products)

            if len(page_products) < 250:
                break  # last page
            page += 1

        return tuple(MappingProxyType(dict(product)) for product in products)

    def _in_price_range(self, listing: Listing, query: SearchQuery) -> bool:
        if listing.price is None:
            return True
        if query.min_price is not None and listing.price < query.min_price:
            return False
        if query.max_price is not None and listing.price > query.max_price:
            return False
        return True

    def _to_listing(self, product: Mapping[str, Any]) -> Listing | None:
        pid = str(product.get("id") or "")
        if not pid:
            return None

        title = str(product.get("title") or "")
        handle = product.get("handle") or pid
        url = f"{_BASE_URL}/products/{handle}"
        description = _strip_html(str(product.get("body_html") or ""))

        # Price: cheapest available variant in SEK (Shopify stores price as a
        # string like "650.00").
        price: float | None = None
        for variant in product.get("variants") or []:
            try:
                v_price = float(variant.get("price") or "0")
            except (TypeError, ValueError):
                continue
            if v_price > 0 and (price is None or v_price < price):
                price = v_price

        images: list[str] = [
            img["src"]
            for img in (product.get("images") or [])
            if isinstance(img, dict) and img.get("src")
        ]

        return Listing(
            source=SourceEnum.FLEASY,
            source_id=pid,
            title=title,
            description=description,
            price=price,
            currency="SEK",
            url=url,
            image_urls=images,
        )
