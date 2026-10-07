"""Application entrypoint.

Runs the Telegram bot (for feedback callbacks) and an interval scheduler (for
deal hunting) together on one asyncio loop. Use ``--once`` for a single pass,
handy for testing or running under cron instead of as a long-lived service.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .ai.base import PromptBuilder
from .ai.gemini import GeminiAnalyzer
from .ai.openai_compat import OpenAICompatAnalyzer
from .ai.router import RoutingAnalyzer, _Provider
from .config import AppConfig, ProviderConfig, Secrets, load
from .dashboard import Dashboard
from .db import Database
from .github_sync import WatchlistGitSync
from .healthcheck import run_checks
from .logging_setup import get_logger, setup_logging
from .logistics import Logistics
from .notifier import Notifier, build_notifier
from .pipeline import Pipeline
from .preferences import PreferenceEngine
from .sources.aterbygg import AterbyggSource
from .sources.auctionet import AuctionetSource
from .sources.base import Source
from .sources.blinto import BlintoSource
from .sources.blocket import BlocketSource
from .sources.ebay import EbaySource
from .sources.fleasy import FleasySource
from .sources.klaravik import KlaravikSource
from .sources.psauction import PSAuctionSource
from .sources.tradera import TraderaSource
from .triggers import ManualTrigger

log = get_logger(__name__)


def _build_sources(config: AppConfig, secrets: Secrets) -> list[Source]:
    sources: list[Source] = []
    if config.sources.ebay.enabled:
        if secrets.ebay_client_id and secrets.ebay_client_secret:
            sources.append(
                EbaySource(
                    client_id=secrets.ebay_client_id,
                    client_secret=secrets.ebay_client_secret,
                    marketplace=config.sources.ebay.marketplace,
                    env=secrets.ebay_env,
                    used_only=config.sources.ebay.used_only,
                )
            )
        else:
            log.warning("eBay enabled but credentials missing; skipping source")
    if config.sources.tradera.enabled:
        if secrets.tradera_app_id and secrets.tradera_app_key:
            sources.append(
                TraderaSource(
                    app_id=secrets.tradera_app_id, app_key=secrets.tradera_app_key
                )
            )
        else:
            log.warning("Tradera enabled but credentials missing; skipping source")
    if config.sources.auctionet.enabled:
        # Public read API: no credentials required.
        sources.append(AuctionetSource(currency=config.sources.auctionet.currency))
    if config.sources.klaravik.enabled:
        # Public auction listings — no credentials required.
        sources.append(KlaravikSource())
    if config.sources.blinto.enabled:
        # Public auction listings — no credentials required.
        sources.append(BlintoSource())
    if config.sources.psauction.enabled:
        # Public auction listings — no credentials required, but may be WAF-protected.
        sources.append(PSAuctionSource())
    if config.sources.blocket.enabled:
        # Uses public search-page structured data without a token; a captured
        # token opts into Blocket's internal search endpoint when configured.
        sources.append(BlocketSource(token=secrets.blocket_token))
    if config.sources.fleasy.enabled:
        # Public Shopify storefront — no credentials required.
        sources.append(FleasySource())
    if config.sources.aterbygg.enabled:
        # Public WooCommerce API — no credentials required.
        sources.append(AterbyggSource())
    return sources


def _provider_api_key(provider: ProviderConfig, secrets: Secrets) -> str:
    """The API key for a provider, looked up on Secrets by its name."""

    return getattr(secrets, f"{provider.name}_api_key", "")


def _build_analyzer(
    config: AppConfig, secrets: Secrets, db: Database
) -> RoutingAnalyzer:
    """Build every configured AI provider and wrap them in the router.

    Each provider gets its own :class:`PromptBuilder` (so they share no HTTP
    client). Providers without an API key are skipped, so a dormant entry
    (e.g. Qwen before ``QWEN_API_KEY`` is set) simply doesn't participate.
    """

    def prompts() -> PromptBuilder:
        return PromptBuilder(
            search=config.search,
            logistics=config.logistics,
            watchlist=config.watchlist,
            inventory=config.inventory,
        )

    providers: list[_Provider] = []
    for pc in config.ai.providers:
        api_key = _provider_api_key(pc, secrets)
        if not api_key:
            log.info("AI provider %s has no API key; skipping", pc.name)
            continue
        if pc.kind == "gemini":
            model = pc.model or secrets.gemini_model
            analyzer = GeminiAnalyzer(api_key=api_key, model=model, prompts=prompts())
        else:
            if not pc.base_url:
                log.warning("AI provider %s (openai_compat) has no base_url; skipping", pc.name)
                continue
            model = pc.model
            analyzer = OpenAICompatAnalyzer(
                base_url=pc.base_url, model=model, api_key=api_key, prompts=prompts()
            )
        providers.append(
            _Provider(
                analyzer=analyzer,
                name=pc.name,
                weight=pc.weight,
                rpm=pc.rpm,
                daily_limit=pc.daily_limit,
            )
        )

    if not providers:
        log.warning("No AI providers configured with keys; analyses will fall back")
    else:
        log.info("AI providers enabled: %s", ", ".join(p.name for p in providers))
    return RoutingAnalyzer(providers, db, mode=config.ai.mode)


def _build_pipeline(
    config: AppConfig,
    secrets: Secrets,
    db: Database,
    dashboard: Dashboard | None,
    alerter: Notifier | None,
) -> Pipeline:
    sources = _build_sources(config, secrets)
    analyzer = _build_analyzer(config, secrets, db)
    prefs = PreferenceEngine(db, config.notify, config.alerts)
    log.info("Enabled sources: %s", ", ".join(s.name for s in sources) or "none")
    return Pipeline(config, db, sources, analyzer, prefs, dashboard=dashboard, alerter=alerter)


async def _guarded_run(pipeline: Pipeline) -> None:
    try:
        await pipeline.run_once()
    except Exception:  # noqa: BLE001 - keep the scheduler alive across failures
        log.exception("Pipeline run failed")


async def _guarded_watchlist_sync(pipeline: Pipeline, sync: WatchlistGitSync) -> None:
    try:
        await pipeline.sync_watchlist_from_github(sync)
    except Exception:  # noqa: BLE001 - keep scheduled marketplace polls alive
        log.exception("GitHub watchlist sync failed")


async def run_service(config: AppConfig, secrets: Secrets) -> None:
    db = Database()
    logistics = Logistics(config.logistics)

    telegram: Notifier | None = None
    if secrets.telegram_token and secrets.telegram_chat_id:
        telegram = build_notifier(
            secrets.telegram_token,
            secrets.telegram_chat_id,
            db,
            logistics,
        )
    else:
        log.warning("Telegram not configured; alerts disabled (dashboard only)")

    dashboard: Dashboard | None = None
    if config.dashboard.enabled:
        dashboard = Dashboard(
            db,
            logistics,
            config.dashboard.host,
            config.dashboard.port,
            min_visible=config.dashboard.min_visible,
        )
    else:
        log.warning("Dashboard disabled; deals will only reach Telegram (if configured)")

    pipeline = _build_pipeline(config, secrets, db, dashboard, telegram)
    if dashboard is not None:
        dashboard.set_sources(pipeline.source_names)

    # Shared 'send more' trigger: lets either channel request an extra poll
    # right now, outside the normal schedule.
    trigger = ManualTrigger(pipeline)
    if telegram is not None:
        telegram.attach_trigger(trigger)
    if dashboard is not None:
        dashboard.attach_trigger(trigger)
        dashboard.attach_pipeline(pipeline)

    if telegram is not None:
        await telegram.app.initialize()
        await telegram.app.start()
        await telegram.app.updater.start_polling()
        await telegram.register_commands()
    if dashboard is not None:
        dashboard.start()

    # next_run_time=None would *pause* the job (APScheduler never computes a
    # fire time for it), which silently disables every future automatic run,
    # not just the immediate one. Instead give it a concrete first fire time
    # one interval from now, so it doesn't double-run alongside the manual
    # kick-off below but still fires on schedule after that.
    first_run_at = datetime.now() + timedelta(minutes=config.poll_interval_minutes)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _guarded_run,
        "interval",
        minutes=config.poll_interval_minutes,
        args=[pipeline],
        next_run_time=first_run_at,
    )
    watchlist_sync: WatchlistGitSync | None = None
    if config.github_sync.enabled and config.watchlist_path:
        watchlist_sync = WatchlistGitSync(config.watchlist_path, config.github_sync)
        scheduler.add_job(
            _guarded_watchlist_sync,
            "interval",
            minutes=config.github_sync.interval_minutes,
            args=[pipeline, watchlist_sync],
            next_run_time=datetime.now(),
        )
    scheduler.start()
    log.info("Scheduler started: every %d min", config.poll_interval_minutes)

    # Kick off an immediate first pass.
    await _guarded_run(pipeline)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    await stop.wait()
    log.info("Shutting down...")

    scheduler.shutdown(wait=False)
    if telegram is not None:
        await telegram.app.updater.stop()
        await telegram.app.stop()
        await telegram.app.shutdown()
    if dashboard is not None:
        dashboard.stop()
    await pipeline.aclose()
    db.close()


async def run_once(config: AppConfig, secrets: Secrets) -> None:
    db = Database()
    logistics = Logistics(config.logistics)

    telegram: Notifier | None = None
    if secrets.telegram_token and secrets.telegram_chat_id and not config.dry_run:
        telegram = build_notifier(
            secrets.telegram_token,
            secrets.telegram_chat_id,
            db,
            logistics,
        )
        await telegram.app.initialize()

    # The server isn't served for a one-shot run, but queuing still writes to the
    # shared DB so a long-running dashboard picks the deals up.
    dashboard: Dashboard | None = None
    if config.dashboard.enabled and not config.dry_run:
        dashboard = Dashboard(db, logistics, config.dashboard.host, config.dashboard.port)

    pipeline = _build_pipeline(config, secrets, db, dashboard, telegram)
    if dashboard is not None:
        dashboard.set_sources(pipeline.source_names)
    await _guarded_run(pipeline)
    await pipeline.aclose()
    if telegram is not None:
        await telegram.app.shutdown()
    db.close()


def main() -> None:
    setup_logging()
    config, secrets = load()
    args = sys.argv[1:]
    if "--check" in args:
        ok = asyncio.run(run_checks(config, secrets))
        sys.exit(0 if ok else 1)
    try:
        if "--once" in args:
            asyncio.run(run_once(config, secrets))
        else:
            asyncio.run(run_service(config, secrets))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
