"""Tests for landed-cost (shipping vs. pickup) estimation."""

from __future__ import annotations

from shopper.config import LogisticsConfig
from shopper.logistics import Logistics
from shopper.models import DealAnalysis, Listing, Source


def _listing(price: float | None, known_shipping_cost: float | None = None) -> Listing:
    return Listing(
        source=Source.BLOCKET,
        source_id="1",
        title="Bench vise",
        price=price,
        url="https://example.com/1",
        known_shipping_cost=known_shipping_cost,
    )


def _analysis(**kwargs) -> DealAnalysis:
    base = dict(is_relevant=True, deal_score=70, fit_score=60, summary="x")
    base.update(kwargs)
    return DealAnalysis(**base)


def _logistics() -> Logistics:
    # 100 SEK/h, 2 SEK/km, 80 km/h, nearby <= 50 km.
    return Logistics(LogisticsConfig())


def test_pickup_cost_round_trip():
    log = _logistics()
    # 100 km one-way => 200 km round trip => 400 SEK distance + 2.5 h * 100 = 250
    assert log.pickup_cost(100) == 650


def test_chooses_cheaper_shipping():
    log = _logistics()
    analysis = _analysis(shipping_available=True, shipping_cost=79, distance_km=100)
    cost = log.estimate(_listing(1000), analysis)
    assert cost.shipping_cost == 79
    assert cost.pickup_cost == 650
    assert cost.method == "shipping"
    assert cost.delivery_cost == 79
    assert cost.total == 1079
    assert cost.is_nearby is False


def test_chooses_cheaper_pickup_when_nearby():
    log = _logistics()
    # 20 km one-way => 40 km round trip => 80 SEK + 0.5h*100 = 50 => 130
    analysis = _analysis(shipping_available=True, shipping_cost=300, distance_km=20)
    cost = log.estimate(_listing(500), analysis)
    assert cost.pickup_cost == 130
    assert cost.method == "pickup"
    assert cost.total == 630
    assert cost.is_nearby is True


def test_shipping_included_is_zero():
    log = _logistics()
    analysis = _analysis(shipping_available=True, shipping_cost=0, distance_km=200)
    cost = log.estimate(_listing(500), analysis)
    assert cost.method == "shipping"
    assert cost.delivery_cost == 0
    assert cost.total == 500


def test_pickup_only_when_no_shipping():
    log = _logistics()
    analysis = _analysis(shipping_available=False, distance_km=30)
    cost = log.estimate(_listing(400), analysis)
    assert cost.shipping_cost is None
    assert cost.method == "pickup"
    assert cost.is_nearby is True


def test_unknown_delivery():
    log = _logistics()
    analysis = _analysis(shipping_available=False)
    cost = log.estimate(_listing(400), analysis)
    assert cost.method == "unknown"
    assert cost.delivery_cost is None
    assert cost.total is None
    assert cost.is_nearby is False


def test_known_shipping_cost_overrides_ai_guess():
    log = _logistics()
    # AI guessed a different (or no) shipping cost; the confirmed marketplace
    # value should win.
    analysis = _analysis(shipping_available=True, shipping_cost=300, distance_km=100)
    cost = log.estimate(_listing(500, known_shipping_cost=0), analysis)
    assert cost.shipping_cost == 0
    assert cost.method == "shipping"
    assert cost.total == 500


def test_known_shipping_cost_used_even_if_ai_missed_it():
    log = _logistics()
    analysis = _analysis(shipping_available=False, distance_km=200)
    cost = log.estimate(_listing(500, known_shipping_cost=79), analysis)
    assert cost.shipping_cost == 79
    assert cost.method == "shipping"
    assert cost.total == 579
