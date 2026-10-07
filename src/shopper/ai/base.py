"""Provider-agnostic analysis core shared by every AI backend.

The prompt text, JSON schema and system instructions are identical no matter
which model runs them -- that's what keeps ``deal_score``/``fit_score``
calibrated across providers when the load balancer spreads work between them.
Each concrete provider (Gemini, an OpenAI-compatible endpoint, ...) only owns
its transport; everything about *what* to ask lives here.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

import httpx

from ..config import Inventory, LogisticsConfig, SearchConfig, Watchlist
from ..logging_setup import get_logger
from ..models import DealAnalysis, Listing
from ..preferences import PreferenceContext

log = get_logger(__name__)

# JSON schema the model must fill in for each listing. Providers that support
# structured output pass this directly; others get it described in the prompt.
RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "is_relevant": {"type": "boolean"},
        "category": {"type": "string"},
        "condition": {"type": "string"},
        "estimated_value": {"type": "number"},
        "deal_score": {"type": "integer"},
        "fit_score": {"type": "integer"},
        "summary": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "shipping_available": {"type": "boolean"},
        "shipping_cost": {"type": "number"},
        "distance_km": {"type": "number"},
    },
    "required": [
        "is_relevant",
        "deal_score",
        "fit_score",
        "summary",
    ],
}

SYSTEM = (
    "You are a sharp buyer of used machinist equipment, hand tools and power "
    "tools. You judge whether a marketplace listing is a genuinely good deal and "
    "whether it fits a specific buyer's taste. Be skeptical of vague listings, "
    "damaged goods, and overpriced items. Prefer quality brands and complete, "
    "working equipment. Respond ONLY with the requested JSON."
)

# Schema + system prompt for turning free-text ("a small metal lathe under
# 5000 kr") into a structured watch item the buyer can review before saving.
SUGGEST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "queries": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "min_price": {"type": "number"},
        "max_price": {"type": "number"},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
    },
    "required": ["name", "queries"],
}

SUGGEST_SYSTEM = (
    "You help a buyer of used machinist equipment, hand tools and power tools "
    "turn a plain-language wish into a structured 'watch item' for a "
    "marketplace scanner. Given the buyer's request, produce: a short name; "
    "queries = concrete search phrases to run against Swedish/European "
    "marketplaces (include common brand names and both Swedish and English "
    "terms where useful); keywords = extra terms that signal a strong match but "
    "aren't searched directly; notes = guidance on what 'good' looks like and "
    "red flags; optional min_price/max_price in SEK if the buyer implied a "
    "budget; priority (low/normal/high). Respond ONLY with the requested JSON."
)

# Marker summary written by :meth:`PromptBuilder.fallback`; the router uses it
# to tell a genuine "not relevant" verdict apart from a provider that failed.
FALLBACK_SUMMARY = "analysis unavailable"


def is_fallback(analysis: DealAnalysis) -> bool:
    """True if ``analysis`` is a degraded fallback rather than a real verdict."""

    return analysis.summary == FALLBACK_SUMMARY



@runtime_checkable
class Analyzer(Protocol):
    """The surface the pipeline depends on, implemented by every provider.

    Concrete providers and the :class:`RoutingAnalyzer` are interchangeable
    behind this protocol, so the pipeline never knows which model judged a
    listing.
    """

    @property
    def model_label(self) -> str:
        """Identifier recorded on each analysis (e.g. the model name)."""
        ...

    async def analyze(
        self, listing: Listing, context: PreferenceContext | None = None
    ) -> DealAnalysis: ...

    async def suggest_watch_item(self, text: str) -> dict: ...

    def set_watchlist(self, watchlist: Watchlist) -> None: ...

    async def aclose(self) -> None: ...


def _listing_brief(listing: Listing) -> str:
    price = f"{listing.price:.0f} {listing.currency}" if listing.price is not None else "unknown"
    return (
        f"title: {listing.title}\n"
        f"price: {price}\n"
        f"location: {listing.location or 'n/a'}\n"
        f"description: {listing.description[:1500]}"
    )


def _watchlist_brief(watchlist: Watchlist) -> str:
    """Render the buyer's wish list as compact guidance for the model."""

    lines: list[str] = []
    for item in watchlist.items:
        header = f"- {item.name}"
        if item.priority != "normal":
            header += f" [priority: {item.priority}]"
        if item.max_price is not None:
            header += f" (aim below {item.max_price:.0f} SEK)"
        lines.append(header)
        if item.keywords:
            lines.append("    look for: " + ", ".join(item.keywords))
        if item.notes:
            lines.append("    notes: " + item.notes)
    return "\n".join(lines)


def _inventory_brief(inventory: Inventory) -> str:
    """Render owned gear plus its compatible tooling as guidance for the model."""

    lines: list[str] = []
    for item in inventory.items:
        header = f"- {item.name}"
        tag = " ".join(p for p in (item.brand, item.model) if p).strip()
        if tag and tag.lower() not in item.name.lower():
            header += f" ({tag})"
        lines.append(header)
        if item.specs:
            lines.append("    specs: " + ", ".join(f"{k} {v}" for k, v in item.specs.items()))
        if item.accessories:
            lines.append("    already own (accessories): " + "; ".join(item.accessories))
        if item.compatible:
            lines.append("    want compatible tooling: " + "; ".join(item.compatible))
        if item.notes:
            lines.append("    notes: " + item.notes)
    return "\n".join(lines)


class PromptBuilder:
    """Builds the shared prompt/image inputs and parses the model's JSON reply.

    Holds the buyer's context (search bounds, home city, wish list, owned gear)
    so every provider renders exactly the same task. Providers fetch the image
    via :meth:`fetch_image` and parse replies with :meth:`parse_analysis`.
    """

    def __init__(
        self,
        search: SearchConfig,
        logistics: LogisticsConfig,
        watchlist: Watchlist | None = None,
        inventory: Inventory | None = None,
    ) -> None:
        self._search = search
        self._logistics = logistics
        self._watchlist = watchlist or Watchlist()
        self._inventory = inventory or Inventory()
        self._http = httpx.AsyncClient(timeout=20, follow_redirects=True)

    def set_watchlist(self, watchlist: Watchlist) -> None:
        self._watchlist = watchlist

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_image(self, url: str) -> tuple[bytes, str] | None:
        """Download a listing image, returning ``(bytes, mime)`` or None."""

        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.debug("Image fetch failed (%s): %s", url, exc)
            return None
        mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        if not mime.startswith("image/"):
            return None
        return resp.content, mime

    def build_prompt(self, listing: Listing, context: PreferenceContext) -> str:
        parts: list[str] = []
        categories = self._watchlist.categories or self._search.categories
        parts.append(
            "Buyer is interested in: "
            + ", ".join(categories)
            + f".\nAcceptable price range: {self._search.min_price:.0f}"
            f"-{self._search.max_price:.0f} SEK."
        )
        if self._watchlist.items:
            parts.append(
                "The buyer's detailed watchlist (what they're hunting for). Reward "
                "strong matches to these in fit_score, respect each item's price "
                "ceiling, and weight higher-priority items up:\n"
                + _watchlist_brief(self._watchlist)
            )
        if self._inventory.items:
            parts.append(
                "The buyer ALREADY OWNS the equipment below, including the listed "
                "accessories. Use it two ways: (1) if this listing duplicates a "
                "machine or accessory already owned, lower fit_score unless it's an "
                "exceptional bargain or a useful spare/upgrade, and say so in the "
                "summary; (2) give a fit_score boost to 'want compatible tooling' "
                "or any accessory that fits an owned machine (matching taper, "
                "spindle bore, chuck mount, tool-post or collet size), and name "
                "the machine it fits in the summary:\n"
                + _inventory_brief(self._inventory)
            )
        if context.liked:
            parts.append(
                "Examples of items the buyer LIKED (wants this kind of thing):\n"
                + "\n".join(f"- {ltng.title}" for ltng in context.liked)
            )
        if context.disliked:
            parts.append(
                "Examples the buyer did NOT want (wrong category or no appeal):\n"
                + "\n".join(f"- {ltng.title}" for ltng in context.disliked)
            )
        if context.too_expensive:
            parts.append(
                "The buyer liked these items but found them TOO EXPENSIVE. For "
                "similar items, be stricter on price: only give a high deal_score "
                "when the asking price is clearly below market value.\n"
                + "\n".join(f"- {ltng.title}" for ltng in context.too_expensive)
            )
        if context.poor_condition:
            parts.append(
                "The buyer rejected these for POOR CONDITION. For similar items, "
                "scrutinise wear/damage and lower the score when condition is weak.\n"
                + "\n".join(f"- {ltng.title}" for ltng in context.poor_condition)
            )
        parts.append(
            "Score fit_score higher when the listing resembles LIKED examples and "
            "lower when it resembles disliked ones. If there are no examples yet, "
            "base fit_score on general relevance to the buyer's interests."
        )
        parts.append(
            f"The buyer lives in {self._logistics.home_city}. They can pick items "
            "up by car, so listings closer to their home are more convenient and "
            "should get a modest fit_score boost. From the listing's location text, "
            "estimate distance_km = the approximate ONE-WAY driving distance in "
            f"kilometres from {self._logistics.home_city} to the item. If the "
            "location is unclear, omit distance_km."
        )
        parts.append(
            "Extract delivery info from the listing text: set shipping_available "
            "true if the seller offers postage/shipping. Set shipping_cost to the "
            "stated shipping price in SEK, or 0 if shipping is included in the "
            "price, or omit it if shipping is offered but the price is unstated. "
            "If the item is pickup-only, set shipping_available false and omit "
            "shipping_cost."
        )
        if listing.known_shipping_cost is not None:
            parts.append(
                "Confirmed shipping cost from the marketplace API: "
                f"{listing.known_shipping_cost:.0f} SEK (0 means free/included). "
                "Use this exact value for shipping_cost and set shipping_available "
                "true; do not guess a different figure."
            )
        parts.append("Listing to evaluate:\n" + _listing_brief(listing))
        parts.append(
            "Return JSON with: is_relevant (bool), category, condition, "
            "estimated_value (SEK number), deal_score (0-100, price vs value), "
            "fit_score (0-100, match to buyer taste), summary (one sentence), "
            "reasons (short bullet strings), shipping_available (bool), "
            "shipping_cost (SEK number, optional), distance_km (number, optional)."
        )
        return "\n\n".join(parts)

    @staticmethod
    def parse_analysis(raw: str, model: str) -> DealAnalysis:
        """Validate a model's JSON reply into a stamped :class:`DealAnalysis`."""

        analysis = DealAnalysis.model_validate(json.loads(raw))
        analysis.model = model
        return analysis

    @staticmethod
    def fallback(model: str = "") -> DealAnalysis:
        """A safe, non-relevant verdict used when a provider fails outright."""

        return DealAnalysis(
            is_relevant=False,
            deal_score=0,
            fit_score=0,
            summary=FALLBACK_SUMMARY,
            model=model,
        )
