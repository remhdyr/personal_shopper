"""On-demand 'send more' trigger, shared by Telegram and the local dashboard.

Both channels let the user request an extra poll right now, outside the
normal schedule -- something to click when bored. Runs are serialized (only
one at a time) and rate-limited with a short cooldown so rapid repeated
clicking can't retrigger the kind of Gemini rate-limit storm an unbounded
backlog once caused. A manual request bypasses the daily Telegram *alert* cap
(that's an anti-spam throttle, and clicking the button is the user explicitly
asking for more) but still respects the per-run alert cap and the AI analysis
budget, which protect real Gemini quota.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from .logging_setup import get_logger
from .pipeline import Pipeline

log = get_logger(__name__)

_COOLDOWN_SECONDS = 30.0


class ManualTrigger:
    def __init__(self, pipeline: Pipeline, cooldown_seconds: float = _COOLDOWN_SECONDS) -> None:
        self._pipeline = pipeline
        self._cooldown = cooldown_seconds
        self._lock = asyncio.Lock()
        self._last_run: float = 0.0

    async def trigger(
        self,
        source_names: list[str] | None = None,
        watch_item: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> str:
        """Run one extra pipeline pass and return a short human-readable summary.

        ``source_names`` optionally restricts the poll to specific sources (from
        the dashboard's source checkboxes); ``None`` queries all enabled sources.
        ``watch_item`` optionally restricts the poll to a single watchlist item's
        queries (the dashboard's per-item "Search now"); ``None`` runs all.
        ``on_progress``, if given, is called with short human-readable status
        strings as the run progresses (source X done, analyzing listing Y, ...)
        so a caller can show agile feedback instead of a single static message
        for the whole run.
        """

        if self._lock.locked():
            return "Already checking for deals, hang tight..."

        remaining = self._cooldown - (time.monotonic() - self._last_run)
        if remaining > 0:
            return f"Just checked a moment ago \u2014 try again in {remaining:.0f}s."

        async with self._lock:
            self._last_run = time.monotonic()
            log.info("Manual 'send more' trigger fired")
            try:
                stats = await self._pipeline.run_once(
                    ignore_daily_alert_cap=True,
                    source_names=source_names,
                    watch_item=watch_item,
                    on_progress=on_progress,
                )
            except Exception:  # noqa: BLE001 - never let a manual click crash the caller
                log.exception("Manual trigger run failed")
                return "Something went wrong while checking for deals. Check the logs."

        if stats.notified:
            alerted = f", {stats.alerted} alerted" if stats.alerted else ""
            return (
                f"Found {stats.notified} deal(s) on the dashboard"
                f"{alerted} ({stats.analyzed} analyzed, {stats.new} new)."
            )
        if stats.new == 0:
            return "Nothing new right now \u2014 try again later."
        return f"Checked {stats.new} new listing(s), nothing cleared the bar this time."
