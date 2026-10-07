"""Core data models shared across the pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Source(StrEnum):
    EBAY = "ebay"
    TRADERA = "tradera"
    BLOCKET = "blocket"
    AUCTIONET = "auctionet"
    KLARAVIK = "klaravik"
    BLINTO = "blinto"
    PSAUCTION = "psauction"
    FLEASY = "fleasy"
    ATERBYGG = "aterbygg"


class Listing(BaseModel):
    """A normalized marketplace posting from any source."""

    source: Source
    source_id: str
    title: str
    description: str = ""
    price: float | None = None
    currency: str = "SEK"
    url: str
    image_urls: list[str] = Field(default_factory=list)
    location: str = ""
    # Seller / shop identity, when the marketplace exposes one.
    seller: str = ""
    # False identifies a private seller; None means the source could not tell.
    seller_is_business: bool | None = None
    posted_at: datetime | None = None
    # When an auction lot closes, in UTC. Auction sources (Auctionet, Tradera)
    # populate this; fixed-price listings (eBay Buy-It-Now, Blocket) leave it
    # None and are treated as always-open.
    ends_at: datetime | None = None
    # Whether a known auction reserve price has been met. None means the source
    # does not expose reserve status; false means the current bid cannot win.
    reserve_price_reached: bool | None = None
    # Real shipping price in SEK, fetched directly from the marketplace (not an
    # AI guess). 0 means free/included; None means unknown or not looked up.
    known_shipping_cost: float | None = None
    # Name of the watchlist item whose search query surfaced this listing, so
    # the dashboard can filter the feed per item. None for legacy/global queries.
    watch_item: str | None = None

    @property
    def uid(self) -> str:
        """Globally unique id used for dedup."""

        return f"{self.source.value}:{self.source_id}"

    def age_minutes(self, now: datetime | None = None) -> float | None:
        """Minutes since the listing was posted, or ``None`` if unknown.

        Only sources that expose a real posting timestamp populate
        ``posted_at``; for the rest this stays ``None`` and callers fall back to
        their own freshness heuristic.
        """

        if self.posted_at is None:
            return None
        return ((now or _utcnow()) - self.posted_at).total_seconds() / 60.0

    def is_ended(self, now: datetime | None = None) -> bool:
        """Whether this auction has already closed.

        ``True`` only when a known end time is in the past. Listings without an
        ``ends_at`` (fixed-price offers, or sources that don't expose it) are
        treated as open and return ``False``.
        """

        if self.ends_at is None:
            return False
        return self.ends_at <= (now or _utcnow())


class DealAnalysis(BaseModel):
    """Structured output produced by the AI for a listing."""

    is_relevant: bool
    category: str = ""
    condition: str = ""
    estimated_value: float | None = None
    deal_score: int = 0  # 0-100: how good the price is vs. estimated value
    fit_score: int = 0  # 0-100: how well it matches the user's taste
    summary: str = ""
    reasons: list[str] = Field(default_factory=list)
    # Delivery / logistics, extracted from the listing text by the AI.
    shipping_available: bool = False
    # Fixed shipping price in SEK; 0 means "included in the price"; None if the
    # listing is pickup-only or shipping cost is unknown.
    shipping_cost: float | None = None
    # Estimated one-way driving distance from the buyer's home city, in km.
    distance_km: float | None = None
    # Which AI model produced this verdict (e.g. "gemini-flash-lite-latest").
    # Empty for older rows written before multi-provider routing.
    model: str = ""


class Verdict(StrEnum):
    # Positive: the buyer wants this *kind* of item.
    INTERESTED = "interested"
    TOO_EXPENSIVE = "too_expensive"  # right item, price too high
    POOR_CONDITION = "poor_condition"  # right item, too worn/broken
    # Negative: the buyer does not want this kind of item.
    WRONG_TYPE = "wrong_type"  # not this category at all
    NOT_MY_TASTE = "not_my_taste"  # relevant category, but no appeal


#: Verdicts signalling the buyer likes this kind of item (used as positive
#: few-shot examples), even when the specific deal was rejected on price/condition.
POSITIVE_VERDICTS = frozenset(
    {Verdict.INTERESTED, Verdict.TOO_EXPENSIVE, Verdict.POOR_CONDITION}
)
#: Verdicts signalling the buyer does not want this kind of item.
NEGATIVE_VERDICTS = frozenset({Verdict.WRONG_TYPE, Verdict.NOT_MY_TASTE})

#: Button label for each verdict, in display order. Shared by every
#: notification channel (Telegram inline keyboard, local dashboard, ...) so
#: feedback options stay identical no matter where the user replies from.
VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.INTERESTED: "\U0001f44d Interested",
    Verdict.TOO_EXPENSIVE: "\U0001f4b0 Too expensive",
    Verdict.POOR_CONDITION: "\U0001f527 Condition too poor",
    Verdict.WRONG_TYPE: "\U0001f645 Wrong category",
    Verdict.NOT_MY_TASTE: "\U0001f44e Not my taste",
}

#: One-line explanation of what each verdict means, shown in the dashboard
#: legend so the buttons are unambiguous. Keyed in display order.
VERDICT_DESCRIPTIONS: dict[Verdict, str] = {
    Verdict.INTERESTED: "Right item, good deal \u2014 show me more like this.",
    Verdict.TOO_EXPENSIVE: "Right item, price too high \u2014 be stricter on price.",
    Verdict.POOR_CONDITION: "Right item, too worn or broken \u2014 scrutinise condition.",
    Verdict.WRONG_TYPE: "Not this kind of thing at all \u2014 wrong category.",
    Verdict.NOT_MY_TASTE: "Right category, but this one doesn't appeal.",
}

#: Short confirmation shown after the user records a verdict.
VERDICT_ACKS: dict[Verdict, str] = {
    Verdict.INTERESTED: "\U0001f44d Great \u2014 I'll look for more like this.",
    Verdict.TOO_EXPENSIVE: "\U0001f4b0 Noted \u2014 I'll be stricter on price for similar items.",
    Verdict.POOR_CONDITION: "\U0001f527 Noted \u2014 I'll scrutinise condition more.",
    Verdict.WRONG_TYPE: "\U0001f645 Got it \u2014 I'll show fewer from this category.",
    Verdict.NOT_MY_TASTE: "\U0001f44e Got it \u2014 tuning away from this.",
}


class Feedback(BaseModel):
    """A user's reaction to a notified listing."""

    listing_uid: str
    verdict: Verdict
    created_at: datetime = Field(default_factory=_utcnow)
