"""Tests for the SQLite persistence layer."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from shopper.db import Database
from shopper.models import DealAnalysis, Feedback, Listing, Source, Verdict


def test_dedup_filter_new(db, listing):
    assert db.filter_new([listing]) == [listing]
    db.save_listing(listing)
    assert db.filter_new([listing]) == []
    assert db.is_seen(listing.uid)


def test_feedback_roundtrip(db, listing):
    db.save_listing(listing)
    db.save_feedback(Feedback(listing_uid=listing.uid, verdict=Verdict.INTERESTED))
    liked = db.recent_feedback(Verdict.INTERESTED)
    assert [ltng.uid for ltng in liked] == [listing.uid]
    assert db.recent_feedback(Verdict.NOT_MY_TASTE) == []


def test_recent_feedback_accepts_multiple_verdicts(db):
    for i, verdict in enumerate((Verdict.INTERESTED, Verdict.TOO_EXPENSIVE)):
        ltng = Listing(
            source=Source.EBAY,
            source_id=str(i),
            title=f"item {i}",
            url=f"https://example.com/{i}",
        )
        db.save_listing(ltng)
        db.save_feedback(Feedback(listing_uid=ltng.uid, verdict=verdict))
    both = db.recent_feedback([Verdict.INTERESTED, Verdict.TOO_EXPENSIVE])
    assert {ltng.source_id for ltng in both} == {"0", "1"}
    assert db.recent_feedback([]) == []


def test_recent_feedback_order(db):
    for i in range(3):
        ltng = Listing(
            source=Source.EBAY,
            source_id=str(i),
            title=f"item {i}",
            url=f"https://example.com/{i}",
        )
        db.save_listing(ltng)
        db.save_feedback(Feedback(listing_uid=ltng.uid, verdict=Verdict.INTERESTED))
    liked = db.recent_feedback(Verdict.INTERESTED, limit=2)
    # Most recent first.
    assert [ltng.source_id for ltng in liked] == ["2", "1"]


def test_daily_counter(db):
    assert db.counter("ai") == 0
    assert db.increment("ai") == 1
    assert db.increment("ai") == 2
    assert db.counter("ai") == 2


def test_get_feedback_returns_verdict_or_none(db, listing):
    db.save_listing(listing)
    assert db.get_feedback(listing.uid) is None
    db.save_feedback(Feedback(listing_uid=listing.uid, verdict=Verdict.TOO_EXPENSIVE))
    assert db.get_feedback(listing.uid) is Verdict.TOO_EXPENSIVE


def test_analysis_roundtrip_preserves_model(db, listing):
    db.save_listing(listing)
    analysis = DealAnalysis(
        is_relevant=True,
        deal_score=80,
        fit_score=70,
        summary="solid",
        model="qwen-vl-max",
    )
    db.save_analysis(listing.uid, analysis)
    stored = db.get_analysis(listing.uid)
    assert stored is not None
    assert stored.model == "qwen-vl-max"
    assert stored.deal_score == 80


def test_model_usage_counts(db, listing):
    other = listing.model_copy(update={"source_id": "999"})
    db.save_listing(listing)
    db.save_listing(other)
    db.save_analysis(
        listing.uid,
        DealAnalysis(is_relevant=True, deal_score=1, fit_score=1, summary="a", model="gemini"),
    )
    db.save_analysis(
        other.uid,
        DealAnalysis(is_relevant=True, deal_score=1, fit_score=1, summary="b", model="gemini"),
    )
    assert db.model_usage_counts() == {"gemini": 2}


def test_migrate_adds_model_column_to_legacy_db(tmp_path):
    # Build a database with the pre-model analyses schema, then open it with
    # Database and confirm the migration backfills the column.
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE listings (uid TEXT PRIMARY KEY, source TEXT, source_id TEXT,
            title TEXT, price REAL, currency TEXT, url TEXT, data TEXT,
            seen_at TEXT);
        CREATE TABLE analyses (
            listing_uid TEXT PRIMARY KEY,
            deal_score INTEGER NOT NULL,
            fit_score INTEGER NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()

    database = Database(path)
    try:
        cols = {
            row["name"]
            for row in database._conn.execute("PRAGMA table_info(analyses)")
        }
        assert "model" in cols
        listing = Listing(source=Source.EBAY, source_id="1", title="x", url="https://e/1")
        database.save_listing(listing)
        database.save_analysis(
            listing.uid,
            DealAnalysis(
                is_relevant=True, deal_score=5, fit_score=5, summary="ok", model="gemini"
            ),
        )
        assert database.get_analysis(listing.uid).model == "gemini"
    finally:
        database.close()


def test_listings_for_watch_item_filters_by_tag(db):
    tagged = Listing(
        source=Source.EBAY,
        source_id="a",
        title="lathe",
        url="https://example.com/a",
        watch_item="Milling machine",
    )
    other = Listing(
        source=Source.EBAY,
        source_id="b",
        title="vise",
        url="https://example.com/b",
        watch_item="Bench vise",
    )
    untagged = Listing(
        source=Source.EBAY, source_id="c", title="misc", url="https://example.com/c"
    )
    for ltng in (tagged, other, untagged):
        db.save_listing(ltng)

    got = db.listings_for_watch_item("Milling machine")
    assert [ltng.uid for ltng in got] == [tagged.uid]
    assert db.listings_for_watch_item("Nonexistent") == []


def test_listings_for_watch_item_newest_first(db):
    for i in range(3):
        db.save_listing(
            Listing(
                source=Source.EBAY,
                source_id=str(i),
                title=f"item {i}",
                url=f"https://example.com/{i}",
                watch_item="Milling machine",
            )
        )
    got = db.listings_for_watch_item("Milling machine")
    assert [ltng.source_id for ltng in got] == ["2", "1", "0"]


def _analyzed(db, source_id: str, deal: int, fit: int, *, relevant: bool = True) -> Listing:
    ltng = Listing(
        source=Source.EBAY,
        source_id=source_id,
        title=f"item {source_id}",
        url=f"https://example.com/{source_id}",
    )
    db.save_listing(ltng)
    db.save_analysis(
        ltng.uid,
        DealAnalysis(is_relevant=relevant, deal_score=deal, fit_score=fit, summary="x"),
    )
    return ltng


def test_dashboard_pending_count(db, listing):
    db.save_listing(listing)
    assert db.dashboard_pending_count() == 0
    db.queue_for_dashboard(listing.uid)
    assert db.dashboard_pending_count() == 1
    db.dequeue_from_dashboard(listing.uid)
    assert db.dashboard_pending_count() == 0


def test_dashboard_pending_excludes_expired_auctions(db, listing):
    now = datetime.now(UTC)
    expired = listing.model_copy(
        update={
            "source_id": "expired",
            "ends_at": now - timedelta(minutes=1),
        }
    )
    future = listing.model_copy(
        update={
            "source_id": "future",
            "ends_at": now + timedelta(hours=1),
        }
    )
    undated = listing.model_copy(update={"source_id": "undated"})

    for item in (expired, future, undated):
        db.save_listing(item)
        db.queue_for_dashboard(item.uid)

    assert {item.uid for item in db.dashboard_pending()} == {future.uid, undated.uid}
    assert db.dashboard_pending_count() == 2


def test_clear_dashboard_removes_pending_and_backfill_listings(db, listing):
    db.save_listing(listing)
    db.save_analysis(
        listing.uid,
        DealAnalysis(is_relevant=True, deal_score=80, fit_score=80, summary="x"),
    )
    db.queue_for_dashboard(listing.uid)

    assert db.clear_dashboard() == 1
    assert db.dashboard_pending() == []
    assert not db.is_seen(listing.uid)


def test_top_unseen_listings_ranks_by_combined_score(db):
    _analyzed(db, "low", 50, 50)
    _analyzed(db, "best", 80, 75)
    _analyzed(db, "mid", 60, 60)
    got = db.top_unseen_listings()
    assert [ltng.source_id for ltng in got] == ["best", "mid", "low"]


def test_top_unseen_listings_excludes_acted_on_and_irrelevant(db):
    surfaced = _analyzed(db, "surfaced", 90, 90)
    db.mark_notified(surfaced.uid)  # already pushed to a channel
    rated = _analyzed(db, "rated", 88, 88)
    db.save_feedback(Feedback(listing_uid=rated.uid, verdict=Verdict.INTERESTED))
    hidden = _analyzed(db, "hidden", 85, 85)
    db.mark_dismissed(hidden.uid)
    _analyzed(db, "irrelevant", 95, 95, relevant=False)
    _analyzed(db, "fresh", 70, 70)  # never seen, relevant -> the only survivor

    got = db.top_unseen_listings()
    assert [ltng.source_id for ltng in got] == ["fresh"]


def test_top_unseen_listings_respects_limit(db):
    for i in range(5):
        _analyzed(db, str(i), 50 + i, 50)
    got = db.top_unseen_listings(limit=2)
    assert [ltng.source_id for ltng in got] == ["4", "3"]
