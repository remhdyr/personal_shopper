"""Tests for the preference engine's notify decision and adaptive threshold."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from shopper.config import AlertsConfig, NotifyConfig
from shopper.models import DealAnalysis, Feedback, Listing, Source, Verdict
from shopper.preferences import PreferenceEngine


def _analysis(deal: int, fit: int, relevant: bool = True) -> DealAnalysis:
    return DealAnalysis(
        is_relevant=relevant, deal_score=deal, fit_score=fit, summary="x"
    )


def _listing_posted(minutes_ago: int | None) -> Listing:
    posted = None if minutes_ago is None else datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return Listing(
        source=Source.EBAY, source_id="a", title="t", url="https://e.com/a", posted_at=posted
    )


def _feedback(db, source_id: str, verdict: Verdict) -> None:
    ltng = Listing(
        source=Source.EBAY, source_id=source_id, title=f"i{source_id}",
        url=f"https://e.com/{source_id}",
    )
    db.save_listing(ltng)
    db.save_feedback(Feedback(listing_uid=ltng.uid, verdict=verdict))


def test_should_notify_requires_all_gates(db):
    engine = PreferenceEngine(db, NotifyConfig(min_deal_score=65, min_fit_score=55))
    assert engine.should_notify(_analysis(70, 60))
    assert not engine.should_notify(_analysis(60, 60))  # deal too low
    assert not engine.should_notify(_analysis(70, 50))  # fit too low
    assert not engine.should_notify(_analysis(90, 90, relevant=False))  # irrelevant


def test_adaptive_threshold_rises_with_likes(db):
    notify = NotifyConfig(min_deal_score=65, min_fit_score=55)
    engine = PreferenceEngine(db, notify)
    assert engine.adaptive_fit_threshold() == 55

    for i in range(5):
        _feedback(db, str(i), Verdict.INTERESTED)

    assert engine.adaptive_fit_threshold() == 60  # +1 per like


def test_adaptive_threshold_capped(db):
    notify = NotifyConfig(min_deal_score=65, min_fit_score=55)
    engine = PreferenceEngine(db, notify)
    for i in range(50):
        _feedback(db, str(i), Verdict.INTERESTED)
    # Capped at baseline + 20.
    assert engine.adaptive_fit_threshold() == 75


def test_too_expensive_raises_deal_bar(db):
    notify = NotifyConfig(min_deal_score=65, min_fit_score=55)
    engine = PreferenceEngine(db, notify)
    assert engine.adaptive_deal_threshold() == 65

    for i in range(3):
        _feedback(db, str(i), Verdict.TOO_EXPENSIVE)

    assert engine.adaptive_deal_threshold() == 68  # +1 per "too expensive"
    # "Too expensive" is a positive taste signal, so fit bar is unaffected.
    assert engine.adaptive_fit_threshold() == 55


def test_relief_relaxes_bars_when_feed_is_dry(db):
    notify = NotifyConfig(min_deal_score=65, min_fit_score=55, relief_target=8)
    engine = PreferenceEngine(db, notify)
    # A listing under both bars is rejected at full feed depth...
    assert not engine.should_notify(_analysis(60, 50))
    assert not engine.should_notify(_analysis(60, 50), pending_count=7)  # barely dry
    # ...but an empty feed relaxes the bars enough to let the best of what's
    # available through.
    assert engine.should_notify(_analysis(60, 50), pending_count=0)


def test_relief_never_floods_below_the_floor(db):
    notify = NotifyConfig(min_deal_score=65, min_fit_score=55, relief_target=8)
    engine = PreferenceEngine(db, notify)
    # Even with an empty feed the bars never fall more than the adapt cap below
    # the base, so far-below-bar junk still stays out.
    assert not engine.should_notify(_analysis(40, 50), pending_count=0)  # deal < 45 floor
    assert not engine.should_notify(_analysis(60, 30), pending_count=0)  # fit < 35 floor


def test_relief_disabled_when_target_is_zero(db):
    notify = NotifyConfig(min_deal_score=65, min_fit_score=55, relief_target=0)
    engine = PreferenceEngine(db, notify)
    assert not engine.should_notify(_analysis(60, 50), pending_count=0)


def test_few_shot_groups_graded_feedback(db):
    engine = PreferenceEngine(db, NotifyConfig())
    _feedback(db, "1", Verdict.INTERESTED)
    _feedback(db, "2", Verdict.TOO_EXPENSIVE)
    _feedback(db, "3", Verdict.POOR_CONDITION)
    _feedback(db, "4", Verdict.WRONG_TYPE)
    _feedback(db, "5", Verdict.NOT_MY_TASTE)

    ctx = engine.few_shot()
    liked_ids = {ltng.source_id for ltng in ctx.liked}
    disliked_ids = {ltng.source_id for ltng in ctx.disliked}
    assert liked_ids == {"1", "2", "3"}  # positive taste signals
    assert disliked_ids == {"4", "5"}
    assert {ltng.source_id for ltng in ctx.too_expensive} == {"2"}
    assert {ltng.source_id for ltng in ctx.poor_condition} == {"3"}


def test_should_alert_requires_hot_and_fresh(db):
    alerts = AlertsConfig(min_deal_score=80, min_fit_score=70, max_age_minutes=15)
    engine = PreferenceEngine(db, NotifyConfig(), alerts)
    fresh = _listing_posted(5)

    assert engine.should_alert(fresh, _analysis(85, 75), baseline_ready=True)
    assert not engine.should_alert(fresh, _analysis(70, 75), baseline_ready=True)  # deal too low
    assert not engine.should_alert(fresh, _analysis(85, 60), baseline_ready=True)  # fit too low
    stale = _listing_posted(120)
    assert not engine.should_alert(stale, _analysis(85, 75), baseline_ready=True)  # too old


def test_should_alert_without_timestamp_needs_baseline(db):
    engine = PreferenceEngine(db, NotifyConfig(), AlertsConfig())
    unknown = _listing_posted(None)  # source gave no posting time

    assert not engine.should_alert(unknown, _analysis(90, 90), baseline_ready=False)
    assert engine.should_alert(unknown, _analysis(90, 90), baseline_ready=True)


def test_should_alert_can_be_disabled(db):
    engine = PreferenceEngine(db, NotifyConfig(), AlertsConfig(enabled=False))
    assert not engine.should_alert(_listing_posted(1), _analysis(99, 99), baseline_ready=True)
