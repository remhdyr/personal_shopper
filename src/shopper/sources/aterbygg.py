"""Återbygg adapter (best-effort).

Återbygg (aterbygg.nu) is a Swedish second-hand building-materials store.
This adapter first tries the public WooCommerce Store API (``/wp-json/wc/store/v1``),
which is unauthenticated and returns JSON directly. If that endpoint returns a
non-JSON or error response the adapter falls back to the WooCommerce v2 REST
API (which is also publicly readable for product listings on most installs).
Both approaches are best-effort: the site can disable these endpoints or change
its platform at any time. It is disabled by default in ``config.yaml``.
"""

from __future__ import annotations

import json

import httpx

from ..logging_setup import get_logger
from ..models import Listing
from ..models import Source as SourceEnum
from ._http import request_with_retry
from .base import SearchQuery, Source

log = get_logger(__name__)

_BASE_URL = "https://aterbygg.nu"
# WooCommerce Store API (no credentials needed, available on most WC stores).
_STORE_API = f"{_BASE_URL}/wp-json/wc/store/v1/products"
# WooCommerce REST API v2 (public product listing, no consumer keys needed for
# read-only access when the site permits it).
_REST_API = f"{_BASE_URL}/wp-json/wc/v2/products"


class AterbyggSource(Source):
    """Adapter for Återbygg second-hand building materials (aterbygg.nu)."""

    name = "aterbygg"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; shopper/0.1)",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: SearchQuery) -> list[Listing]:
        listings = await self._search_store_api(query)
        if listings is None:
            listings = await self._search_rest_api(query)
        return listings or []

    # ------------------------------------------------------------------
    # WooCommerce Store API (preferred)
    # ------------------------------------------------------------------

    async def _search_store_api(self, query: SearchQuery) -> list[Listing] | None:
        """Try the public WooCommerce Store API. Returns None on failure."""
        params: dict[str, str] = {
            "search": query.text,
            "per_page": str(min(query.limit, 100)),
        }
        if query.min_price is not None:
            params["min_price"] = f"{query.min_price:.0f}"
        if query.max_price is not None:
            params["max_price"] = f"{query.max_price:.0f}"

        try:
            resp = await request_with_retry(self._client, "GET", _STORE_API, params=params)
        except httpx.HTTPError as exc:
            log.debug("Återbygg Store API failed (%s); will try REST API", exc)
            return None

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            log.debug("Återbygg Store API returned non-JSON; will try REST API")
            return None

        if not isinstance(data, list):
            log.debug("Unexpected Store API shape; will try REST API")
            return None

        listings = [self._store_to_listing(p) for p in data if p.get("id")]
        return [lt for lt in listings if lt is not None]

    def _store_to_listing(self, p: dict) -> Listing | None:
        pid = str(p.get("id") or "")
        if not pid:
            return None

        title = str(p.get("name") or "")
        url = str(p.get("permalink") or f"{_BASE_URL}/?p={pid}")
        description = self._plain(
            str(p.get("description") or "") or str(p.get("short_description") or "")
        )

        price = self._parse_price(p.get("prices", {}).get("price") or p.get("price") or "")

        images: list[str] = []
        for img in p.get("images") or []:
            if isinstance(img, dict) and (src := img.get("src") or img.get("url")):
                images.append(src)

        return Listing(
            source=SourceEnum.ATERBYGG,
            source_id=pid,
            title=title,
            description=description,
            price=price,
            currency="SEK",
            url=url,
            image_urls=images,
        )

    # ------------------------------------------------------------------
    # WooCommerce REST API v2 (fallback)
    # ------------------------------------------------------------------

    async def _search_rest_api(self, query: SearchQuery) -> list[Listing] | None:
        """Try the WooCommerce REST API v2 as a fallback. Returns None on failure."""
        params: dict[str, str] = {
            "search": query.text,
            "per_page": str(min(query.limit, 100)),
            "status": "publish",
        }
        if query.min_price is not None:
            params["min_price"] = f"{query.min_price:.0f}"
        if query.max_price is not None:
            params["max_price"] = f"{query.max_price:.0f}"

        try:
            resp = await request_with_retry(self._client, "GET", _REST_API, params=params)
        except httpx.HTTPError as exc:
            log.error("Återbygg search failed (both APIs): %s", exc)
            return None

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("Återbygg REST API parse error: %s", exc)
            return None

        if not isinstance(data, list):
            log.error("Unexpected Återbygg REST API response shape")
            return None

        listings = [self._rest_to_listing(p) for p in data if p.get("id")]
        return [lt for lt in listings if lt is not None]

    def _rest_to_listing(self, p: dict) -> Listing | None:
        pid = str(p.get("id") or "")
        if not pid:
            return None

        title = str(p.get("name") or "")
        url = str(p.get("permalink") or f"{_BASE_URL}/?p={pid}")
        description = self._plain(
            str(p.get("description") or "") or str(p.get("short_description") or "")
        )
        price = self._parse_price(p.get("price") or p.get("regular_price") or "")

        images: list[str] = []
        for img in p.get("images") or []:
            if isinstance(img, dict) and (src := img.get("src")):
                images.append(src)

        return Listing(
            source=SourceEnum.ATERBYGG,
            source_id=pid,
            title=title,
            description=description,
            price=price,
            currency="SEK",
            url=url,
            image_urls=images,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_price(raw: object) -> float | None:
        if not raw:
            return None
        try:
            return float(str(raw).replace(",", ".").strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _plain(text: str) -> str:
        """Strip basic HTML tags from a WooCommerce description."""
        import html as _html
        import re as _re

        return _re.sub(r"\s+", " ", _html.unescape(_re.sub(r"<[^>]+>", " ", text))).strip()
