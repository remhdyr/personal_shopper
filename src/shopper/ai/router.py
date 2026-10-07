"""Capacity-aware load balancer across multiple AI providers.

Free/cheap model tiers each cap requests per minute and (often) per day. To get
more total analyses without paying, the :class:`RoutingAnalyzer` spreads work
across several providers, sending more to whichever has the most daily budget
left, throttling each to its per-minute limit, and failing over to another
provider when one errors. When every provider is out of daily budget it raises
:class:`NoCapacityError` so the pipeline can stop cleanly and retry tomorrow.

Per-provider daily usage is persisted in the database (counter key
``ai:<name>``) so limits survive restarts; the per-minute window is tracked in
memory since it only matters within a single run.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field

from ..db import Database
from ..logging_setup import get_logger
from ..models import DealAnalysis, Listing
from ..preferences import PreferenceContext
from .base import Analyzer, is_fallback

log = get_logger(__name__)

# Cap on a single cooldown sleep when every provider is momentarily rpm-limited
# but still has daily budget; keeps the loop responsive without busy-waiting.
_MAX_COOLDOWN_S = 10.0


class NoCapacityError(Exception):
    """Raised when every provider has exhausted its daily budget."""


@dataclass
class _Provider:
    """A wrapped provider plus its live rate/budget bookkeeping."""

    analyzer: Analyzer
    name: str
    weight: float
    rpm: int
    daily_limit: int
    _calls: deque[float] = field(default_factory=deque)

    def _prune(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= 60.0:
            self._calls.popleft()

    def rpm_ready(self, now: float) -> bool:
        self._prune(now)
        return self.rpm <= 0 or len(self._calls) < self.rpm

    def seconds_until_ready(self, now: float) -> float:
        self._prune(now)
        if self.rpm <= 0 or len(self._calls) < self.rpm:
            return 0.0
        return max(0.0, 60.0 - (now - self._calls[0]))

    def record_call(self, now: float) -> None:
        self._calls.append(now)


class RoutingAnalyzer:
    """Spread analyses across providers by remaining daily capacity."""

    def __init__(
        self,
        providers: list[_Provider],
        db: Database,
        mode: str = "capacity_weighted",
    ) -> None:
        self._providers = providers
        self._db = db
        self._mode = mode
        self._lock = asyncio.Lock()

    @property
    def model_label(self) -> str:
        return "router"

    def _remaining(self, provider: _Provider) -> float:
        if provider.daily_limit <= 0:
            return float("inf")
        used = self._db.counter(f"ai:{provider.name}")
        return max(0.0, provider.daily_limit - used)

    def remaining_today(self) -> dict[str, float]:
        """Per-provider daily budget still available (for diagnostics/dashboard)."""

        return {p.name: self._remaining(p) for p in self._providers}

    def _with_budget(self) -> list[_Provider]:
        return [p for p in self._providers if self._remaining(p) > 0]

    def _pick(self, candidates: list[_Provider]) -> _Provider:
        if self._mode == "round_robin":
            return candidates[0]
        weights: list[float] = []
        for p in candidates:
            remaining = self._remaining(p)
            budget = 1_000_000.0 if remaining == float("inf") else remaining
            weights.append(max(p.weight, 0.0) * budget)
        if sum(weights) <= 0:
            return candidates[0]
        return random.choices(candidates, weights=weights, k=1)[0]

    async def analyze(
        self,
        listing: Listing,
        context: PreferenceContext | None = None,
    ) -> DealAnalysis:
        async with self._lock:
            provider = await self._acquire()
            tried: set[str] = set()
            last: DealAnalysis | None = None
            while provider is not None:
                tried.add(provider.name)
                provider.record_call(time.monotonic())
                self._db.increment(f"ai:{provider.name}")
                analysis = await provider.analyzer.analyze(listing, context)
                if not is_fallback(analysis):
                    return analysis
                log.warning(
                    "Provider %s failed on %s; trying another", provider.name, listing.uid
                )
                last = analysis
                provider = self._next_ready(exclude=tried)
            return last if last is not None else self._fallback()

    async def _acquire(self) -> _Provider | None:
        """Return a provider ready to call now, waiting out brief rpm cooldowns.

        Raises :class:`NoCapacityError` if no provider has daily budget left.
        """

        while True:
            with_budget = self._with_budget()
            if not with_budget:
                raise NoCapacityError("all AI providers out of daily budget")
            now = time.monotonic()
            ready = [p for p in with_budget if p.rpm_ready(now)]
            if ready:
                return self._pick(ready)
            cooldown = min(p.seconds_until_ready(now) for p in with_budget)
            await asyncio.sleep(min(max(cooldown, 0.1), _MAX_COOLDOWN_S))

    def _next_ready(self, exclude: set[str]) -> _Provider | None:
        now = time.monotonic()
        candidates = [
            p
            for p in self._with_budget()
            if p.name not in exclude and p.rpm_ready(now)
        ]
        return self._pick(candidates) if candidates else None

    def _fallback(self) -> DealAnalysis:
        label = self._providers[0].analyzer.model_label if self._providers else ""
        return DealAnalysis(
            is_relevant=False,
            deal_score=0,
            fit_score=0,
            summary="analysis unavailable",
            model=label,
        )

    async def suggest_watch_item(self, text: str) -> dict:
        candidates = self._with_budget() or self._providers
        return await candidates[0].analyzer.suggest_watch_item(text)

    def set_watchlist(self, watchlist) -> None:
        for p in self._providers:
            p.analyzer.set_watchlist(watchlist)

    async def aclose(self) -> None:
        for p in self._providers:
            await p.analyzer.aclose()
