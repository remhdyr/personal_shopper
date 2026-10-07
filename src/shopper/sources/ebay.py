"""eBay Browse API adapter.

Uses the official Browse API (``/buy/browse/v1/item_summary/search``) with an
application access token obtained via the OAuth2 client-credentials grant. This
is the free, supported path and does not require a user login.
"""

from __future__ import annotations

import base64
import time

import httpx

from ..logging_setup import get_logger
from ..models import Listing
from ..models import Source as SourceEnum
from ._fx import CurrencyConverter
from ._http import RateLimitError, RateLimitGate, request_with_retry
from ._units import add_metric
from .base import SearchQuery, Source

log = get_logger(__name__)

_ENV_HOSTS = {
    "production": {
        "auth": "https://api.ebay.com/identity/v1/oauth2/token",
        "api": "https://api.ebay.com",
    },
    "sandbox": {
        "auth": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "api": "https://api.sandbox.ebay.com",
    },
}
_SCOPE = "https://api.ebay.com/oauth/api_scope"

# The Browse API doesn't support EBAY_SE, so listings arrive in the market's own
# currency; map each supported marketplace to it so prices can be normalized to
# SEK (the currency the rest of the app reasons in).
_MARKETPLACE_CURRENCY = {
    "EBAY_SE": "SEK",
    "EBAY_DE": "EUR",
    "EBAY_FR": "EUR",
    "EBAY_IT": "EUR",
    "EBAY_ES": "EUR",
    "EBAY_IE": "EUR",
    "EBAY_NL": "EUR",
    "EBAY_AT": "EUR",
    "EBAY_BE": "EUR",
    "EBAY_GB": "GBP",
    "EBAY_US": "USD",
    "EBAY_CA": "CAD",
    "EBAY_AU": "AUD",
    "EBAY_CH": "CHF",
    "EBAY_PL": "PLN",
    "EBAY_HK": "HKD",
    "EBAY_SG": "SGD",
}


class EbaySource(Source):
    name = "ebay"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        marketplace: str = "EBAY_SE",
        env: str = "production",
        used_only: bool = True,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._marketplace = marketplace
        self._used_only = used_only
        # Currency the marketplace prices in; prices are converted to SEK.
        self._currency = _MARKETPLACE_CURRENCY.get(marketplace, "EUR")
        hosts = _ENV_HOSTS.get(env, _ENV_HOSTS["production"])
        self._auth_url = hosts["auth"]
        self._api_base = hosts["api"]
        self._client = httpx.AsyncClient(timeout=20)
        self._fx = CurrencyConverter(self._client)
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._rate_limit = RateLimitGate()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        creds = f"{self._client_id}:{self._client_secret}".encode()
        headers = {
            "Authorization": f"Basic {base64.b64encode(creds).decode()}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "client_credentials", "scope": _SCOPE}
        resp = await request_with_retry(
            self._client, "POST", self._auth_url, headers=headers, data=data
        )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 7200))
        return self._token

    def _build_filter(
        self,
        query: SearchQuery,
        *,
        sek_per_unit: float = 1.0,
        currency: str = "SEK",
    ) -> str | None:
        # eBay's filter field is a comma-joined list of clauses.
        clauses: list[str] = []
        # Restrict to used items so the feed isn't swamped by identical new
        # stock from thousands of retailers.
        if self._used_only:
            clauses.append("conditions:{USED}")
        # Price bounds are expressed in SEK; convert them to the marketplace's
        # own currency for the API filter (sek_per_unit=1.0 leaves SEK as-is).
        if query.min_price is not None or query.max_price is not None:
            lo = "" if query.min_price is None else f"{query.min_price / sek_per_unit:.0f}"
            hi = "" if query.max_price is None else f"{query.max_price / sek_per_unit:.0f}"
            # eBay expects price:[min..max] plus a currency qualifier.
            clauses.append(f"price:[{lo}..{hi}],priceCurrency:{currency}")
        return ",".join(clauses) if clauses else None

    async def search(self, query: SearchQuery) -> list[Listing]:
        if not self._client_id or not self._client_secret:
            log.warning("eBay credentials missing; skipping")
            return []
        if self._rate_limit.blocked():
            return []
        try:
            token = await self._get_token()
        except httpx.HTTPError as exc:
            log.error("eBay auth failed: %s", exc)
            return []

        # Prices come back in the marketplace's currency but the app reasons in
        # SEK, so we need an FX rate to convert both the filter and the results.
        # Bail out rather than risk treating e.g. EUR prices as SEK.
        sek_per_unit = await self._fx.sek_per_unit(self._currency)
        if sek_per_unit is None:
            log.error(
                "eBay: no SEK rate for %s; skipping to avoid mispricing", self._currency
            )
            return []

        params: dict[str, str] = {"q": query.text, "limit": str(query.limit)}
        price_filter = self._build_filter(
            query, sek_per_unit=sek_per_unit, currency=self._currency
        )
        if price_filter:
            params["filter"] = price_filter

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self._marketplace,
            "Content-Type": "application/json",
        }
        url = f"{self._api_base}/buy/browse/v1/item_summary/search"
        try:
            resp = await request_with_retry(
                self._client, "GET", url, params=params, headers=headers
            )
        except RateLimitError as exc:
            self._rate_limit.trip(exc.retry_after)
            log.warning("eBay rate limited; pausing searches for %.0fs", exc.retry_after)
            return []
        except httpx.HTTPError as exc:
            log.error("eBay search failed for %r: %s", query.text, exc)
            return []

        items = resp.json().get("itemSummaries") or []
        return [
            self._to_sek(self._to_listing(item), sek_per_unit)
            for item in items
            if item.get("itemId")
        ]

    def _to_sek(self, listing: Listing, sek_per_unit: float) -> Listing:
        """Normalize a listing's price to SEK using the market's FX rate."""

        # Shipping is quoted in the marketplace's currency (same as the price),
        # so convert it on the same rate. 0 (free) must stay 0, not become None.
        if listing.known_shipping_cost is not None and listing.currency != "SEK":
            listing.known_shipping_cost = round(listing.known_shipping_cost * sek_per_unit, 2)
        if listing.price is not None and listing.currency != "SEK":
            listing.price = round(listing.price * sek_per_unit, 2)
            listing.currency = "SEK"
        return listing

    @staticmethod
    def _parse_shipping_cost(item: dict) -> float | None:
        """Cheapest fixed shipping cost from a Browse item, in its own currency.

        eBay lists one or more ``shippingOptions``, each with a ``shippingCost``
        ``{value, currency}``. Free shipping is a real ``0.0`` (so landed cost
        counts it as free, not unknown). Calculated/undeliverable options carry
        no value; if none has a numeric cost we return ``None`` so logistics
        keeps using the driving-cost fallback.
        """
        costs: list[float] = []
        for option in item.get("shippingOptions") or []:
            if not isinstance(option, dict):
                continue
            raw = (option.get("shippingCost") or {}).get("value")
            try:
                costs.append(float(raw))
            except (TypeError, ValueError):
                continue
        return min(costs) if costs else None

    def _to_listing(self, item: dict) -> Listing:
        price_info = item.get("price") or {}
        try:
            price = float(price_info["value"]) if "value" in price_info else None
        except (TypeError, ValueError):
            price = None

        images: list[str] = []
        if img := item.get("image", {}).get("imageUrl"):
            images.append(img)
        for extra in item.get("additionalImages") or []:
            if url := extra.get("imageUrl"):
                images.append(url)

        location = ""
        loc = item.get("itemLocation") or {}
        if loc:
            location = ", ".join(
                str(v) for v in (loc.get("city"), loc.get("country")) if v
            )

        return Listing(
            source=SourceEnum.EBAY,
            source_id=str(item["itemId"]),
            # eBay's US/UK marketplaces use imperial units; annotate them with a
            # metric value so the buyer (who thinks in cm/kg) can read them.
            title=add_metric(item.get("title", "")),
            description=add_metric(item.get("shortDescription", "")),
            price=price,
            currency=price_info.get("currency", "SEK"),
            url=item.get("itemWebUrl", ""),
            image_urls=images,
            location=location,
            # Raw shipping in the marketplace currency; _to_sek converts it.
            known_shipping_cost=self._parse_shipping_cost(item),
        )
