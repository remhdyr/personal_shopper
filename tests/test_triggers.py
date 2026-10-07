"""Tests for the shared 'send more' manual trigger."""

from __future__ import annotations

from datetime import UTC, datetime

from shopper.config import AppConfig
from shopper.models import DealAnalysis, Listing, Source
from shopper.pipeline import Pipeline
from shopper.preferences import PreferenceEngine
from shopper.sources.base import SearchQuery
from shopper.triggers import ManualTrigger


class _FakeSource:
    name = "fake"

    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    async def search(self, query: SearchQuery) -> list[Listing]:
        return self._listings

    async def enrich(self, listing: Listing) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _FakeAnalyzer:
    async def analyze(self, listing: Listing, context=None) -> DealAnalysis:
        return DealAnalysis(is_relevant=True, deal_score=90, fit_score=90, summary="great deal")

    async def aclose(self) -> None:
        pass


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Listing] = []

    async def send_listing(self, listing: Listing, analysis: DealAnalysis) -> None:
        self.sent.append(listing)


def _listings(n: int, *, posted_at: datetime | None = None) -> list[Listing]:
    return [
        Listing(
            source=Source.EBAY,
            source_id=str(i),
            title=f"item {i}",
            price=100,
            url=f"https://example.com/{i}",
            posted_at=posted_at,
        )
        for i in range(n)
    ]


def _pipeline(db, *, n_listings: int, dashboard=None, alerter=None, posted_at=None) -> Pipeline:
    config = AppConfig()
    config.dry_run = False
    config.search.queries = ["tools"]
    source = _FakeSource(_listings(n_listings, posted_at=posted_at))
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    return Pipeline(
        config,
        db,
        [source],
        _FakeAnalyzer(),
        prefs,
        dashboard=dashboard or _FakeNotifier(),
        alerter=alerter,
    )


async def test_trigger_reports_how_many_were_sent(db):
    pipeline = _pipeline(db, n_listings=2)
    trigger = ManualTrigger(pipeline)

    message = await trigger.trigger()

    assert "Found 2 deal(s)" in message


async def test_trigger_reports_nothing_new(db):
    pipeline = _pipeline(db, n_listings=0)
    trigger = ManualTrigger(pipeline)

    message = await trigger.trigger()

    assert "Nothing new" in message


async def test_trigger_ignores_daily_alert_cap(db):
    # Seed so it isn't a cold start, and post the listings "now" so they're
    # fresh; with the daily alert cap at 0, only a manual trigger should still
    # push them (it explicitly bypasses that anti-spam cap).
    db.save_listing(_listings(1)[0])
    alerter = _FakeNotifier()
    pipeline = _pipeline(
        db, n_listings=3, alerter=alerter, posted_at=datetime.now(UTC)
    )
    pipeline._config.alerts.max_per_day = 0

    trigger = ManualTrigger(pipeline)
    message = await trigger.trigger()

    # index 0 was seeded/seen; indexes 1 and 2 are new and alert despite the cap.
    assert len(alerter.sent) == 2
    assert "2 alerted" in message


async def test_trigger_cooldown_blocks_rapid_repeat_calls(db):
    pipeline = _pipeline(db, n_listings=1)
    trigger = ManualTrigger(pipeline, cooldown_seconds=60)

    first = await trigger.trigger()
    second = await trigger.trigger()

    assert "Found 1 deal(s)" in first
    assert "try again in" in second


async def test_trigger_rejects_concurrent_calls(db):
    pipeline = _pipeline(db, n_listings=1)
    trigger = ManualTrigger(pipeline)

    async with trigger._lock:
        message = await trigger.trigger()

    assert "Already checking" in message


async def test_trigger_survives_pipeline_exception(db):
    class _BoomAnalyzer:
        async def analyze(self, listing, context=None):
            raise RuntimeError("boom")

        async def aclose(self) -> None:
            pass

    config = AppConfig()
    config.dry_run = False
    config.search.queries = ["tools"]
    source = _FakeSource(_listings(1))
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    pipeline = Pipeline(
        config, db, [source], _BoomAnalyzer(), prefs, dashboard=_FakeNotifier()
    )
    trigger = ManualTrigger(pipeline)

    message = await trigger.trigger()

    assert "Something went wrong" in message


class _NamedSource(_FakeSource):
    def __init__(self, name: str, listings: list[Listing]) -> None:
        super().__init__(listings)
        self.name = name
        self.searched = False

    async def search(self, query: SearchQuery) -> list[Listing]:
        self.searched = True
        return self._listings


async def test_trigger_scopes_poll_to_selected_sources(db):
    config = AppConfig()
    config.dry_run = False
    config.search.queries = ["tools"]
    a = _NamedSource("ebay", _listings(1))
    b = _NamedSource("tradera", _listings(1))
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    pipeline = Pipeline(
        config, db, [a, b], _FakeAnalyzer(), prefs, dashboard=_FakeNotifier()
    )
    trigger = ManualTrigger(pipeline)

    await trigger.trigger(source_names=["ebay"])

    assert a.searched is True
    assert b.searched is False


def test_pipeline_source_names_reports_enabled_sources(db):
    config = AppConfig()
    config.search.queries = ["tools"]
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    pipeline = Pipeline(
        config,
        db,
        [_NamedSource("ebay", []), _NamedSource("tradera", [])],
        _FakeAnalyzer(),
        prefs,
        dashboard=_FakeNotifier(),
    )

    assert pipeline.source_names == ["ebay", "tradera"]
