"""Tradera adapter (best-effort).

Tradera exposes a SOAP API. There is no lightweight JSON endpoint, so this
adapter builds a minimal SOAP 1.1 envelope for ``SearchService.Search`` and
parses the XML response. It is wrapped in defensive error handling so any
breakage degrades gracefully rather than crashing the pipeline.

Register an app at https://api.tradera.com/ to obtain an AppId and AppKey.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

from ..logging_setup import get_logger
from ..models import Listing
from ..models import Source as SourceEnum
from ._http import RateLimitError, RateLimitGate, request_with_retry
from .base import SearchQuery, Source

log = get_logger(__name__)

_ENDPOINT = "https://api.tradera.com/v3/SearchService.asmx"
_ITEM_ENDPOINT = "https://api.tradera.com/v3/PublicService.asmx"
_NS = "http://api.tradera.com"
_ITEM_URL = "https://www.tradera.com/item/{id}"

_ENVELOPE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:t="http://api.tradera.com">
  <soap:Header>
    <t:AuthenticationHeader>
      <t:AppId>{app_id}</t:AppId>
      <t:AppKey>{app_key}</t:AppKey>
    </t:AuthenticationHeader>
    <t:ConfigurationHeader>
      <t:Sandbox>0</t:Sandbox>
      <t:MaxResultAge>0</t:MaxResultAge>
    </t:ConfigurationHeader>
  </soap:Header>
  <soap:Body>
    <t:Search>
      <t:query>{query}</t:query>
      <t:categoryId>0</t:categoryId>
      <t:pageNumber>1</t:pageNumber>
      <t:orderBy>Relevance</t:orderBy>
    </t:Search>
  </soap:Body>
</soap:Envelope>"""

_ITEM_ENVELOPE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:t="http://api.tradera.com">
  <soap:Header>
    <t:AuthenticationHeader>
      <t:AppId>{app_id}</t:AppId>
      <t:AppKey>{app_key}</t:AppKey>
    </t:AuthenticationHeader>
    <t:ConfigurationHeader>
      <t:Sandbox>0</t:Sandbox>
      <t:MaxResultAge>0</t:MaxResultAge>
    </t:ConfigurationHeader>
  </soap:Header>
  <soap:Body>
    <t:GetItem>
      <t:itemId>{item_id}</t:itemId>
    </t:GetItem>
  </soap:Body>
</soap:Envelope>"""


def _text(node: ET.Element | None) -> str:
    return node.text or "" if node is not None else ""


def _localname(tag: str) -> str:
    """Strip the ``{namespace}`` prefix from an ElementTree tag."""

    return tag.rsplit("}", 1)[-1]


def _https(url: str) -> str:
    """Upgrade a Tradera image URL to https (they're served on both)."""

    return url.replace("http://", "https://", 1) if url.startswith("http://") else url


def _parse_number(raw: str | None) -> float | None:
    """Parse a Tradera money string tolerantly.

    Tradera usually returns bare integers ("79"), but has been seen to return
    decimals in either separator ("79.00", "79,00") and with thousands spaces
    ("1 250"). A strict integer-only regex would silently drop those.
    """
    if not raw:
        return None
    cleaned = raw.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        return None
    return float(cleaned)


class TraderaSource(Source):
    name = "tradera"

    def __init__(self, app_id: str, app_key: str) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._client = httpx.AsyncClient(timeout=20)
        self._rate_limit = RateLimitGate()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: SearchQuery) -> list[Listing]:
        if not self._app_id or not self._app_key:
            log.warning("Tradera credentials missing; skipping")
            return []
        if self._rate_limit.blocked():
            return []

        body = _ENVELOPE.format(
            app_id=html.escape(self._app_id),
            app_key=html.escape(self._app_key),
            query=html.escape(query.text),
        )
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f"{_NS}/Search",
        }
        try:
            resp = await request_with_retry(
                self._client, "POST", _ENDPOINT, content=body, headers=headers
            )
        except RateLimitError as exc:
            self._rate_limit.trip(exc.retry_after)
            log.warning("Tradera rate limited; pausing searches for %.0fs", exc.retry_after)
            return []
        except httpx.HTTPError as exc:
            log.error("Tradera search failed for %r: %s", query.text, exc)
            return []

        try:
            listings = self._parse(resp.text)
        except ET.ParseError as exc:
            log.error("Tradera response parse error: %s", exc)
            return []

        return self._apply_price_filter(listings, query)

    async def enrich(self, listing: Listing) -> None:
        """Fill in shipping cost and full-size images via ``PublicService.GetItem``.

        ``SearchService.Search`` only returns a low-res ``ThumbnailLink`` and no
        shipping data, so this single per-item lookup upgrades both. Best-effort:
        any failure leaves the search-provided values in place.
        """
        if not self._app_id or not self._app_key:
            return

        body = _ITEM_ENVELOPE.format(
            app_id=html.escape(self._app_id),
            app_key=html.escape(self._app_key),
            item_id=html.escape(listing.source_id),
        )
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f"{_NS}/GetItem",
        }
        try:
            resp = await self._client.post(_ITEM_ENDPOINT, content=body, headers=headers)
            resp.raise_for_status()
            cost = self._parse_shipping_cost(resp.text)
            images = self._parse_images(resp.text)
        except (httpx.HTTPError, ET.ParseError) as exc:
            log.debug("Tradera GetItem failed for %s: %s", listing.source_id, exc)
            return

        if cost is not None:
            listing.known_shipping_cost = cost
        # Replace the low-res search thumbnail with the full-size images.
        if images:
            listing.image_urls = images
        ends_at = self._parse_end_date(resp.text)
        if ends_at is not None:
            listing.ends_at = ends_at

    def _parse_shipping_cost(self, xml: str) -> float | None:
        root = ET.fromstring(xml)
        costs: list[float] = []
        for option in root.iter(f"{{{_NS}}}ShippingOptions"):
            cost = _parse_number(_text(option.find(f"{{{_NS}}}Cost")))
            if cost is not None:
                costs.append(cost)
        return min(costs) if costs else None

    def _parse_end_date(self, xml: str) -> datetime | None:
        """Extract the auction end time from a GetItem response.

        Tradera returns ``EndDate`` as an ISO-8601 ``dateTime`` (e.g.
        ``2026-08-15T18:30:00``). It carries no timezone; Tradera operates in
        Swedish local time, so we localise to Europe/Stockholm and convert to
        UTC. Any parse failure leaves ``ends_at`` unset (best-effort).
        """
        root = ET.fromstring(xml)
        raw = _text(root.find(f".//{{{_NS}}}EndDate")).strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Stockholm"))
        return parsed.astimezone(UTC)

    def _parse_images(self, xml: str) -> list[str]:
        """Extract full-size image URLs from a GetItem response.

        ``DetailedImageLinks`` offers several sizes tagged by ``Format``; we
        want the full-size ``normal`` variant (the ``/images/`` path). If those
        are absent we fall back to the ``ImageLinks`` string array, which also
        points at ``/images/``. URLs are upgraded to https.
        """
        root = ET.fromstring(xml)
        normal: list[str] = []
        for link in root.iter(f"{{{_NS}}}DetailedImageLinks"):
            fmt = _text(link.find(f"{{{_NS}}}Format")).strip().lower()
            url = _text(link.find(f"{{{_NS}}}Url")).strip()
            if url and fmt == "normal":
                normal.append(_https(url))
        if normal:
            return normal
        # Fallback: the ImageLinks <string> array (namespaceless children).
        fallback: list[str] = []
        for container in root.iter(f"{{{_NS}}}ImageLinks"):
            for child in container:
                if _localname(child.tag) == "string" and child.text:
                    fallback.append(_https(child.text.strip()))
        return fallback

    def _parse(self, xml: str) -> list[Listing]:
        root = ET.fromstring(xml)
        listings: list[Listing] = []
        # Despite the singular-sounding name, each result is wrapped in its own
        # <Items> element (not <Item>), namespaced with the Tradera namespace.
        for item in root.iter(f"{{{_NS}}}Items"):
            item_id = _text(item.find(f"{{{_NS}}}Id"))
            if not item_id:
                continue
            title = _text(item.find(f"{{{_NS}}}ShortDescription"))
            description = _text(item.find(f"{{{_NS}}}LongDescription"))
            thumb = _text(item.find(f"{{{_NS}}}ThumbnailLink"))
            price = self._extract_price(item)
            listings.append(
                Listing(
                    source=SourceEnum.TRADERA,
                    source_id=item_id,
                    title=title,
                    description=description,
                    price=price,
                    currency="SEK",
                    url=_ITEM_URL.format(id=item_id),
                    image_urls=[thumb] if thumb else [],
                )
            )
        return listings

    def _extract_price(self, item: ET.Element) -> float | None:
        for tag in ("BuyItNowPrice", "MaxBid", "OpeningBid", "NextBid"):
            price = _parse_number(_text(item.find(f"{{{_NS}}}{tag}")))
            if price is not None:
                return price
        return None

    def _apply_price_filter(
        self, listings: list[Listing], query: SearchQuery
    ) -> list[Listing]:
        out: list[Listing] = []
        for ltng in listings:
            if ltng.price is not None:
                if query.min_price is not None and ltng.price < query.min_price:
                    continue
                if query.max_price is not None and ltng.price > query.max_price:
                    continue
            out.append(ltng)
        return out
