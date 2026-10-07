"""Tests for Telegram notifier message formatting (no network)."""

from __future__ import annotations

from shopper.logistics import LandedCost
from shopper.models import (
    VERDICT_ACKS,
    VERDICT_DESCRIPTIONS,
    VERDICT_LABELS,
    DealAnalysis,
    Listing,
    Source,
    Verdict,
)
from shopper.notifier import _escape, _escape_url, _format_caption


def test_every_verdict_has_label_description_and_ack():
    # The dashboard legend and feedback buttons are built from these maps, so
    # every verdict must be represented in each to avoid a blank/missing entry.
    for verdict in Verdict:
        assert verdict in VERDICT_LABELS
        assert verdict in VERDICT_DESCRIPTIONS
        assert verdict in VERDICT_ACKS
        assert VERDICT_DESCRIPTIONS[verdict].strip()



def _listing(**overrides) -> Listing:
    base = dict(
        source=Source.EBAY,
        source_id="1",
        title="Mitutoyo caliper",
        price=450,
        currency="SEK",
        url="https://example.com/itm/1",
        description="A nice caliper",
    )
    base.update(overrides)
    return Listing(**base)


def _analysis(**overrides) -> DealAnalysis:
    base = dict(is_relevant=True, deal_score=80, fit_score=75, summary="looks good")
    base.update(overrides)
    return DealAnalysis(**base)


def _cost() -> LandedCost:
    return LandedCost(
        item_price=450,
        shipping_cost=None,
        pickup_cost=None,
        distance_km=None,
        is_nearby=False,
        method="unknown",
        delivery_cost=None,
        total=None,
    )


def test_escape_url_escapes_parens_and_backslash():
    assert _escape_url("https://x/a(b)c") == "https://x/a(b\\)c"
    assert _escape_url("https://x/a\\b") == "https://x/a\\\\b"


def test_caption_escapes_url_with_parens():
    listing = _listing(url="https://example.com/wiki/Lathe_(tool)")
    caption = _format_caption(listing, _analysis(), _cost())
    # The raw ")" must be backslash-escaped inside the MarkdownV2 link.
    assert "(https://example.com/wiki/Lathe_(tool\\))" in caption


def test_caption_escapes_special_chars_in_title():
    listing = _listing(title="Caliper 150mm (new!) - v2.0")
    caption = _format_caption(listing, _analysis(), _cost())
    # Special MarkdownV2 chars in the title get escaped.
    assert "\\(new\\!\\)" in caption
    assert _escape("a.b") == "a\\.b"
