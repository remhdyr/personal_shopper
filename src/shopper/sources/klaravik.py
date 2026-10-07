"""Klaravik adapter (best-effort, public HTML search)."""

from __future__ import annotations

import re

import httpx

from ..logging_setup import get_logger
from ..models import Listing
from ..models import Source as SourceEnum
from ._auction_html import (
    absolute_url,
    attr,
    class_text,
    in_price_range,
    parse_iso_datetime,
    parse_sek_price,
    plain_text,
    query_matches,
)
from ._http import request_with_retry
from .base import SearchQuery, Source

log = get_logger(__name__)

_BASE_URL = "https://www.klaravik.se"
_SEARCH_URL = f"{_BASE_URL}/auktion/"
_IMAGE_RE = re.compile(
    r"<img\b[^>]*(?:src|data-src|data-original)=[\"']([^\"']+)[\"']",
    re.I,
)
_CARD_RE = re.compile(
    r"<a\b[^>]*href=[\"'](?P<href>/auktion/produkt/[^\"']+)[\"'][^>]*>.*?</a>",
    re.I | re.S,
)
_ID_RE = re.compile(r"/produkt/(\d+)-")
_RESERVE_RE = re.compile(
    r'"resPriceReached"\s*:\s*(true|false).*?"zeroReserve"\s*:\s*(true|false)',
    re.I | re.S,
)
_FREIGHT_RE = re.compile(
    r'<div\b[^>]*class=["\'][^"\']*\bobject-freight__content\b[^"\']*["\'][^>]*>'
    r"(.*?)</div>",
    re.I | re.S,
)


class KlaravikSource(Source):
    """Adapter for Klaravik machine, vehicle and tool auctions."""

    name = "klaravik"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; shopper/0.1)",
                "Accept": "text/html",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def enrich(self, listing: Listing) -> None:
        try:
            resp = await request_with_retry(self._client, "GET", listing.url)
        except httpx.HTTPError as exc:
            log.debug("Klaravik detail fetch failed for %s: %s", listing.source_id, exc)
            return

        location = class_text(resp.text, "object-position__municipallity").rstrip(".")
        if location:
            listing.location = location
        shipping_details = _shipping_details_from_detail(resp.text)
        if shipping_details:
            listing.description = (
                f"Klaravik shipping: {shipping_details}\n{listing.description}"
            ).strip()

        reserve_status = _reserve_status_from_detail(resp.text)
        if reserve_status is not None:
            listing.reserve_price_reached = reserve_status
            label = "reached" if reserve_status else "not reached"
            listing.description = f"{listing.description}\nReserve price: {label}.".strip()

    async def search(self, query: SearchQuery) -> list[Listing]:
        params = {
            "searchtext": query.text,
            "dosearch": "",
            "setperpage": str(min(max(query.limit, 1), 120)),
        }
        try:
            resp = await request_with_retry(self._client, "GET", _SEARCH_URL, params=params)
        except httpx.HTTPError as exc:
            log.error("Klaravik search failed for %r: %s", query.text, exc)
            return []

        return self._parse(resp.text, query)

    def _parse(self, text: str, query: SearchQuery) -> list[Listing]:
        listings: list[Listing] = []
        seen: set[str] = set()
        for match in _CARD_RE.finditer(text):
            listing = self._to_listing(match.group(0), match.group("href"))
            if listing is None or listing.source_id in seen:
                continue
            seen.add(listing.source_id)
            if not in_price_range(listing.price, query.min_price, query.max_price):
                continue
            if not query_matches(f"{listing.title} {listing.description}", query.text):
                continue
            listings.append(listing)
            if len(listings) >= query.limit:
                break
        return listings

    def _to_listing(self, block: str, href: str) -> Listing | None:
        source_id = attr(block, "data-prod-id")
        if not source_id:
            id_match = _ID_RE.search(href)
            source_id = id_match.group(1) if id_match else ""
        if not source_id:
            return None

        title = (
            attr(block, "title")
            or attr(block, "alt")
            or class_text(block, "product_card__title")
        )
        url = absolute_url(_BASE_URL, href)
        image_urls = [absolute_url(_BASE_URL, src) for src in _image_sources(block)]
        description = plain_text(block)

        return Listing(
            source=SourceEnum.KLARAVIK,
            source_id=source_id,
            title=title,
            description=description,
            price=parse_sek_price(block),
            currency="SEK",
            url=url,
            image_urls=image_urls,
            location=class_text(block, "product_card__info-text"),
            posted_at=parse_iso_datetime(attr(block, "data-auction-start")),
            ends_at=parse_iso_datetime(attr(block, "data-auction-close")),
        )


def _image_sources(block: str) -> list[str]:
    sources: list[str] = []
    for src in _IMAGE_RE.findall(block):
        if "/images/icon" in src or src.endswith(".svg"):
            continue
        if src not in sources:
            sources.append(src)
    return sources


def _reserve_status_from_detail(text: str) -> bool | None:
    match = _RESERVE_RE.search(text)
    if match is None:
        return None
    reserve_reached = match.group(1).lower() == "true"
    no_reserve = match.group(2).lower() == "true"
    return reserve_reached or no_reserve


def _shipping_details_from_detail(text: str) -> str:
    match = _FREIGHT_RE.search(text)
    return plain_text(match.group(1)) if match else ""