"""Blocket adapter (best-effort, unofficial).

Blocket has no public API. When ``BLOCKET_TOKEN`` is set, this adapter calls the
internal search endpoint used by logged-in browser sessions. Without a token it
falls back to the public search page's schema.org JSON-LD product list. Treat
both paths as fragile: Blocket can change or block them at any time.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from html import unescape

import httpx

from ..logging_setup import get_logger
from ..models import Listing
from ..models import Source as SourceEnum
from ._auction_html import in_price_range, query_matches
from .base import SearchQuery, Source

log = get_logger(__name__)

_SEARCH_URL = "https://api.blocket.se/search_bff/v2/content"
_PUBLIC_SEARCH_URL = "https://www.blocket.se/recommerce/forsale/search"
_ITEM_URL = "https://www.blocket.se/recommerce/forsale/item/{id}"
_PUBLIC_JSON_RE = re.compile(
    r'<script\b[^>]*id=["\']seoStructuredData["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_DETAIL_STATE_RE = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\('
    r'("(?:\\.|[^"\\])*")\);',
    re.S,
)
_SHIPPING_COST_RE = re.compile(r"\bfrakt(?:\s+från)?\s+(\d[\d\s\xa0]*)\s*kr\b", re.I)


def _parse_list_time(raw: object) -> datetime | None:
    """Parse Blocket's ``list_time`` (ISO 8601) into a datetime, tolerantly."""

    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class BlocketSource(Source):
    name = "blocket"

    def __init__(self, token: str = "") -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; shopper/0.1)",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def enrich(self, listing: Listing) -> None:
        try:
            resp = await self._client.get(listing.url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.debug("Blocket detail fetch failed for %s: %s", listing.source_id, exc)
            return
        seller, seller_is_business, location, shipping_details, shipping_cost = (
            _detail_metadata(resp.text)
        )
        listing.seller = seller
        listing.seller_is_business = seller_is_business
        if location:
            listing.location = location
        if shipping_details:
            listing.description = f"{shipping_details}\n{listing.description}".strip()
        if shipping_cost is not None:
            listing.known_shipping_cost = shipping_cost

    async def search(self, query: SearchQuery) -> list[Listing]:
        if not self._token:
            return await self._search_public_page(query)

        params: dict[str, str] = {
            "q": query.text,
            "lim": str(query.limit),
            "sort": "rel",
        }
        if query.min_price is not None:
            params["price_min"] = f"{query.min_price:.0f}"
        if query.max_price is not None:
            params["price_max"] = f"{query.max_price:.0f}"

        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            resp = await self._client.get(_SEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("Blocket search failed for %r: %s", query.text, exc)
            return []

        data = resp.json().get("data") or []
        return [self._to_listing(ad) for ad in data if ad.get("ad_id") or ad.get("id")]

    async def _search_public_page(self, query: SearchQuery) -> list[Listing]:
        params: dict[str, str] = {"q": query.text}
        try:
            resp = await self._client.get(_PUBLIC_SEARCH_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("Blocket public search failed for %r: %s", query.text, exc)
            return []

        return self._parse_public_page(resp.text, query)

    def _parse_public_page(self, text: str, query: SearchQuery) -> list[Listing]:
        match = _PUBLIC_JSON_RE.search(text)
        if match is None:
            log.error("Blocket public search response did not include structured data")
            return []

        try:
            data = json.loads(unescape(match.group(1)))
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("Blocket public structured data parse error: %s", exc)
            return []

        entries = data.get("mainEntity", {}).get("itemListElement") or []
        listings: list[Listing] = []
        seen: set[str] = set()
        for entry in entries:
            product = entry.get("item") if isinstance(entry, dict) else None
            if not isinstance(product, dict):
                continue
            listing = self._product_to_listing(product)
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

    def _product_to_listing(self, product: dict) -> Listing | None:
        url = str(product.get("url") or "")
        source_id = url.rstrip("/").rsplit("/", 1)[-1]
        if not source_id:
            return None

        offers = product.get("offers") or {}
        price = None
        try:
            price = float(str(offers.get("price") or "").replace(" ", ""))
        except (TypeError, ValueError):
            price = None

        image = product.get("image")
        images = [str(image)] if image else []

        return Listing(
            source=SourceEnum.BLOCKET,
            source_id=source_id,
            title=str(product.get("name") or ""),
            description=str(product.get("description") or ""),
            price=price,
            currency=str(offers.get("priceCurrency") or "SEK"),
            url=url,
            image_urls=images,
        )

    def _to_listing(self, ad: dict) -> Listing:
        ad_id = str(ad.get("ad_id") or ad.get("id"))
        price = None
        price_info = ad.get("price") or {}
        if isinstance(price_info, dict) and "value" in price_info:
            try:
                price = float(price_info["value"])
            except (TypeError, ValueError):
                price = None

        images: list[str] = []
        for img in ad.get("images") or []:
            if isinstance(img, dict) and (url := img.get("url")):
                images.append(url)

        location = ""
        for loc in ad.get("location") or []:
            if isinstance(loc, dict) and loc.get("name"):
                location = loc["name"]
                break

        return Listing(
            source=SourceEnum.BLOCKET,
            source_id=ad_id,
            title=ad.get("subject", ""),
            description=ad.get("body", ""),
            price=price,
            currency="SEK",
            url=ad.get("share_url") or _ITEM_URL.format(id=ad_id),
            image_urls=images,
            location=location,
            seller=_seller_from_ad(ad),
            posted_at=_parse_list_time(ad.get("list_time")),
        )


def _seller_from_detail(text: str) -> str:
    return _detail_metadata(text)[0]


def _detail_metadata(text: str) -> tuple[str, bool | None, str, str, float | None]:
    detail = _detail_payload(text)
    if not isinstance(detail, dict):
        return "", None, "", "", None

    shop = detail.get("shopProfileData")
    seller = str(shop.get("name") or "").strip() if isinstance(shop, dict) else ""
    if isinstance(shop, dict):
        seller_is_business: bool | None = True
    elif "shopProfileData" in detail and shop is None:
        seller_is_business = False
    else:
        seller_is_business = None

    item = detail.get("itemData")
    location_data = item.get("location") if isinstance(item, dict) else None
    if isinstance(location_data, dict):
        postal_name = str(location_data.get("postalName") or "").strip()
        postal_code = str(location_data.get("postalCode") or "").strip()
        location = ", ".join(part for part in (postal_name, postal_code) if part)
    else:
        location = ""

    transaction = detail.get("transactableData")
    shipping_details = ""
    shipping_cost = None
    if isinstance(transaction, dict):
        if transaction.get("eligibleForShipping") is True:
            opted_in = _nested_dict(detail, "transactableUiData", "sections", "sidebar", "optedIn")
            shipping_price = opted_in.get("shippingPrice")
            shipping_text = (
                str(shipping_price.get("text") or "").strip()
                if isinstance(shipping_price, dict)
                else ""
            )
            if shipping_text:
                shipping_details = f"Blocket shipping offered: {shipping_text.rstrip('.')}."
                cost_match = _SHIPPING_COST_RE.search(shipping_text)
                if cost_match:
                    shipping_cost = float(cost_match.group(1).replace(" ", "").replace("\xa0", ""))
            else:
                shipping_details = "Blocket shipping is offered; cost is not stated."
            if shipping_cost is None and transaction.get("sellerPaysShipping") is True:
                shipping_cost = 0.0
        else:
            shipping_details = "Blocket shipping is not currently offered; pickup is required."

    return seller, seller_is_business, location, shipping_details, shipping_cost


def _detail_payload(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = _DETAIL_STATE_RE.search(text)
        if match is None:
            return None
        try:
            data = json.loads(json.loads(match.group(1)))
        except (json.JSONDecodeError, TypeError):
            return None

    if not isinstance(data, dict):
        return None
    if "itemData" in data:
        return data
    try:
        detail = data["loaderData"]["item-recommerce"]
    except (KeyError, TypeError):
        return None
    return detail if isinstance(detail, dict) else None


def _nested_dict(value: object, *keys: str) -> dict:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _seller_from_ad(ad: dict) -> str:
    seller = ad.get("seller") or ad.get("seller_info") or ad.get("advertiser") or {}
    if not isinstance(seller, dict):
        return ""
    return str(seller.get("name") or seller.get("company_name") or "").strip()
