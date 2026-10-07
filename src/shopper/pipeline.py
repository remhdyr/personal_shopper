"""The end-to-end deal-hunting pipeline.

One pass: query every enabled source, drop already-seen and out-of-range
listings, analyze the rest with Gemini, and route the ones that clear the
adaptive bar. Everything that qualifies lands on the hosted dashboard (the
primary, browse-any-time UI); the exceptional, freshly-posted hits *also* get a
Telegram push so you can pounce before someone else does. Daily/per-run caps
protect the Gemini free tier and keep alerts from becoming spam.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from .ai.base import Analyzer
from .ai.router import NoCapacityError
from .config import AppConfig, QuerySpec, WatchItem, Watchlist
from .db import Database
from .github_sync import WatchlistGitSync
from .logging_setup import get_logger
from .logistics import Logistics
from .models import DealAnalysis, Listing
from .preferences import PreferenceEngine
from .sources.base import SearchQuery, Source

log = get_logger(__name__)

_AI_COUNTER = "ai_analyses"
_ALERT_COUNTER = "alerts"

# Optional callback fired with short human-readable status strings as a run
# progresses (source X done, analyzing listing Y, ...), so a caller polling
# from the outside (e.g. the dashboard) can show agile feedback instead of a
# single static "searching..." message for the whole 10-60s run.
ProgressCallback = Callable[[str], None]


def _report(on_progress: ProgressCallback | None, message: str) -> None:
    if on_progress is not None:
        try:
            on_progress(message)
        except Exception:  # noqa: BLE001 - a broken progress sink shouldn't fail the run
            log.debug("Progress callback failed", exc_info=True)

# How far ahead (in emitted listings) one source may get before the merge waits
# for a slower source that hasn't reported yet. Small enough to keep the limited
# AI budget spread fairly across sources, large enough that the first few deals
# still stream out immediately instead of blocking on the slowest source.
_SOURCE_LEAD = 3


class _Sink(Protocol):
    """Anything that can accept a qualifying deal (dashboard or Telegram)."""

    async def send_listing(self, listing: Listing, analysis: DealAnalysis) -> None: ...


@dataclass
class RunStats:
    """Summary of one pipeline pass, used to report back on-demand triggers."""

    collected: int = 0
    new: int = 0
    analyzed: int = 0
    # Deals routed to the dashboard feed this pass.
    notified: int = 0
    # Subset of the above that also fired a Telegram push alert.
    alerted: int = 0


class Pipeline:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        sources: list[Source],
        analyzer: Analyzer,
        preferences: PreferenceEngine,
        dashboard: _Sink | None = None,
        alerter: _Sink | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._sources = sources
        self._analyzer = analyzer
        self._prefs = preferences
        # The dashboard is the primary feed: every qualifying deal lands here.
        self._dashboard = dashboard
        # The alerter (Telegram) is the selective push for fresh, top-tier hits.
        self._alerter = alerter
        self._logistics = Logistics(config.logistics)
        self._sources_by_name = {source.name: source for source in sources}
        # Serialize runs so a manual "send more" can't overlap a scheduled poll
        # (which would double-analyze listings and inflate the daily counters).
        self._run_lock = asyncio.Lock()
        # Serialize watchlist edits and Git synchronization with each other.
        # This is intentionally separate from _run_lock so dashboard mutations
        # do not wait for a potentially minutes-long poll.
        self._watchlist_lock = asyncio.Lock()
        # False only until the first poll finishes. It gates freshness alerts
        # for sources without a posting timestamp, so a cold-start backlog
        # doesn't fire a flood of "new" alerts on the very first run.
        self._baseline_ready = db.has_any_listings()
        # When True, suppress Telegram push alerts (the dashboard still gets
        # every deal). Toggled from the dashboard while the user is watching the
        # screen; in-memory only, so it resets to unmuted on restart.
        self._alerts_muted = False
        # Incremented when the dashboard is cleared, allowing an in-flight
        # search to finish without publishing stale results.
        self._dashboard_generation = 0

    def _price_ok(self, listing: Listing) -> bool:
        if listing.price is None:
            return True  # unknown price: let the AI judge it
        return self._config.search.min_price <= listing.price <= self._config.search.max_price

    def _is_excluded_blocket_seller(self, listing: Listing) -> bool:
        seller = listing.seller.strip().casefold()
        if not seller:
            return False
        excluded = {
            name.strip().casefold()
            for name in self._config.sources.blocket.excluded_sellers
        }
        return seller in excluded

    @property
    def source_names(self) -> list[str]:
        """Names of the enabled sources, in configured order."""

        return list(self._sources_by_name)

    @property
    def telegram_available(self) -> bool:
        """Whether a Telegram alerter is configured (for the mute toggle UI)."""

        return self._alerter is not None

    @property
    def alerts_muted(self) -> bool:
        return self._alerts_muted

    def set_alerts_muted(self, muted: bool) -> None:
        """Mute/unmute Telegram push alerts (dashboard toggle)."""

        self._alerts_muted = muted
        log.info("Telegram alerts %s", "muted" if muted else "unmuted")

    async def clear_dashboard(self) -> int:
        """Clear dashboard state and invalidate results from an in-flight run."""

        self._dashboard_generation += 1
        removed = self._db.clear_dashboard()
        log.info("Cleared %d listings from the dashboard", removed)
        return removed

    # --- Watchlist management (driven by the dashboard) ---------------------
    #
    # query_specs() derives searches live from config.watchlist, so mutating it
    # here is enough for the next poll to pick up the change; we also persist to
    # watchlist.yaml and refresh the analyzer's guidance. The mutation lock only
    # serializes edits and Git sync with each other. A poll derives its search
    # specs synchronously before its first await, then keeps that local list for
    # the in-flight poll; edits therefore affect the next poll.

    def get_watchlist(self) -> Watchlist:
        """The current wish list (read-only snapshot for the dashboard)."""

        return self._config.watchlist

    async def suggest_watch_item(self, text: str) -> dict:
        """Ask the AI to draft a watch item from free text (not saved)."""

        return await self._analyzer.suggest_watch_item(text)

    async def add_watch_item(self, item: WatchItem) -> None:
        async with self._watchlist_lock:
            items = self._config.watchlist.items
            if any(existing.name == item.name for existing in items):
                raise ValueError(f"A watch item named {item.name!r} already exists")
            items.append(item)
            self._persist_watchlist()

    async def update_watch_item(self, name: str, item: WatchItem) -> None:
        async with self._watchlist_lock:
            items = self._config.watchlist.items
            index = next((i for i, it in enumerate(items) if it.name == name), None)
            if index is None:
                raise KeyError(name)
            if item.name != name and any(it.name == item.name for it in items):
                raise ValueError(f"A watch item named {item.name!r} already exists")
            items[index] = item
            self._persist_watchlist()

    async def remove_watch_item(self, name: str) -> None:
        async with self._watchlist_lock:
            items = self._config.watchlist.items
            remaining = [it for it in items if it.name != name]
            if len(remaining) == len(items):
                raise KeyError(name)
            self._config.watchlist.items = remaining
            self._persist_watchlist()

    def _persist_watchlist(self) -> None:
        """Save the live wish list to disk and refresh the analyzer's guidance."""

        if self._config.watchlist_path:
            self._config.watchlist.save(self._config.watchlist_path)
        self._analyzer.set_watchlist(self._config.watchlist)

    async def sync_watchlist_from_github(self, sync: WatchlistGitSync) -> bool:
        """Apply a Git-synchronized watchlist and refresh live AI guidance."""

        async with self._watchlist_lock:
            if not await sync.sync():
                return False
            if not self._config.watchlist_path:
                return False
            self._config.watchlist = Watchlist.load(self._config.watchlist_path)
            self._analyzer.set_watchlist(self._config.watchlist)
            return True

    async def _collect(
        self, source_names: list[str] | None = None, watch_item: str | None = None
    ) -> list[Listing]:
        """Batch form of :meth:`_iter_collected`: drain the whole sweep to a list.

        Kept for callers/tests that want the full, deterministic round-robin
        ordering up front. The live poll consumes the streaming form directly so
        it can start analysing before the entire sweep finishes.
        """

        return [
            listing async for listing in self._iter_collected(source_names, watch_item)
        ]

    async def _iter_collected(
        self,
        source_names: list[str] | None = None,
        watch_item: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AsyncIterator[Listing]:
        """Stream deduped listings in fair round-robin order as searches finish.

        ``source_names`` optionally restricts the poll to a subset of enabled
        sources (used by a scoped manual "send more"); ``None`` queries all.
        ``watch_item`` optionally restricts the poll to a single watchlist
        item's queries (a "search this item now" request); ``None`` runs all.
        Every surfaced listing is tagged with the item whose query found it.

        Yielding as each search returns -- rather than gathering the whole sweep
        first -- is what keeps time-to-first-deal low: with ~74 searches per
        source a full sweep can take 10-60s, and the old code showed nothing on
        the dashboard until all of it finished. Fairness is preserved with a
        bounded lead (see ``_SOURCE_LEAD``): a fast source (e.g. Tradera) may get
        a few listings ahead so the first deals stream out immediately, but not
        so far ahead that it monopolises the limited AI budget before a slower
        source (e.g. eBay, which can return 1000+ hits) has reported.
        """

        wanted = set(source_names) if source_names else None
        specs = [
            spec
            for spec in self._config.query_specs()
            if watch_item is None or spec.watch_item == watch_item
        ]
        sources = [
            source
            for source in self._sources
            if wanted is None or source.name in wanted
        ]
        if not sources or not specs:
            return

        _report(
            on_progress,
            f"Searching {len(sources)} source(s) — {', '.join(s.name for s in sources)}…",
        )

        # Each (source, spec) pair is an independent HTTP search, so run them
        # concurrently. A per-source semaphore caps how many are in flight to
        # stay clear of marketplace rate limits. Each finished search pushes its
        # results onto a queue that the consumer below drains as they arrive.
        limit = max(1, self._config.search.concurrency)
        queue: asyncio.Queue[tuple[str, QuerySpec | None, list[Listing]]] = asyncio.Queue()

        async def search_source(source: Source) -> None:
            sem = asyncio.Semaphore(limit)

            async def run(spec: QuerySpec) -> None:
                query = SearchQuery(
                    text=spec.text,
                    min_price=spec.min_price,
                    max_price=spec.max_price,
                    location=self._config.search.location,
                )
                async with sem:
                    try:
                        listings = await source.search(query)
                    except Exception as exc:  # noqa: BLE001 - one bad search shouldn't stop the rest
                        log.error("Source %s failed on %r: %s", source.name, spec.text, exc)
                        listings = []
                await queue.put((source.name, spec, listings))

            await asyncio.gather(*(run(spec) for spec in specs))
            await queue.put((source.name, None, []))  # this source is done

        producers = [asyncio.create_task(search_source(source)) for source in sources]
        order = [source.name for source in sources]
        buffers: dict[str, deque[Listing]] = {name: deque() for name in order}
        done: dict[str, bool] = {name: False for name in order}
        emitted: dict[str, int] = {name: 0 for name in order}
        seen: set[str] = set()

        def absorb(name: str, spec: QuerySpec, listings: list[Listing]) -> None:
            for listing in listings:
                if listing.uid in seen:
                    continue  # first spec to surface a uid wins its watch_item tag
                seen.add(listing.uid)
                listing.watch_item = spec.watch_item
                buffers[name].append(listing)

        def mark_done(name: str) -> None:
            done[name] = True
            completed = sum(1 for d in done.values() if d)
            _report(
                on_progress,
                f"{name} done ({completed}/{len(order)} sources) — "
                f"{len(seen)} listing(s) found so far",
            )

        def drain_ready() -> None:
            # Absorb everything already available without blocking so the merge
            # sees each source's latest results before choosing what to emit.
            while not queue.empty():
                name, spec, listings = queue.get_nowait()
                if spec is None:
                    mark_done(name)
                else:
                    absorb(name, spec, listings)

        try:
            while True:
                drain_ready()
                candidates = [name for name in order if buffers[name]]
                if candidates:
                    # Prefer the source that has emitted the least so far, so the
                    # feed stays balanced across sources.
                    cand = min(candidates, key=lambda n: (emitted[n], order.index(n)))
                    # ...but hold back if a still-running source with an empty
                    # buffer has fallen too far behind: emitting now would let the
                    # fast source (e.g. Tradera) run away with the AI budget
                    # before the slow one (e.g. eBay) has reported. A source that
                    # is done and drained is exhausted, not starved, so it never
                    # blocks the others.
                    starved = any(
                        not buffers[name]
                        and not done[name]
                        and emitted[name] <= emitted[cand] - _SOURCE_LEAD
                        for name in order
                    )
                    if not starved:
                        emitted[cand] += 1
                        yield buffers[cand].popleft()
                        continue
                elif all(done[name] for name in order):
                    break
                # Nothing to emit yet (empty buffers) or the leader is too far
                # ahead of a lagging source: wait for the next search to land.
                name, spec, listings = await queue.get()
                if spec is None:
                    mark_done(name)
                else:
                    absorb(name, spec, listings)
        finally:
            for producer in producers:
                producer.cancel()
            await asyncio.gather(*producers, return_exceptions=True)


    async def run_once(
        self,
        *,
        ignore_daily_alert_cap: bool = False,
        source_names: list[str] | None = None,
        watch_item: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> RunStats:
        """Run one pipeline pass.

        ``ignore_daily_alert_cap`` lets an explicit manual "send more" request
        skip the daily Telegram *alert* cap (an anti-spam throttle) -- the
        per-run alert cap and the AI analysis budget still apply, since those
        protect real Gemini quota.

        ``source_names`` optionally restricts the poll to a subset of enabled
        sources (a scoped manual "send more"); ``None`` queries all.
        ``watch_item`` optionally restricts the poll to a single watchlist
        item's queries; ``None`` runs every configured search.

        Runs are serialized: if a poll is already in progress, this awaits it
        so scheduled and manual runs can't race on the shared counters/db.
        """
        async with self._run_lock:
            return await self._run_once_locked(
                ignore_daily_alert_cap=ignore_daily_alert_cap,
                source_names=source_names,
                watch_item=watch_item,
                on_progress=on_progress,
            )

    async def _run_once_locked(
        self,
        *,
        ignore_daily_alert_cap: bool,
        source_names: list[str] | None = None,
        watch_item: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> RunStats:
        liked_context = self._prefs.few_shot()
        stats = RunStats()
        dashboard_generation = self._dashboard_generation
        per_run_cap = self._config.max_ai_analyses_per_run
        # Feed depth at the start of the pass: when the dashboard is running dry
        # the preference engine relaxes its bars so a quiet market can't leave it
        # empty. Add this pass's own matches so the relief tapers as it fills.
        pending_at_start = self._db.dashboard_pending_count()

        # Stream: analyse and dispatch each listing as searches return, rather
        # than waiting for the whole (10-60s) sweep to finish. The first
        # qualifying deal reaches the dashboard within seconds of the first
        # search, and hitting a cap below cancels the remaining searches.
        async for listing in self._iter_collected(source_names, watch_item, on_progress):
            stats.collected += 1

            # Drop already-seen, expired and out-of-range listings before they
            # can consume any of the AI budget.
            if (
                listing.is_ended()
                or self._db.is_seen(listing.uid)
                or not self._price_ok(listing)
            ):
                continue
            stats.new += 1

            if per_run_cap and stats.analyzed >= per_run_cap:
                log.info(
                    "Per-run AI analysis cap (%d) reached; remaining listings"
                    " deferred to the next poll",
                    per_run_cap,
                )
                break

            # Check the AI budget *before* marking the listing seen. Otherwise a
            # listing saved here would be deduped forever yet never analysed.
            if self._db.counter(_AI_COUNTER) >= self._config.max_ai_analyses_per_day:
                log.warning("Daily AI analysis budget reached; deferring rest")
                break

            # Enrich (shipping cost, full-size images) BEFORE persisting so the
            # stored copy -- which the dashboard renders from -- has the upgraded
            # data. save_listing is INSERT OR IGNORE, so saving after is the only
            # way the enriched image URLs reach the DB.
            source = self._sources_by_name.get(listing.source.value)
            if source is not None:
                try:
                    await source.enrich(listing)
                except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
                    log.debug("Enrich failed for %s: %s", listing.uid, exc)

            # A current bid below a known reserve cannot win the lot, so defer
            # it without spending an AI analysis. It remains un-seen and is
            # reconsidered on a later poll after bidding advances.
            if listing.reserve_price_reached is False:
                log.info("Deferring %s; reserve price not reached", listing.uid)
                continue

            if listing.source.value == "blocket":
                if (
                    self._config.sources.blocket.private_sellers_only
                    and listing.seller_is_business is not False
                ):
                    log.info("Deferring %s; Blocket seller is not verified private", listing.uid)
                    continue
                if self._is_excluded_blocket_seller(listing):
                    log.info(
                        "Deferring %s; Blocket seller %r is excluded",
                        listing.uid,
                        listing.seller,
                    )
                    continue

            # Analyse BEFORE persisting the listing. If every AI provider is out
            # of daily budget the router raises NoCapacityError; breaking here
            # leaves this listing un-seen so it's retried on the next run rather
            # than deduped away un-analysed.
            _report(
                on_progress,
                f"Analyzing '{listing.title[:60]}' ({stats.analyzed + 1} so far, "
                f"{stats.notified} match(es) found)…",
            )
            try:
                analysis = await self._analyzer.analyze(listing, liked_context)
            except NoCapacityError:
                log.warning("All AI providers exhausted for today; deferring rest")
                break

            if listing.is_ended():
                # The auction may close while the analyzer is working. Count the
                # call against the budget, but don't store or dispatch a stale lot.
                self._db.increment(_AI_COUNTER)
                stats.analyzed += 1
                log.info("Discarding expired listing %s after analysis", listing.uid)
                continue

            if dashboard_generation != self._dashboard_generation:
                log.info("Discarding stale result for %s after dashboard clear", listing.uid)
                continue

            self._db.save_listing(listing)
            self._db.increment(_AI_COUNTER)
            self._db.save_analysis(listing.uid, analysis)
            stats.analyzed += 1

            if not self._prefs.should_notify(
                analysis, pending_count=pending_at_start + stats.notified
            ):
                continue

            await self._dispatch(
                listing,
                analysis,
                stats,
                ignore_daily_alert_cap=ignore_daily_alert_cap,
                dashboard_generation=dashboard_generation,
            )

        log.info("Processed %d listings, %d new in range", stats.collected, stats.new)

        # A baseline now exists: later runs may treat new-to-us listings from
        # timestamp-less sources as fresh.
        self._baseline_ready = True
        return stats

    async def _dispatch(
        self,
        listing: Listing,
        analysis: DealAnalysis,
        stats: RunStats,
        *,
        ignore_daily_alert_cap: bool,
        dashboard_generation: int,
    ) -> None:
        """Route one qualifying deal to the dashboard and, if hot+fresh, Telegram."""

        log.info(
            "MATCH %s deal=%d fit=%d | %s",
            listing.uid,
            analysis.deal_score,
            analysis.fit_score,
            listing.title,
        )

        if dashboard_generation != self._dashboard_generation:
            log.info("Discarding stale dispatch for %s after dashboard clear", listing.uid)
            return
        if listing.is_ended():
            self._db.dequeue_from_dashboard(listing.uid)
            log.info("Skipping expired listing %s during dispatch", listing.uid)
            return

        if self._config.dry_run:
            cost = self._logistics.estimate(listing, analysis)
            total = f"{cost.total:.0f} SEK" if cost.total is not None else "n/a"
            nearby = " [nearby]" if cost.is_nearby else ""
            would_alert = self._prefs.should_alert(
                listing, analysis, baseline_ready=self._baseline_ready
            )
            log.info(
                "[dry-run] would list on dashboard%s: total ~%s via %s%s | %s",
                " + ALERT" if would_alert else "",
                total,
                cost.method,
                nearby,
                listing.url,
            )
            stats.notified += 1
            if would_alert:
                stats.alerted += 1
            return

        if self._dashboard is not None:
            # Primary feed: every qualifying deal is browsable on the dashboard.
            try:
                await self._dashboard.send_listing(listing, analysis)
                stats.notified += 1
            except Exception:  # noqa: BLE001 - one bad queue op shouldn't kill the run
                log.exception("Dashboard queue failed for %s", listing.uid)
            if dashboard_generation != self._dashboard_generation:
                return
            await self._maybe_alert(
                listing,
                analysis,
                stats,
                ignore_daily_cap=ignore_daily_alert_cap,
                dashboard_generation=dashboard_generation,
            )
            return

        # No dashboard configured: fall back to Telegram as the sole channel so
        # qualifying deals aren't silently dropped (degraded single-channel mode).
        if self._alerter is not None:
            try:
                await self._alerter.send_listing(listing, analysis)
                self._db.increment(_ALERT_COUNTER)
                stats.notified += 1
                stats.alerted += 1
            except Exception:  # noqa: BLE001 - a bad send shouldn't strand the rest
                log.exception("Telegram send failed for %s", listing.uid)
            return

        log.info("No notification channel configured; %s left unsent", listing.uid)

    async def _maybe_alert(
        self,
        listing: Listing,
        analysis: DealAnalysis,
        stats: RunStats,
        *,
        ignore_daily_cap: bool,
        dashboard_generation: int,
    ) -> None:
        """Send a Telegram push if the deal is exceptional and freshly posted."""

        if self._alerter is None:
            return
        if dashboard_generation != self._dashboard_generation:
            log.info("Skipping stale alert for %s after dashboard clear", listing.uid)
            return
        if self._alerts_muted:
            log.debug("Telegram muted; skipping alert for %s", listing.uid)
            return
        if not self._prefs.should_alert(listing, analysis, baseline_ready=self._baseline_ready):
            return

        per_run = self._config.alerts.max_per_run
        if per_run and stats.alerted >= per_run:
            log.info(
                "Per-run alert cap (%d) reached; skipping Telegram for %s", per_run, listing.uid
            )
            return
        per_day = self._config.alerts.max_per_day
        if not ignore_daily_cap and per_day and self._db.counter(_ALERT_COUNTER) >= per_day:
            log.info("Daily alert cap (%d) reached; skipping Telegram for %s", per_day, listing.uid)
            return

        try:
            await self._alerter.send_listing(listing, analysis)
        except Exception:  # noqa: BLE001 - a failed alert shouldn't strand the run
            log.exception("Telegram alert failed for %s", listing.uid)
            return
        self._db.increment(_ALERT_COUNTER)
        stats.alerted += 1
        log.info(
            "ALERT %s deal=%d fit=%d | %s",
            listing.uid,
            analysis.deal_score,
            analysis.fit_score,
            listing.title,
        )

    async def aclose(self) -> None:
        for source in self._sources:
            await source.aclose()
        await self._analyzer.aclose()
