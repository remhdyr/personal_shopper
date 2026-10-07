"""PS Auction adapter (Klevu public search)."""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

from ..logging_setup import get_logger
from ..models import Listing
from ..models import Source as SourceEnum
from ._auction_html import (
    in_price_range,
)
from ._http import request_with_retry
from .base import SearchQuery, Source

log = get_logger(__name__)

_SEARCH_URL = "https://eucs15.ksearchnet.com/cloud-search/n-search/search"
_TICKET = "klevu-15682769483569331"
_STOCKHOLM = ZoneInfo("Europe/Stockholm")


class PSAuctionSource(Source):
    """Adapter for PS Auction.

    psauction.com is fronted by AWS WAF for direct page requests. The public
    site search is powered by Klevu, which exposes the same listing data without
    requiring a browser-issued WAF token.
    """

    name = "psauction"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; shopper/0.1)",
                "Accept": "application/xml,text/xml",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: SearchQuery) -> list[Listing]:
        params = {
            "ticket": _TICKET,
            "term": query.text,
            "paginationStartsFrom": "0",
            "noOfResults": str(min(max(query.limit, 1), 100)),
            "showOutOfStockProducts": "false",
            "klevuSort": "rel",
        }
        try:
            resp = await request_with_retry(self._client, "GET", _SEARCH_URL, params=params)
        except httpx.HTTPError as exc:
            log.error("PS Auction search failed for %r: %s", query.text, exc)
            return []

        return self._parse(resp.text, query)

    def _parse(self, text: str, query: SearchQuery) -> list[Listing]:
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            log.error("PS Auction response parse error: %s", exc)
            return []

        listings: list[Listing] = []
        seen: set[str] = set()
        for item in root.findall("result"):
            listing = self._to_listing(item)
            if listing is None or listing.source_id in seen:
                continue
            seen.add(listing.source_id)
            if not in_price_range(listing.price, query.min_price, query.max_price):
                continue
            listings.append(listing)
            if len(listings) >= query.limit:
                break
        return listings

    def _to_listing(self, item: ET.Element) -> Listing | None:
        source_id = _text(item, "id") or _text(item, "sku")
        if not source_id:
            return None

        title = _text(item, "name") or _text(item, "shortDesc")
        description = _description(item)
        image = _text(item, "imageUrl") or _text(item, "image")

        return Listing(
            source=SourceEnum.PSAUCTION,
            source_id=source_id,
            title=title,
            description=description,
            price=_parse_price(_text(item, "price") or _text(item, "basePrice")),
            currency=_text(item, "currency") or _text(item, "storeBaseCurrency") or "SEK",
            url=_text(item, "url"),
            image_urls=[image] if image else [],
            location=_text(item, "location"),
            ends_at=_parse_ending_time(_text(item, "endingTime")),
            known_shipping_cost=_known_shipping_cost(item),
        )


def _text(item: ET.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _description(item: ET.Element) -> str:
    shipping_type = _text(item, "shipping_type")
    delivery_info = _text(item, "deliveryInfo")
    parts = [
        f"PS Auction shipping: {shipping_type}" if shipping_type else "",
        f"Delivery details: {delivery_info}" if delivery_info else "",
        _text(item, "shortDesc"),
        _text(item, "category"),
        _text(item, "auctionTitle"),
    ]
    return "\n".join(part for part in parts if part)


def _known_shipping_cost(item: ET.Element) -> float | None:
    shipping_type = _text(item, "shipping_type").casefold()
    free_shipping = _text(item, "freeShipping").casefold()
    if "shipping included" in shipping_type or free_shipping in {"1", "true", "yes"}:
        return 0.0
    return None


def _parse_price(raw: str) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _parse_ending_time(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=_STOCKHOLM).astimezone(UTC)