"""Landed-cost estimation: item price + cheapest delivery (ship vs. drive).

The AI extracts shipping availability/cost and an estimated one-way driving
distance from the buyer's home city. This module turns that into concrete money
using the buyer's car cost, so the figures shown are deterministic rather than
guessed by the model.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import LogisticsConfig
from .models import DealAnalysis, Listing


@dataclass
class LandedCost:
    item_price: float | None
    shipping_cost: float | None  # None = pickup-only / unknown
    pickup_cost: float | None  # None = distance unknown
    distance_km: float | None
    is_nearby: bool
    method: str  # "shipping" | "pickup" | "unknown"
    delivery_cost: float | None  # cost of the cheapest available method
    total: float | None  # item_price + delivery_cost when both are known


class Logistics:
    def __init__(self, config: LogisticsConfig) -> None:
        self._config = config

    def pickup_cost(self, distance_km: float) -> float:
        """Round-trip driving cost from home to the listing and back."""

        round_trip_km = 2 * max(distance_km, 0.0)
        hours = round_trip_km / self._config.avg_speed_kmh if self._config.avg_speed_kmh else 0.0
        return round_trip_km * self._config.car_cost_per_km + hours * self._config.car_cost_per_hour

    def estimate(self, listing: Listing, analysis: DealAnalysis) -> LandedCost:
        if listing.known_shipping_cost is not None:
            shipping = listing.known_shipping_cost
        else:
            shipping = analysis.shipping_cost if analysis.shipping_available else None

        pickup: float | None = None
        if analysis.distance_km is not None:
            pickup = round(self.pickup_cost(analysis.distance_km))

        is_nearby = (
            analysis.distance_km is not None
            and analysis.distance_km <= self._config.nearby_km
        )

        # Choose the cheapest available delivery method.
        method = "unknown"
        delivery_cost: float | None = None
        candidates: list[tuple[str, float]] = []
        if shipping is not None:
            candidates.append(("shipping", shipping))
        if pickup is not None:
            candidates.append(("pickup", pickup))
        if candidates:
            method, delivery_cost = min(candidates, key=lambda c: c[1])

        total: float | None = None
        if listing.price is not None and delivery_cost is not None:
            total = listing.price + delivery_cost

        return LandedCost(
            item_price=listing.price,
            shipping_cost=shipping,
            pickup_cost=pickup,
            distance_km=analysis.distance_km,
            is_nearby=is_nearby,
            method=method,
            delivery_cost=delivery_cost,
            total=total,
        )
