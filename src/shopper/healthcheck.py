"""Connectivity / credential health checks.

Run with ``shopper --check``. Each configured service is pinged with a minimal,
read-only request so you can confirm your ``.env`` is wired up correctly before
the first real run. Secrets are never printed.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from telegram import Bot
from telegram.error import TelegramError

from .ai.gemini import GeminiAnalyzer
from .config import AppConfig, Secrets
from .logging_setup import get_logger
from .models import Listing
from .models import Source as SourceEnum
from .sources.auctionet import AuctionetSource
from .sources.base import SearchQuery
from .sources.blinto import BlintoSource
from .sources.blocket import BlocketSource
from .sources.ebay import EbaySource
from .sources.klaravik import KlaravikSource
from .sources.psauction import PSAuctionSource
from .sources.tradera import TraderaSource

log = get_logger(__name__)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    skipped: bool = False


async def _check_telegram(secrets: Secrets) -> CheckResult:
    name = "Telegram"
    if not secrets.telegram_token or not secrets.telegram_chat_id:
        return CheckResult(name, False, "not configured", skipped=True)
    try:
        bot = Bot(token=secrets.telegram_token)
        me = await bot.get_me()
        chat = await bot.get_chat(chat_id=secrets.telegram_chat_id)
        return CheckResult(name, True, f"bot @{me.username}; chat '{chat.id}' reachable")
    except TelegramError as exc:
        return CheckResult(name, False, f"error: {exc}")


async def _check_gemini(config: AppConfig, secrets: Secrets) -> CheckResult:
    name = "Gemini"
    if not secrets.gemini_api_key:
        return CheckResult(name, False, "not configured", skipped=True)
    analyzer = GeminiAnalyzer(
        api_key=secrets.gemini_api_key,
        model=secrets.gemini_model,
        search=config.search,
        logistics=config.logistics,
        watchlist=config.watchlist,
        inventory=config.inventory,
    )
    probe = Listing(
        source=SourceEnum.EBAY,
        source_id="healthcheck",
        title="Mitutoyo digital caliper 150mm",
        description="Used digital caliper in working order, minor wear.",
        price=300,
        url="https://example.com/healthcheck",
    )
    try:
        analysis = await analyzer.analyze(probe)
        if analysis.summary == "analysis unavailable":
            return CheckResult(name, False, f"model {secrets.gemini_model} call failed")
        return CheckResult(
            name, True, f"model {secrets.gemini_model} responded (deal={analysis.deal_score})"
        )
    except Exception as exc:  # noqa: BLE001 - report any failure
        return CheckResult(name, False, f"error: {exc}")
    finally:
        await analyzer.aclose()


async def _check_tradera(config: AppConfig, secrets: Secrets) -> CheckResult:
    name = "Tradera"
    if not config.sources.tradera.enabled:
        return CheckResult(name, False, "disabled in config", skipped=True)
    if not secrets.tradera_app_id or not secrets.tradera_app_key:
        return CheckResult(name, False, "not configured", skipped=True)
    source = TraderaSource(app_id=secrets.tradera_app_id, app_key=secrets.tradera_app_key)
    try:
        results = await source.search(SearchQuery(text="skruvstäd", limit=1))
        return CheckResult(name, True, f"search OK ({len(results)} result(s) for probe)")
    except httpx.HTTPError as exc:
        return CheckResult(name, False, f"error: {exc}")
    finally:
        await source.aclose()


async def _check_ebay(config: AppConfig, secrets: Secrets) -> CheckResult:
    name = "eBay"
    if not config.sources.ebay.enabled:
        return CheckResult(name, False, "disabled in config", skipped=True)
    if not secrets.ebay_client_id or not secrets.ebay_client_secret:
        return CheckResult(name, False, "not configured", skipped=True)
    source = EbaySource(
        client_id=secrets.ebay_client_id,
        client_secret=secrets.ebay_client_secret,
        marketplace=config.sources.ebay.marketplace,
        env=secrets.ebay_env,
    )
    try:
        results = await source.search(SearchQuery(text="caliper", limit=1))
        return CheckResult(name, True, f"auth + search OK ({len(results)} result(s))")
    except httpx.HTTPError as exc:
        return CheckResult(name, False, f"error: {exc}")
    finally:
        await source.aclose()


async def _check_auctionet(config: AppConfig, _secrets: Secrets) -> CheckResult:
    name = "Auctionet"
    if not config.sources.auctionet.enabled:
        return CheckResult(name, False, "disabled in config", skipped=True)
    source = AuctionetSource(currency=config.sources.auctionet.currency)
    try:
        results = await source.search(SearchQuery(text="svarv", limit=1))
        return CheckResult(name, True, f"search OK ({len(results)} result(s) for probe)")
    except httpx.HTTPError as exc:
        return CheckResult(name, False, f"error: {exc}")
    finally:
        await source.aclose()


async def _check_klaravik(config: AppConfig, _secrets: Secrets) -> CheckResult:
    name = "Klaravik"
    if not config.sources.klaravik.enabled:
        return CheckResult(name, False, "disabled in config", skipped=True)
    source = KlaravikSource()
    try:
        results = await source.search(SearchQuery(text="svarv", limit=1))
        return CheckResult(name, True, f"search OK ({len(results)} result(s) for probe)")
    except httpx.HTTPError as exc:
        return CheckResult(name, False, f"error: {exc}")
    finally:
        await source.aclose()


async def _check_blinto(config: AppConfig, _secrets: Secrets) -> CheckResult:
    name = "Blinto"
    if not config.sources.blinto.enabled:
        return CheckResult(name, False, "disabled in config", skipped=True)
    source = BlintoSource()
    try:
        results = await source.search(SearchQuery(text="svarv", limit=1))
        return CheckResult(name, True, f"search OK ({len(results)} result(s) for probe)")
    except httpx.HTTPError as exc:
        return CheckResult(name, False, f"error: {exc}")
    finally:
        await source.aclose()


async def _check_psauction(config: AppConfig, _secrets: Secrets) -> CheckResult:
    name = "PS Auction"
    if not config.sources.psauction.enabled:
        return CheckResult(name, False, "disabled in config", skipped=True)
    source = PSAuctionSource()
    try:
        results = await source.search(SearchQuery(text="svarv", limit=1))
        return CheckResult(name, True, f"search OK ({len(results)} result(s) for probe)")
    except httpx.HTTPError as exc:
        return CheckResult(name, False, f"error: {exc}")
    finally:
        await source.aclose()


async def _check_blocket(config: AppConfig, secrets: Secrets) -> CheckResult:
    name = "Blocket"
    if not config.sources.blocket.enabled:
        return CheckResult(name, False, "disabled in config", skipped=True)
    source = BlocketSource(token=secrets.blocket_token)
    try:
        results = await source.search(SearchQuery(text="svarv", limit=1))
        mode = "token" if secrets.blocket_token else "public page"
        return CheckResult(name, True, f"{mode} search OK ({len(results)} result(s))")
    except httpx.HTTPError as exc:
        return CheckResult(name, False, f"error: {exc}")
    finally:
        await source.aclose()


async def run_checks(config: AppConfig, secrets: Secrets) -> bool:
    """Run all health checks. Returns True if nothing configured failed."""

    results = [
        await _check_telegram(secrets),
        await _check_gemini(config, secrets),
        await _check_tradera(config, secrets),
        await _check_ebay(config, secrets),
        await _check_auctionet(config, secrets),
        await _check_klaravik(config, secrets),
        await _check_blinto(config, secrets),
        await _check_psauction(config, secrets),
        await _check_blocket(config, secrets),
    ]

    log.info("--- Health check ---")
    any_failed = False
    for r in results:
        if r.skipped:
            symbol = "\u2013"  # en dash
        elif r.ok:
            symbol = "\u2713"  # check
        else:
            symbol = "\u2717"  # cross
            any_failed = True
        log.info("%s %-9s %s", symbol, r.name, r.detail)

    if any_failed:
        log.warning("Some configured services failed. Fix the above before running.")
    else:
        log.info("All configured services are reachable.")
    return not any_failed
