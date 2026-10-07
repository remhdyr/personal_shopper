"""Blinto adapter (best-effort, public HTML listings)."""

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
    parse_sek_price,
    plain_text,
    query_matches,
)
from ._http import request_with_retry
from .base import SearchQuery, Source

log = get_logger(__name__)

_BASE_URL = "https://www.blinto.se"
_SEARCH_URL = f"{_BASE_URL}/"
_PICKUP_DETAILS = (
    "Seller-provided shipping: unavailable. Buyer must arrange pickup or freight "
    "from the listed location."
)
_IMAGE_RE = re.compile(
    r"<img\b[^>]*(?:src|actualimg|data-src|data-original)=[\"']([^\"']+)[\"']",
    re.I,
)
_CARD_RE = re.compile(
    r"<a\b[^>]*href=[\"'](?P<href>/auction/[^\"']+)[\"'][^>]*>.*?</a>",
    re.I | re.S,
)
_RESERVE_STATUS_RE = re.compile(
    r'<li\b[^>]*\bid=["\']li-resprice-reached(?:_[^"\']*)?["\'][^>]*>(.*?)</li>',
    re.I | re.S,
)


class BlintoSource(Source):
    """Adapter for Blinto industrial auctions."""

    name = "blinto"

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
            log.debug("Blinto detail fetch failed for %s: %s", listing.source_id, exc)
            return

        reserve_status = _reserve_status_from_detail(resp.text)
        if reserve_status is None:
            return
        listing.reserve_price_reached = reserve_status
        label = "reached" if reserve_status else "not reached"
        listing.description = f"{listing.description}\nReserve price: {label}.".strip()

    async def search(self, query: SearchQuery) -> list[Listing]:
        try:
            resp = await request_with_retry(
                self._client, "GET", _SEARCH_URL, params={"keywords": query.text}
            )
        except httpx.HTTPError as exc:
            log.error("Blinto search failed for %r: %s", query.text, exc)
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
        source_id = href.strip("/").rsplit("/", 1)[-1]
        if not source_id:
            return None

        first_line = class_text(block, "brand-type")
        second_line = class_text(block, "h3-second-line")
        title = " ".join(part for part in [first_line, second_line] if part).strip()
        title = attr(block, "alt") or attr(block, "title") or title
        image_urls = [absolute_url(_BASE_URL, src) for src in _image_sources(block)]

        return Listing(
            source=SourceEnum.BLINTO,
            source_id=source_id,
            title=title,
            description=f"{_PICKUP_DETAILS}\n{plain_text(block)}".strip(),
            price=parse_sek_price(block),
            currency="SEK",
            url=absolute_url(_BASE_URL, href),
            image_urls=image_urls,
            location=class_text(block, "card-location"),
        )


def _image_sources(block: str) -> list[str]:
    sources: list[str] = []
    for src in _IMAGE_RE.findall(block):
        src = src.removeprefix("_hidden_")
        if "/images/" in src or src.endswith(".svg"):
            continue
        if src not in sources:
            sources.append(src)
    return sources


def _reserve_status_from_detail(text: str) -> bool | None:
    match = _RESERVE_STATUS_RE.search(text)
    if match is None:
        return None
    status = plain_text(match.group(1)).lower()
    if "ej uppnått" in status:
        return False
    if "uppnått" in status:
        return True
    return None