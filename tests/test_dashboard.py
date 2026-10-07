"""Tests for the hosted dashboard: db queue helpers and send_listing."""

from __future__ import annotations

import pytest

from shopper.config import LogisticsConfig
from shopper.dashboard import Dashboard, _card
from shopper.logistics import Logistics
from shopper.models import DealAnalysis, Verdict


def _analysis(**overrides) -> DealAnalysis:
    defaults = dict(is_relevant=True, deal_score=80, fit_score=80, summary="nice")
    defaults.update(overrides)
    return DealAnalysis(**defaults)


def test_card_includes_verdict_when_given(listing):
    logistics = Logistics(LogisticsConfig())
    # No verdict -> fields are None (pending feed).
    plain = _card(listing, _analysis(), logistics)
    assert plain["verdict"] is None
    assert plain["verdict_label"] is None
    # With a verdict -> value and human label surface (browse view).
    rated = _card(listing, _analysis(), logistics, Verdict.TOO_EXPENSIVE)
    assert rated["verdict"] == "too_expensive"
    assert "expensive" in rated["verdict_label"].lower()


def test_card_includes_model_badge(listing):
    logistics = Logistics(LogisticsConfig())
    card = _card(listing, _analysis(model="qwen-vl-max"), logistics)
    assert card["model"] == "qwen-vl-max"
    # No analysis -> empty model (badge omitted client-side).
    assert _card(listing, None, logistics)["model"] == ""


def test_card_flags_backfill(listing):
    logistics = Logistics(LogisticsConfig())
    # Default: a real, above-bar pending card.
    assert _card(listing, _analysis(), logistics)["backfill"] is False
    # Backfill card surfaced to keep the feed active.
    assert _card(listing, _analysis(), logistics, backfill=True)["backfill"] is True


def test_db_dashboard_queue_roundtrip(db, listing):
    db.save_listing(listing)
    db.save_analysis(listing.uid, _analysis())

    assert db.dashboard_pending() == []

    db.queue_for_dashboard(listing.uid)

    pending = db.dashboard_pending()
    assert [ltng.uid for ltng in pending] == [listing.uid]
    assert db.get_analysis(listing.uid).deal_score == 80

    db.dequeue_from_dashboard(listing.uid)
    assert db.dashboard_pending() == []


@pytest.mark.asyncio
async def test_dashboard_send_listing_queues_and_marks_notified(db, listing):
    dashboard = Dashboard(db, Logistics(LogisticsConfig()), host="127.0.0.1", port=0)
    db.save_listing(listing)
    analysis = _analysis()
    db.save_analysis(listing.uid, analysis)

    await dashboard.send_listing(listing, analysis)

    assert [ltng.uid for ltng in db.dashboard_pending()] == [listing.uid]
