"""Preference learning and the notify decision.

Two responsibilities:
1. Pull recent feedback from the database to use as few-shot context for the AI
   (see :mod:`shopper.ai.gemini`). Feedback is graded: some verdicts mean "I like
   this kind of item, but not this deal" (too expensive / poor condition), which
   still counts as a positive taste signal while tightening price/condition bars.
2. Decide whether a scored listing clears the (adaptive) thresholds to notify.

Thresholds adapt to feedback volume: more "interested" votes make the fit bar
pickier, and more "too expensive" votes make the deal bar stricter, so
notifications stay useful instead of overwhelming.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import AlertsConfig, NotifyConfig
from .db import Database
from .models import (
    NEGATIVE_VERDICTS,
    POSITIVE_VERDICTS,
    DealAnalysis,
    Listing,
    Verdict,
)

_FEWSHOT_LIMIT = 8
_ADAPT_CAP = 20
# Each pending deal below the relief target lowers both dashboard bars by this
# many points, so a dry feed loosens itself instead of staying empty.
_RELIEF_PER_ITEM = 3


@dataclass
class PreferenceContext:
    """Recent feedback examples handed to the AI to personalise scoring."""

    liked: list[Listing] = field(default_factory=list)
    disliked: list[Listing] = field(default_factory=list)
    too_expensive: list[Listing] = field(default_factory=list)
    poor_condition: list[Listing] = field(default_factory=list)


class PreferenceEngine:
    def __init__(
        self, db: Database, notify: NotifyConfig, alerts: AlertsConfig | None = None
    ) -> None:
        self._db = db
        self._notify = notify
        self._alerts = alerts or AlertsConfig()

    def few_shot(self) -> PreferenceContext:
        """Gather recent feedback examples for AI context."""

        return PreferenceContext(
            liked=self._db.recent_feedback(POSITIVE_VERDICTS, _FEWSHOT_LIMIT),
            disliked=self._db.recent_feedback(NEGATIVE_VERDICTS, _FEWSHOT_LIMIT),
            too_expensive=self._db.recent_feedback(Verdict.TOO_EXPENSIVE, _FEWSHOT_LIMIT),
            poor_condition=self._db.recent_feedback(Verdict.POOR_CONDITION, _FEWSHOT_LIMIT),
        )

    def adaptive_fit_threshold(self) -> int:
        """Raise the fit bar as the user marks more items "interested".

        Each pure "interested" vote nudges the required fit score up by 1 point,
        capped at +20 over the configured baseline.
        """

        likes = len(self._db.recent_feedback(Verdict.INTERESTED, 100))
        return min(self._notify.min_fit_score + likes, self._notify.min_fit_score + _ADAPT_CAP)

    def adaptive_deal_threshold(self) -> int:
        """Raise the deal bar as the user marks more items "too expensive".

        This directly acts on the "right item, wrong price" signal so only
        stronger bargains get through, capped at +20 over the baseline.
        """

        too_pricey = len(self._db.recent_feedback(Verdict.TOO_EXPENSIVE, 100))
        return min(
            self._notify.min_deal_score + too_pricey, self._notify.min_deal_score + _ADAPT_CAP
        )

    def should_notify(self, analysis: DealAnalysis, *, pending_count: int | None = None) -> bool:
        if not analysis.is_relevant:
            return False
        relief = self._relief(pending_count)
        deal_bar = self._relaxed(
            self.adaptive_deal_threshold(), self._notify.min_deal_score, relief
        )
        fit_bar = self._relaxed(
            self.adaptive_fit_threshold(), self._notify.min_fit_score, relief
        )
        if analysis.deal_score < deal_bar:
            return False
        if analysis.fit_score < fit_bar:
            return False
        return True

    def _relief(self, pending_count: int | None) -> int:
        """Points to shave off both bars when the pending feed is running dry.

        Each deal short of ``relief_target`` lowers the bars a little, so a
        ratcheted-up threshold (or a quiet market) can't leave the dashboard
        empty. ``None`` (no feed-depth context) means no relief.
        """

        target = self._notify.relief_target
        if not target or pending_count is None:
            return 0
        return max(0, target - pending_count) * _RELIEF_PER_ITEM

    @staticmethod
    def _relaxed(threshold: int, base: int, relief: int) -> int:
        """Lower ``threshold`` by ``relief``, but never below a sane floor.

        The floor is one adapt-cap under the configured base, so relief loosens
        a dry feed without ever flooding it with far-below-bar listings.
        """

        return max(threshold - relief, max(0, base - _ADAPT_CAP))

    def should_alert(
        self, listing: Listing, analysis: DealAnalysis, *, baseline_ready: bool
    ) -> bool:
        """Whether to *push* a Telegram alert for this (already-qualifying) deal.

        A stricter, non-adaptive bar than :meth:`should_notify`, plus a
        freshness gate: alerts are only for exceptional deals that were *just*
        posted, so you can act fast. Callers pass listings that are new to us;
        ``baseline_ready`` is False only during the very first poll, so a
        cold-start backlog (whose posting time we can't verify) doesn't trigger
        a flood.
        """

        if not self._alerts.enabled:
            return False
        if not analysis.is_relevant:
            return False
        if analysis.deal_score < self._alerts.min_deal_score:
            return False
        if analysis.fit_score < self._alerts.min_fit_score:
            return False
        return self._is_fresh(listing, baseline_ready=baseline_ready)

    def _is_fresh(self, listing: Listing, *, baseline_ready: bool) -> bool:
        age = listing.age_minutes()
        if age is not None:
            # The source told us when it was posted: trust that exactly.
            return age <= self._alerts.max_age_minutes
        # No posting timestamp from this source. The caller only hands us
        # listings that are new to us, so once a baseline poll has run, "new to
        # us" means "appeared within one poll interval" -- close enough to fresh.
        return baseline_ready
