"""Auctionet adapter.

Auctionet is an online auction marketplace with a public, read-only JSON API
(no credentials required): ``/api/v2/items.json``. We fetch active (not-yet-ended)
lots matching each query and normalise them to :class:`Listing` objects.

Auctions expose a real ``published_at`` timestamp, which the pipeline uses to
tell whether a lot was *just* posted -- exactly the signal that drives a
freshly-posted Telegram alert.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

import httpx

from ..logging_setup import get_logger
from ..models import Listing
from ..models import Source as SourceEnum
from ._http import request_with_retry
from .base import SearchQuery, Source

log = get_logger(__name__)

_ENDPOINT = "https://auctionet.com/api/v2/items.json"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Collapse Auctionet's HTML description/condition into plain text."""

    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


class AuctionetSource(Source):
    name = "auctionet"

    def __init__(self, currency: str = "SEK") -> None:
        self._currency = currency
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
        params = {
            "q": query.text,
            "is": "ended:false",  # only active lots
            "per_page": str(min(query.limit, 50)),
            "page": "1",
        }
        try:
            resp = await request_with_retry(self._client, "GET", _ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            log.error("Auctionet search failed for %r: %s", query.text, exc)
            return []

        try:
            items = resp.json().get("items") or []
        except ValueError as exc:
            log.error("Auctionet response parse error: %s", exc)
            return []

        listings = [self._to_listing(item) for item in items if item.get("id")]
        return [ltng for ltng in listings if self._keep(ltng, query)]

    def _keep(self, listing: Listing, query: SearchQuery) -> bool:
        # The app reasons in one currency; comparing a EUR bid to SEK bounds
        # would be meaningless, so drop other-currency lots outright.
        if listing.currency != self._currency:
            return False
        if listing.price is not None:
            if query.min_price is not None and listing.price < query.min_price:
                return False
            if query.max_price is not None and listing.price > query.max_price:
                return False
        return True

    def _to_listing(self, item: dict) -> Listing:
        # Current cost to lead the auction now: the next valid bid (equals the
        # starting bid when there are no bids yet), falling back to the estimate.
        price = (
            _num(item.get("next_bid_amount"))
            or _num(item.get("starting_bid_amount"))
            or _num(item.get("estimate"))
        )

        posted_at = None
        published = item.get("published_at")
        if isinstance(published, int | float) and not isinstance(published, bool):
            posted_at = datetime.fromtimestamp(published, tz=UTC)

        ends_at = None
        ends = item.get("ends_at")
        if isinstance(ends, int | float) and not isinstance(ends, bool):
            ends_at = datetime.fromtimestamp(ends, tz=UTC)

        images: list[str] = []
        for img in item.get("images") or []:
            if not isinstance(img, dict):
                continue
            url = img.get("w640") or img.get("hd") or img.get("thumb")
            if url:
                images.append(url)

        location = str(item.get("location") or "").strip()

        return Listing(
            source=SourceEnum.AUCTIONET,
            source_id=str(item["id"]),
            title=str(item.get("title") or ""),
            description=self._describe(item),
            price=price,
            currency=str(item.get("currency") or "SEK"),
            url=str(item.get("url") or ""),
            image_urls=images,
            location=location,
            posted_at=posted_at,
            ends_at=ends_at,
        )

    def _describe(self, item: dict) -> str:
        """Fold description, condition and auction context into one text blob."""

        parts: list[str] = []
        if desc := _strip_html(str(item.get("description") or "")):
            parts.append(desc)
        if cond := _strip_html(str(item.get("condition") or "")):
            parts.append(f"Condition: {cond}")
        estimate = _num(item.get("estimate"))
        if estimate is not None:
            currency = str(item.get("currency") or "SEK")
            parts.append(f"Auctioneer estimate: {estimate:.0f} {currency}.")
        if house := str(item.get("house") or "").strip():
            parts.append(f"Auction house: {house}.")
        return "\n".join(parts)
