"""Tests for the run_once pipeline loop: routing, caps and freshness alerts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from shopper.ai.router import NoCapacityError
from shopper.config import AppConfig
from shopper.models import DealAnalysis, Listing, Source
from shopper.pipeline import Pipeline
from shopper.preferences import PreferenceEngine
from shopper.sources.base import SearchQuery


class _FakeSource:
    name = "fake"

    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    async def search(self, query: SearchQuery) -> list[Listing]:
        return list(self._listings)

    async def enrich(self, listing: Listing) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _FakeAnalyzer:
    def __init__(self, *, deal: int = 90, fit: int = 90, relevant: bool = True) -> None:
        self.calls = 0
        self._deal, self._fit, self._relevant = deal, fit, relevant

    async def analyze(self, listing: Listing, context=None) -> DealAnalysis:
        self.calls += 1
        return DealAnalysis(
            is_relevant=self._relevant,
            deal_score=self._deal,
            fit_score=self._fit,
            summary="x",
        )

    async def aclose(self) -> None:
        pass


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_listing(self, listing: Listing, analysis: DealAnalysis) -> None:
        self.sent.append(listing.uid)


def _config() -> AppConfig:
    config = AppConfig()
    config.dry_run = False
    # Watchlist is empty in tests, so query_specs falls back to search.queries.
    config.search.queries = ["tools"]
    return config


def _listings(n: int, *, posted_at: datetime | None = None, price: float = 100) -> list[Listing]:
    return [
        Listing(
            source=Source.EBAY,
            source_id=str(i),
            title=f"item {i}",
            price=price,
            url=f"https://example.com/{i}",
            posted_at=posted_at,
        )
        for i in range(n)
    ]


async def test_all_qualifying_deals_reach_the_dashboard(db):
    config = _config()
    analyzer = _FakeAnalyzer()
    dash = _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    pipeline = Pipeline(config, db, [_FakeSource(_listings(5))], analyzer, prefs, dashboard=dash)
    stats = await pipeline.run_once()

    assert analyzer.calls == 5
    assert len(dash.sent) == 5
    assert stats.notified == 5
    assert stats.alerted == 0  # no alerter attached


async def test_expired_listings_are_skipped_but_open_and_undated_listings_run(db):
    config = _config()
    analyzer = _FakeAnalyzer()
    listings = _listings(3)
    now = datetime.now(UTC)
    listings[0].source = Source.PSAUCTION
    listings[0].ends_at = now - timedelta(minutes=1)
    listings[1].ends_at = now + timedelta(hours=1)

    pipeline = Pipeline(
        config,
        db,
        [_FakeSource(listings)],
        analyzer,
        PreferenceEngine(db, config.notify, config.alerts),
        dashboard=_FakeSink(),
    )

    stats = await pipeline.run_once()

    assert analyzer.calls == 2
    assert stats.analyzed == 2
    assert not db.is_seen(listings[0].uid)
    assert db.is_seen(listings[1].uid)
    assert db.is_seen(listings[2].uid)


async def test_listing_that_expires_during_analysis_is_not_saved_or_dispatched(db):
    config = _config()
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingAnalyzer(_FakeAnalyzer):
        async def analyze(self, listing: Listing, context=None) -> DealAnalysis:
            started.set()
            await release.wait()
            return await super().analyze(listing, context)

    listing = _listings(1)[0]
    listing.ends_at = datetime.now(UTC) + timedelta(minutes=1)
    dash = _FakeSink()
    pipeline = Pipeline(
        config,
        db,
        [_FakeSource([listing])],
        _BlockingAnalyzer(),
        PreferenceEngine(db, config.notify, config.alerts),
        dashboard=dash,
    )

    run = asyncio.create_task(pipeline.run_once())
    await started.wait()
    listing.ends_at = datetime.now(UTC) - timedelta(seconds=1)
    release.set()
    stats = await run

    assert stats.analyzed == 1
    assert db.counter("ai_analyses") == 1
    assert not db.is_seen(listing.uid)
    assert dash.sent == []


async def test_clear_dashboard_discards_in_flight_results(db):
    config = _config()
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingAnalyzer(_FakeAnalyzer):
        async def analyze(self, listing: Listing, context=None) -> DealAnalysis:
            started.set()
            await release.wait()
            return await super().analyze(listing, context)

    listing = _listings(1)[0]
    dash = _FakeSink()
    pipeline = Pipeline(
        config,
        db,
        [_FakeSource([listing])],
        _BlockingAnalyzer(),
        PreferenceEngine(db, config.notify, config.alerts),
        dashboard=dash,
    )
    run = asyncio.create_task(pipeline.run_once())
    await started.wait()
    assert await pipeline.clear_dashboard() == 0
    release.set()
    await run

    assert dash.sent == []
    assert not db.is_seen(listing.uid)


async def test_excluded_blocket_seller_is_not_analyzed_or_marked_seen(db):
    config = _config()
    analyzer = _FakeAnalyzer()
    listing = Listing(
        source=Source.BLOCKET,
        source_id="auction-repost",
        title="Auction repost",
        price=500,
        seller="blinto",
        seller_is_business=True,
        url="https://www.blocket.se/recommerce/forsale/item/auction-repost",
    )
    pipeline = Pipeline(
        config,
        db,
        [_FakeSource([listing])],
        analyzer,
        PreferenceEngine(db, config.notify, config.alerts),
    )

    stats = await pipeline.run_once()

    assert analyzer.calls == 0
    assert stats.analyzed == 0
    assert not db.is_seen(listing.uid)


async def test_private_only_blocket_policy_defers_unknown_seller_type(db):
    config = _config()
    analyzer = _FakeAnalyzer()
    listing = Listing(
        source=Source.BLOCKET,
        source_id="unknown-seller",
        title="Unknown seller",
        price=500,
        url="https://www.blocket.se/recommerce/forsale/item/unknown-seller",
    )
    pipeline = Pipeline(
        config,
        db,
        [_FakeSource([listing])],
        analyzer,
        PreferenceEngine(db, config.notify, config.alerts),
    )

    await pipeline.run_once()

    assert analyzer.calls == 0
    assert not db.is_seen(listing.uid)


async def test_blocket_business_sellers_can_be_enabled(db):
    config = _config()
    config.sources.blocket.private_sellers_only = False
    analyzer = _FakeAnalyzer()
    listing = Listing(
        source=Source.BLOCKET,
        source_id="business-seller",
        title="Business listing",
        price=500,
        seller="Machine Dealer",
        seller_is_business=True,
        url="https://www.blocket.se/recommerce/forsale/item/business-seller",
    )
    pipeline = Pipeline(
        config,
        db,
        [_FakeSource([listing])],
        analyzer,
        PreferenceEngine(db, config.notify, config.alerts),
    )

    await pipeline.run_once()

    assert analyzer.calls == 1


class _ExhaustedAnalyzer:
    """Raises NoCapacityError after ``limit`` analyses, like a drained router."""

    def __init__(self, limit: int) -> None:
        self.calls = 0
        self._limit = limit

    async def analyze(self, listing: Listing, context=None) -> DealAnalysis:
        if self.calls >= self._limit:
            raise NoCapacityError("out of budget")
        self.calls += 1
        return DealAnalysis(is_relevant=True, deal_score=90, fit_score=90, summary="x")

    async def aclose(self) -> None:
        pass


async def test_no_capacity_breaks_run_leaving_rest_retryable(db):
    config = _config()
    analyzer = _ExhaustedAnalyzer(limit=2)
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    listings = _listings(5)

    pipeline = Pipeline(config, db, [_FakeSource(listings)], analyzer, prefs, dashboard=_FakeSink())
    stats = await pipeline.run_once()

    # Only the two analysed listings were persisted; the rest stay un-seen so a
    # later run (with budget) can still process them.
    assert stats.analyzed == 2
    assert db.is_seen(listings[0].uid)
    assert db.is_seen(listings[1].uid)
    assert not db.is_seen(listings[2].uid)
    assert not db.is_seen(listings[3].uid)
    assert not db.is_seen(listings[4].uid)


class _EnrichingSource(_FakeSource):
    """A source whose enrich() upgrades the listing's images (like Tradera)."""

    # Must match the listings' Source.value so the pipeline's per-source enrich
    # lookup (keyed by listing.source.value) finds us -- _listings uses EBAY.
    name = "ebay"

    async def enrich(self, listing: Listing) -> None:
        listing.image_urls = ["https://img.example.com/full/" + listing.source_id + ".jpg"]


async def test_enriched_images_are_persisted_for_the_dashboard(db):
    # Regression: enrich() must run BEFORE save_listing so the DB copy the
    # dashboard renders from carries the full-size images, not the low-res
    # search thumbnail (save_listing is INSERT OR IGNORE).
    config = _config()
    listings = _listings(1)
    listings[0].image_urls = ["https://img.example.com/thumb.jpg"]  # low-res
    dash = _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    pipeline = Pipeline(
        config, db, [_EnrichingSource(listings)], _FakeAnalyzer(), prefs, dashboard=dash
    )
    await pipeline.run_once()

    # The real dashboard renders from db.dashboard_pending(); emulate the queue.
    db.queue_for_dashboard(listings[0].uid)
    stored = db.dashboard_pending()
    assert len(stored) == 1
    assert stored[0].image_urls == ["https://img.example.com/full/0.jpg"]


async def test_ai_budget_exhausted_does_not_strand_listings(db):
    # If the AI budget is already spent, listings must NOT be marked seen --
    # otherwise dedup would drop them forever without ever analysing them.
    config = _config()
    config.max_ai_analyses_per_day = 3
    for _ in range(3):
        db.increment("ai_analyses")  # budget now fully spent

    analyzer = _FakeAnalyzer()
    listings = _listings(4)
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    pipeline = Pipeline(config, db, [_FakeSource(listings)], analyzer, prefs, dashboard=_FakeSink())
    await pipeline.run_once()

    assert analyzer.calls == 0
    assert all(not db.is_seen(ltng.uid) for ltng in listings)


async def test_per_run_ai_cap_defers_the_rest(db):
    config = _config()
    config.max_ai_analyses_per_run = 2

    analyzer = _FakeAnalyzer()
    dash = _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    pipeline = Pipeline(config, db, [_FakeSource(_listings(5))], analyzer, prefs, dashboard=dash)
    await pipeline.run_once()

    assert analyzer.calls == 2
    assert len(dash.sent) == 2


async def test_failed_dashboard_send_does_not_abort_run(db):
    config = _config()

    class _BoomSink:
        def __init__(self) -> None:
            self.attempts = 0

        async def send_listing(self, listing: Listing, analysis: DealAnalysis) -> None:
            self.attempts += 1
            raise RuntimeError("dashboard down")

    analyzer = _FakeAnalyzer()
    dash = _BoomSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    pipeline = Pipeline(config, db, [_FakeSource(_listings(3))], analyzer, prefs, dashboard=dash)
    stats = await pipeline.run_once()

    # Every listing was attempted (run didn't abort on the first failure)...
    assert dash.attempts == 3
    assert analyzer.calls == 3
    # ...but failed queues never counted as notifications.
    assert stats.notified == 0


async def test_fresh_hot_listing_fires_a_telegram_alert(db):
    config = _config()
    listings = _listings(1, posted_at=datetime.now(UTC))  # just posted
    analyzer = _FakeAnalyzer(deal=90, fit=90)
    dash, tele = _FakeSink(), _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    pipeline = Pipeline(
        config, db, [_FakeSource(listings)], analyzer, prefs, dashboard=dash, alerter=tele
    )
    stats = await pipeline.run_once()

    assert dash.sent == ["ebay:0"]
    assert tele.sent == ["ebay:0"]  # fresh + hot -> alert even on a cold start
    assert stats.alerted == 1
    assert db.counter("alerts") == 1


async def test_stale_listing_reaches_dashboard_but_does_not_alert(db):
    config = _config()
    old = datetime.now(UTC) - timedelta(hours=2)
    analyzer = _FakeAnalyzer(deal=90, fit=90)
    dash, tele = _FakeSink(), _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    pipeline = Pipeline(
        config,
        db,
        [_FakeSource(_listings(1, posted_at=old))],
        analyzer,
        prefs,
        dashboard=dash,
        alerter=tele,
    )
    await pipeline.run_once()

    assert dash.sent == ["ebay:0"]
    assert tele.sent == []  # too old to alert


async def test_cold_start_suppresses_timestampless_alerts(db):
    config = _config()
    analyzer = _FakeAnalyzer(deal=90, fit=90)
    dash, tele = _FakeSink(), _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    # Empty db -> cold start; listing has no posted_at, so freshness is unknown.
    pipeline = Pipeline(
        config, db, [_FakeSource(_listings(1))], analyzer, prefs, dashboard=dash, alerter=tele
    )
    await pipeline.run_once()

    assert dash.sent == ["ebay:0"]
    assert tele.sent == []  # can't verify freshness on the first poll -> no alert


async def test_timestampless_alert_fires_after_baseline(db):
    config = _config()
    db.save_listing(_listings(1)[0])  # seed -> not a cold start
    analyzer = _FakeAnalyzer(deal=90, fit=90)
    dash, tele = _FakeSink(), _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    # index 0 is already seen; index 1 is new-to-us with no posting timestamp.
    pipeline = Pipeline(
        config, db, [_FakeSource(_listings(2))], analyzer, prefs, dashboard=dash, alerter=tele
    )
    await pipeline.run_once()

    assert dash.sent == ["ebay:1"]
    assert tele.sent == ["ebay:1"]  # new-to-us after baseline counts as fresh


async def test_alert_respects_daily_cap(db):
    config = _config()
    config.alerts.max_per_day = 1
    listings = _listings(3, posted_at=datetime.now(UTC))
    analyzer = _FakeAnalyzer(deal=90, fit=90)
    dash, tele = _FakeSink(), _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    pipeline = Pipeline(
        config, db, [_FakeSource(listings)], analyzer, prefs, dashboard=dash, alerter=tele
    )
    stats = await pipeline.run_once()

    assert len(dash.sent) == 3  # every deal still lands on the dashboard
    assert len(tele.sent) == 1  # but alerts are capped
    assert stats.alerted == 1


async def test_without_dashboard_all_matches_go_to_telegram(db):
    config = _config()
    analyzer = _FakeAnalyzer(deal=90, fit=90)
    tele = _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    # Degraded single-channel mode: no dashboard, so Telegram carries everything.
    pipeline = Pipeline(
        config, db, [_FakeSource(_listings(3))], analyzer, prefs, dashboard=None, alerter=tele
    )
    stats = await pipeline.run_once()

    assert len(tele.sent) == 3
    assert stats.notified == 3
    assert stats.alerted == 3


# --- Watchlist CRUD -------------------------------------------------------


class _WatchAnalyzer(_FakeAnalyzer):
    def __init__(self) -> None:
        super().__init__()
        self.watchlist = None

    def set_watchlist(self, watchlist) -> None:
        self.watchlist = watchlist

    async def suggest_watch_item(self, text: str) -> dict:
        return {"name": "Drafted", "queries": [text]}


def _watch_pipeline(db, tmp_path):
    from shopper.config import WatchItem

    config = _config()
    config.watchlist_path = str(tmp_path / "watchlist.yaml")
    config.watchlist.items = [WatchItem(name="Lathe", queries=["svarv"])]
    analyzer = _WatchAnalyzer()
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    pipeline = Pipeline(config, db, [_FakeSource([])], analyzer, prefs, dashboard=_FakeSink())
    return pipeline, config, analyzer


async def test_add_watch_item_persists_and_syncs_analyzer(db, tmp_path):
    from shopper.config import WatchItem, Watchlist

    pipeline, config, analyzer = _watch_pipeline(db, tmp_path)

    await pipeline.add_watch_item(WatchItem(name="Mill", queries=["fräs"]))

    assert [i.name for i in config.watchlist.items] == ["Lathe", "Mill"]
    assert analyzer.watchlist is config.watchlist
    saved = Watchlist.load(config.watchlist_path)
    assert [i.name for i in saved.items] == ["Lathe", "Mill"]


async def test_add_duplicate_name_is_rejected(db, tmp_path):
    import pytest

    from shopper.config import WatchItem

    pipeline, config, _ = _watch_pipeline(db, tmp_path)

    with pytest.raises(ValueError, match="already exists"):
        await pipeline.add_watch_item(WatchItem(name="Lathe", queries=["x"]))
    assert len(config.watchlist.items) == 1


async def test_update_watch_item_replaces_in_place(db, tmp_path):
    from shopper.config import WatchItem

    pipeline, config, _ = _watch_pipeline(db, tmp_path)

    await pipeline.update_watch_item(
        "Lathe", WatchItem(name="Lathe", queries=["svarv", "metallsvarv"], max_price=5000)
    )

    item = config.watchlist.items[0]
    assert item.queries == ["svarv", "metallsvarv"]
    assert item.max_price == 5000


async def test_update_missing_item_raises_keyerror(db, tmp_path):
    import pytest

    from shopper.config import WatchItem

    pipeline, _, _ = _watch_pipeline(db, tmp_path)

    with pytest.raises(KeyError):
        await pipeline.update_watch_item("Nope", WatchItem(name="Nope", queries=["x"]))


async def test_remove_watch_item(db, tmp_path):
    from shopper.config import Watchlist

    pipeline, config, _ = _watch_pipeline(db, tmp_path)

    await pipeline.remove_watch_item("Lathe")

    assert config.watchlist.items == []
    assert Watchlist.load(config.watchlist_path).items == []


async def test_remove_watch_item_does_not_wait_for_poll_lock(db, tmp_path):
    pipeline, config, _ = _watch_pipeline(db, tmp_path)

    async with pipeline._run_lock:
        await asyncio.wait_for(pipeline.remove_watch_item("Lathe"), timeout=1)

    assert config.watchlist.items == []


async def test_remove_missing_item_raises_keyerror(db, tmp_path):
    import pytest

    pipeline, _, _ = _watch_pipeline(db, tmp_path)

    with pytest.raises(KeyError):
        await pipeline.remove_watch_item("Nope")


async def test_watchlist_edit_changes_next_poll_queries(db, tmp_path):
    from shopper.config import WatchItem

    pipeline, config, _ = _watch_pipeline(db, tmp_path)

    await pipeline.add_watch_item(WatchItem(name="Mill", queries=["fräs"]))

    # query_specs derives live from the watchlist, so the new query is included.
    assert {s.text for s in config.query_specs()} == {"svarv", "fräs"}


async def test_github_sync_reloads_watchlist_and_analyzer(db, tmp_path):
    from shopper.config import WatchItem, Watchlist

    pipeline, config, analyzer = _watch_pipeline(db, tmp_path)
    Watchlist(items=[WatchItem(name="Mill", queries=["fräs"])]).save(config.watchlist_path)

    class _Sync:
        async def sync(self) -> bool:
            return True

    assert await pipeline.sync_watchlist_from_github(_Sync())  # type: ignore[arg-type]
    assert [item.name for item in config.watchlist.items] == ["Mill"]
    assert analyzer.watchlist is config.watchlist


# --- Per-item scoped search & Telegram mute -------------------------------


class _QueryAwareSource:
    """Returns one listing per query, encoding the query text in the id."""

    name = "fake"

    def __init__(self) -> None:
        self.queried: list[str] = []

    async def search(self, query: SearchQuery) -> list[Listing]:
        self.queried.append(query.text)
        return [
            Listing(
                source=Source.EBAY,
                source_id=query.text,
                title=f"hit for {query.text}",
                price=100,
                url=f"https://example.com/{query.text}",
            )
        ]

    async def enrich(self, listing: Listing) -> None:
        pass

    async def aclose(self) -> None:
        pass


async def test_scoped_watch_item_poll_only_runs_that_items_queries(db, tmp_path):
    from shopper.config import WatchItem

    pipeline, config, _ = _watch_pipeline(db, tmp_path)
    config.watchlist.items = [
        WatchItem(name="Lathe", queries=["svarv"]),
        WatchItem(name="Mill", queries=["fräs"]),
    ]
    source = _QueryAwareSource()
    dash = _FakeSink()
    pipeline._sources = [source]
    pipeline._dashboard = dash

    await pipeline.run_once(watch_item="Lathe")

    # Only the Lathe query ran, and its listing carries the origin tag.
    assert source.queried == ["svarv"]
    assert dash.sent == ["ebay:svarv"]


async def test_collected_listings_are_tagged_with_origin_item(db, tmp_path):
    from shopper.config import WatchItem

    pipeline, config, _ = _watch_pipeline(db, tmp_path)
    config.watchlist.items = [WatchItem(name="Lathe", queries=["svarv"])]
    pipeline._sources = [_QueryAwareSource()]

    collected = await pipeline._collect()

    assert [ltng.watch_item for ltng in collected] == ["Lathe"]


async def test_collect_round_robins_across_sources(db):
    """A flooding source must not starve others of the AI budget.

    eBay can return 1000+ hits while Tradera/Auctionet return a handful; if the
    merge kept all of one source first, the per-run/daily AI cap would be spent
    before the other sources' listings were ever reached. The merge interleaves
    sources so the front of the list (what the budget actually analyses) spans
    every source.
    """

    def _named(source_name: str, ids: list[str]) -> _FakeSource:
        src = _FakeSource(
            [
                Listing(
                    source=Source.EBAY,
                    source_id=i,
                    title=i,
                    price=100,
                    url=f"https://example.com/{i}",
                )
                for i in ids
            ]
        )
        src.name = source_name
        return src

    config = _config()
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    flooder = _named("ebay", ["e0", "e1", "e2", "e3"])
    small = _named("tradera", ["t0"])
    pipeline = Pipeline(config, db, [flooder, small], _FakeAnalyzer(), prefs,
                        dashboard=_FakeSink())

    collected = await pipeline._collect()
    order = [ltng.source_id for ltng in collected]

    # Interleaved: e0, t0, then the flooder's remainder — so 'tradera' appears
    # near the front instead of after all four eBay hits.
    assert order == ["e0", "t0", "e1", "e2", "e3"]


async def test_iter_collected_streams_before_all_searches_finish(db):
    """The first result must be emitted without waiting for the whole sweep.

    Time-to-first-deal is the reason the poll streams: a fast search's listing
    should surface long before a slow sibling search returns, instead of the old
    behaviour where nothing appeared until the entire collection finished.
    """
    import asyncio

    config = _config()
    config.search.queries = ["fast", "slow"]
    config.search.concurrency = 2

    class _MixedSource:
        name = "fake"

        async def search(self, query: SearchQuery) -> list[Listing]:
            if query.text == "slow":
                await asyncio.sleep(0.5)
            return [
                Listing(
                    source=Source.EBAY,
                    source_id=query.text,
                    title=query.text,
                    price=100,
                    url=f"https://example.com/{query.text}",
                )
            ]

        async def enrich(self, listing: Listing) -> None:
            pass

        async def aclose(self) -> None:
            pass

    prefs = PreferenceEngine(db, config.notify, config.alerts)
    pipeline = Pipeline(config, db, [_MixedSource()], _FakeAnalyzer(), prefs, dashboard=_FakeSink())

    loop = asyncio.get_event_loop()
    start = loop.time()
    first = None
    async for listing in pipeline._iter_collected():
        first = listing
        elapsed = loop.time() - start
        break

    assert first is not None
    assert first.source_id == "fast"
    assert elapsed < 0.4  # surfaced well before the 0.5s slow search finished


async def test_fast_source_does_not_starve_a_slow_one(db):
    """A fast source may lead, but not run away with the whole budget.

    Regression: streaming by arrival meant Tradera (fast) filled its buffer
    first and got emitted exclusively until eBay (slow) reported, exhausting the
    per-run AI budget before any eBay listing was reached. The bounded lead caps
    how far ahead the fast source can get, so the slow source's listings still
    make the front of the merged stream.
    """
    import asyncio

    from shopper.pipeline import _SOURCE_LEAD

    config = _config()
    config.search.queries = ["q"]

    def _named(source_name: str, n: int, *, delay: float) -> object:
        class _S:
            name = source_name

            async def search(self, query: SearchQuery) -> list[Listing]:
                await asyncio.sleep(delay)
                return [
                    Listing(
                        source=Source.EBAY,
                        source_id=f"{source_name}{i}",
                        title=f"{source_name}{i}",
                        price=100,
                        url=f"https://example.com/{source_name}{i}",
                    )
                    for i in range(n)
                ]

            async def enrich(self, listing: Listing) -> None:
                pass

            async def aclose(self) -> None:
                pass

        return _S()

    fast = _named("tradera", 20, delay=0.0)
    slow = _named("ebay", 20, delay=0.2)
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    pipeline = Pipeline(config, db, [slow, fast], _FakeAnalyzer(), prefs, dashboard=_FakeSink())

    order = [ltng.source_id async for ltng in pipeline._iter_collected()]

    # The fast source never gets more than the lead ahead of the slow source at
    # the point the slow source first appears.
    first_slow = next(i for i, uid in enumerate(order) if uid.startswith("ebay"))
    assert first_slow <= _SOURCE_LEAD
    # And both sources are fully represented once everything drains.
    assert sum(uid.startswith("ebay") for uid in order) == 20
    assert sum(uid.startswith("tradera") for uid in order) == 20


async def test_searches_run_concurrently_per_source(db):
    import asyncio

    config = _config()
    config.search.queries = ["a", "b", "c", "d"]
    config.search.concurrency = 4

    class _SlowSource:
        name = "fake"

        def __init__(self) -> None:
            self.in_flight = 0
            self.max_in_flight = 0

        async def search(self, query: SearchQuery) -> list[Listing]:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.02)  # hold the slot so overlap is observable
            self.in_flight -= 1
            return []

        async def enrich(self, listing: Listing) -> None:
            pass

        async def aclose(self) -> None:
            pass

    source = _SlowSource()
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    pipeline = Pipeline(config, db, [source], _FakeAnalyzer(), prefs, dashboard=_FakeSink())

    await pipeline._collect()

    # All four searches overlapped rather than running one at a time.
    assert source.max_in_flight == 4


async def test_search_concurrency_is_capped_by_config(db):
    import asyncio

    config = _config()
    config.search.queries = ["a", "b", "c", "d", "e"]
    config.search.concurrency = 2

    class _SlowSource:
        name = "fake"

        def __init__(self) -> None:
            self.in_flight = 0
            self.max_in_flight = 0

        async def search(self, query: SearchQuery) -> list[Listing]:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.02)
            self.in_flight -= 1
            return []

        async def enrich(self, listing: Listing) -> None:
            pass

        async def aclose(self) -> None:
            pass

    source = _SlowSource()
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    pipeline = Pipeline(config, db, [source], _FakeAnalyzer(), prefs, dashboard=_FakeSink())

    await pipeline._collect()

    assert source.max_in_flight == 2  # never exceeds the configured limit


async def test_one_failing_search_does_not_abort_the_source(db):
    config = _config()
    config.search.queries = ["good1", "boom", "good2"]

    class _FlakySource:
        name = "fake"

        async def search(self, query: SearchQuery) -> list[Listing]:
            if query.text == "boom":
                raise RuntimeError("marketplace hiccup")
            return [
                Listing(
                    source=Source.EBAY,
                    source_id=query.text,
                    title=query.text,
                    price=100,
                    url=f"https://example.com/{query.text}",
                )
            ]

        async def enrich(self, listing: Listing) -> None:
            pass

        async def aclose(self) -> None:
            pass

    prefs = PreferenceEngine(db, config.notify, config.alerts)
    pipeline = Pipeline(config, db, [_FlakySource()], _FakeAnalyzer(), prefs, dashboard=_FakeSink())

    collected = await pipeline._collect()

    # The two good searches still surfaced their listings.
    assert {ltng.source_id for ltng in collected} == {"good1", "good2"}


async def test_muted_alerts_still_reach_dashboard(db):
    config = _config()
    analyzer = _FakeAnalyzer(deal=95, fit=95)
    dash = _FakeSink()
    tele = _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    fresh = _listings(3, posted_at=datetime.now(UTC))

    pipeline = Pipeline(
        config, db, [_FakeSource(fresh)], analyzer, prefs, dashboard=dash, alerter=tele
    )
    pipeline.set_alerts_muted(True)
    stats = await pipeline.run_once()

    assert len(dash.sent) == 3  # dashboard is unaffected by muting
    assert tele.sent == []  # no Telegram pushes while muted
    assert stats.alerted == 0


async def test_unmuting_restores_telegram_alerts(db):
    config = _config()
    analyzer = _FakeAnalyzer(deal=95, fit=95)
    tele = _FakeSink()
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    fresh = _listings(2, posted_at=datetime.now(UTC))

    pipeline = Pipeline(
        config, db, [_FakeSource(fresh)], analyzer, prefs, dashboard=_FakeSink(), alerter=tele
    )
    pipeline.set_alerts_muted(True)
    pipeline.set_alerts_muted(False)
    assert pipeline.alerts_muted is False
    await pipeline.run_once()

    assert len(tele.sent) == 2


def test_telegram_available_reflects_alerter(db):
    config = _config()
    prefs = PreferenceEngine(db, config.notify, config.alerts)

    without = Pipeline(config, db, [_FakeSource([])], _FakeAnalyzer(), prefs, dashboard=_FakeSink())
    with_tele = Pipeline(
        config, db, [_FakeSource([])], _FakeAnalyzer(), prefs, dashboard=_FakeSink(),
        alerter=_FakeSink(),
    )

    assert without.telegram_available is False
    assert with_tele.telegram_available is True



async def test_suggest_watch_item_delegates_to_analyzer(db, tmp_path):
    pipeline, _, _ = _watch_pipeline(db, tmp_path)

    draft = await pipeline.suggest_watch_item("a small lathe")

    assert draft == {"name": "Drafted", "queries": ["a small lathe"]}
