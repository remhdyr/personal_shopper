"""Tests for the capacity-aware provider router."""

from __future__ import annotations

import random

import pytest

from shopper.ai.base import PromptBuilder
from shopper.ai.router import NoCapacityError, RoutingAnalyzer, _Provider
from shopper.models import DealAnalysis, Listing, Source
from shopper.preferences import PreferenceContext


class FakeAnalyzer:
    def __init__(self, model: str, fail: bool = False) -> None:
        self.model = model
        self.fail = fail
        self.calls = 0
        self.closed = False
        self.watchlist = None

    @property
    def model_label(self) -> str:
        return self.model

    async def analyze(self, listing, context=None) -> DealAnalysis:
        self.calls += 1
        if self.fail:
            return PromptBuilder.fallback(self.model)
        return DealAnalysis(
            is_relevant=True, deal_score=50, fit_score=50, summary="ok", model=self.model
        )

    async def suggest_watch_item(self, text: str) -> dict:
        return {"name": "x", "queries": []}

    def set_watchlist(self, watchlist) -> None:
        self.watchlist = watchlist

    async def aclose(self) -> None:
        self.closed = True


def _listing() -> Listing:
    return Listing(source=Source.EBAY, source_id="1", title="x", url="https://e/1")


def _provider(name, analyzer, *, weight=1.0, rpm=15, daily_limit=1000) -> _Provider:
    return _Provider(
        analyzer=analyzer, name=name, weight=weight, rpm=rpm, daily_limit=daily_limit
    )


async def test_capacity_weighted_favours_provider_with_more_budget(db):
    random.seed(0)
    a = FakeAnalyzer("model-a")
    b = FakeAnalyzer("model-b")
    # b starts almost exhausted, so a should win most routes.
    for _ in range(90):
        db.increment("ai:b")
    router = RoutingAnalyzer(
        [
            _provider("a", a, daily_limit=1000, rpm=1000),
            _provider("b", b, daily_limit=100, rpm=1000),
        ],
        db,
    )
    for _ in range(40):
        await router.analyze(_listing(), PreferenceContext())
    assert a.calls > b.calls


async def test_failover_when_provider_returns_fallback(db):
    random.seed(1)
    failing = FakeAnalyzer("model-fail", fail=True)
    healthy = FakeAnalyzer("model-ok")
    router = RoutingAnalyzer(
        [
            _provider("fail", failing, weight=1000.0),
            _provider("ok", healthy, weight=0.001),
        ],
        db,
    )
    analysis = await router.analyze(_listing(), PreferenceContext())
    assert analysis.model == "model-ok"
    assert analysis.is_relevant is True
    assert failing.calls == 1
    assert healthy.calls == 1


async def test_rpm_limit_routes_to_other_provider(db):
    a = FakeAnalyzer("model-a")
    b = FakeAnalyzer("model-b")
    router = RoutingAnalyzer(
        [
            _provider("a", a, weight=1_000_000.0, rpm=1),
            _provider("b", b, weight=1.0, rpm=100),
        ],
        db,
    )
    await router.analyze(_listing(), PreferenceContext())  # a (heavy weight)
    await router.analyze(_listing(), PreferenceContext())  # a rpm-limited -> b
    assert a.calls == 1
    assert b.calls == 1


async def test_model_is_stamped_from_serving_provider(db):
    a = FakeAnalyzer("gemini-flash")
    router = RoutingAnalyzer([_provider("gemini", a)], db)
    analysis = await router.analyze(_listing(), PreferenceContext())
    assert analysis.model == "gemini-flash"


async def test_all_exhausted_raises_no_capacity(db):
    a = FakeAnalyzer("model-a")
    b = FakeAnalyzer("model-b")
    db.increment("ai:a")
    db.increment("ai:b")
    router = RoutingAnalyzer(
        [_provider("a", a, daily_limit=1), _provider("b", b, daily_limit=1)], db
    )
    with pytest.raises(NoCapacityError):
        await router.analyze(_listing(), PreferenceContext())


def test_remaining_today_reflects_usage(db):
    a = FakeAnalyzer("model-a")
    for _ in range(10):
        db.increment("ai:a")
    router = RoutingAnalyzer([_provider("a", a, daily_limit=100)], db)
    assert router.remaining_today() == {"a": 90}


async def test_set_watchlist_and_aclose_fan_out(db):
    a = FakeAnalyzer("model-a")
    b = FakeAnalyzer("model-b")
    router = RoutingAnalyzer([_provider("a", a), _provider("b", b)], db)
    router.set_watchlist("WL")
    assert a.watchlist == "WL"
    assert b.watchlist == "WL"
    await router.aclose()
    assert a.closed and b.closed


async def test_suggest_uses_first_provider_with_budget(db):
    a = FakeAnalyzer("model-a")
    router = RoutingAnalyzer([_provider("a", a)], db)
    draft = await router.suggest_watch_item("a lathe")
    assert draft["name"] == "x"
