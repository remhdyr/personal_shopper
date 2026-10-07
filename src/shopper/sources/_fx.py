"""ECB daily FX rates → convert marketplace prices to/from SEK.

eBay's Browse API doesn't serve the Swedish marketplace (``EBAY_SE`` is
unsupported), so listings come back in EUR/GBP/USD/etc. The rest of the app
reasons in SEK, so we convert both the outgoing price filter (SEK → market
currency) and the incoming listing prices (market currency → SEK) using the
European Central Bank's free, key-less daily reference rates.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET

import httpx

from ..logging_setup import get_logger
from ._http import request_with_retry

log = get_logger(__name__)

# ECB publishes "units of currency X per 1 EUR"; EUR itself is the base (1.0).
_ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_ONE_DAY = 86400.0


def parse_ecb_rates(xml_text: str) -> dict[str, float]:
    """Parse the ECB daily XML into ``{currency: units_per_EUR}`` (EUR = 1.0)."""

    root = ET.fromstring(xml_text)
    rates: dict[str, float] = {"EUR": 1.0}
    for cube in root.iter():
        # Rate rows look like <Cube currency="SEK" rate="11.30"/>.
        ccy = cube.get("currency")
        raw = cube.get("rate")
        if ccy and raw:
            try:
                rates[ccy] = float(raw)
            except ValueError:
                continue
    return rates


def derive_sek_rate(rates: dict[str, float], currency: str) -> float | None:
    """SEK per 1 unit of ``currency``, derived from EUR-based ECB rates."""

    if currency == "SEK":
        return 1.0
    sek = rates.get("SEK")
    base = rates.get(currency)
    if sek is None or base is None or base == 0:
        return None
    return sek / base


class CurrencyConverter:
    """Fetches and caches ECB daily rates, sharing one httpx client."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._rates: dict[str, float] = {}
        self._fetched_at: float = 0.0

    async def _rates_cached(self) -> dict[str, float]:
        if self._rates and time.time() - self._fetched_at < _ONE_DAY:
            return self._rates
        resp = await request_with_retry(self._client, "GET", _ECB_DAILY_URL)
        self._rates = parse_ecb_rates(resp.text)
        self._fetched_at = time.time()
        return self._rates

    async def sek_per_unit(self, currency: str) -> float | None:
        """Return SEK per 1 unit of ``currency``, or ``None`` if unavailable."""

        if currency == "SEK":
            return 1.0
        try:
            rates = await self._rates_cached()
        except httpx.HTTPError as exc:
            log.error("FX rate fetch failed: %s", exc)
            return None
        rate = derive_sek_rate(rates, currency)
        if rate is None:
            log.error("No ECB rate for %s; cannot convert to SEK", currency)
        return rate
