"""SQLite persistence: seen listings, AI analyses, feedback and daily counters.

A tiny hand-rolled layer (stdlib ``sqlite3``) keeps the service dependency-light
and the database file trivially portable/backup-able.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from .models import DealAnalysis, Feedback, Listing, Verdict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    uid         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    title       TEXT NOT NULL,
    price       REAL,
    currency    TEXT,
    url         TEXT NOT NULL,
    data        TEXT NOT NULL,      -- full Listing JSON
    seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS analyses (
    listing_uid TEXT PRIMARY KEY REFERENCES listings(uid),
    deal_score  INTEGER NOT NULL,
    fit_score   INTEGER NOT NULL,
    data        TEXT NOT NULL,      -- full DealAnalysis JSON
    model       TEXT NOT NULL DEFAULT '',  -- which AI model produced the verdict
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notifications (
    listing_uid TEXT PRIMARY KEY REFERENCES listings(uid),
    sent_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feedback (
    listing_uid TEXT PRIMARY KEY REFERENCES listings(uid),
    verdict     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_counters (
    day     TEXT NOT NULL,
    name    TEXT NOT NULL,
    value   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, name)
);

CREATE TABLE IF NOT EXISTS dashboard_queue (
    listing_uid TEXT PRIMARY KEY REFERENCES listings(uid),
    queued_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dismissed (
    listing_uid  TEXT PRIMARY KEY REFERENCES listings(uid),
    dismissed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    """Thin sqlite3 wrapper, safe to share across threads.

    The pipeline runs on the main asyncio loop while the local dashboard's
    ``ThreadingHTTPServer`` handles each request on its own thread; both need
    to use the same database. sqlite3 connections aren't thread-safe by
    default, so ``check_same_thread`` is disabled and every access is
    serialised with a lock.
    """

    def __init__(self, path: str | Path = "data/shopper.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        ``executescript`` only creates tables that don't yet exist, so a
        database from before a column was added keeps the old shape. Bring such
        databases forward with idempotent ALTERs guarded by a schema probe.
        """

        with self._lock:
            cols = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(analyses)")
            }
            if "model" not in cols:
                self._conn.execute(
                    "ALTER TABLE analyses ADD COLUMN model TEXT NOT NULL DEFAULT ''"
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- listings / dedup ---------------------------------------------------

    def is_seen(self, uid: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM listings WHERE uid = ?", (uid,)
            ).fetchone()
        return row is not None

    def filter_new(self, listings: Iterable[Listing]) -> list[Listing]:
        """Return only listings not already stored."""

        return [ltng for ltng in listings if not self.is_seen(ltng.uid)]

    def has_any_listings(self) -> bool:
        """True if we've ever stored a listing (i.e. not a cold start)."""

        with self._lock:
            row = self._conn.execute("SELECT 1 FROM listings LIMIT 1").fetchone()
        return row is not None

    def save_listing(self, listing: Listing) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO listings
                    (uid, source, source_id, title, price, currency, url, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing.uid,
                    listing.source.value,
                    listing.source_id,
                    listing.title,
                    listing.price,
                    listing.currency,
                    listing.url,
                    listing.model_dump_json(),
                ),
            )
            self._conn.commit()

    # --- analyses -----------------------------------------------------------

    def save_analysis(self, listing_uid: str, analysis: DealAnalysis) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO analyses
                    (listing_uid, deal_score, fit_score, data, model)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    listing_uid,
                    analysis.deal_score,
                    analysis.fit_score,
                    analysis.model_dump_json(),
                    analysis.model,
                ),
            )
            self._conn.commit()

    def model_usage_counts(self) -> dict[str, int]:
        """How many stored analyses each model produced (for diagnostics)."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT model, COUNT(*) AS n FROM analyses GROUP BY model"
            ).fetchall()
        return {row["model"]: int(row["n"]) for row in rows}

    # --- notifications ------------------------------------------------------

    def mark_notified(self, listing_uid: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO notifications (listing_uid) VALUES (?)",
                (listing_uid,),
            )
            self._conn.commit()

    # --- feedback -----------------------------------------------------------

    def save_feedback(self, feedback: Feedback) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO feedback (listing_uid, verdict)
                VALUES (?, ?)
                """,
                (feedback.listing_uid, feedback.verdict.value),
            )
            self._conn.commit()

    def recent_feedback(
        self, verdict: Verdict | Iterable[Verdict], limit: int = 10
    ) -> list[Listing]:
        """Most recent listings the user reacted to with the given verdict(s)."""

        verdicts = [verdict] if isinstance(verdict, Verdict) else list(verdict)
        if not verdicts:
            return []
        placeholders = ",".join("?" * len(verdicts))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT l.data AS data
                FROM feedback f
                JOIN listings l ON l.uid = f.listing_uid
                WHERE f.verdict IN ({placeholders})
                ORDER BY f.created_at DESC, f.rowid DESC
                LIMIT ?
                """,
                (*[v.value for v in verdicts], limit),
            ).fetchall()
        return [Listing.model_validate(json.loads(r["data"])) for r in rows]

    # --- daily counters -----------------------------------------------------

    def counter(self, name: str, day: date | None = None) -> int:
        day = day or date.today()
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM daily_counters WHERE day = ? AND name = ?",
                (day.isoformat(), name),
            ).fetchone()
        return int(row["value"]) if row else 0

    def increment(self, name: str, day: date | None = None) -> int:
        day = day or date.today()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO daily_counters (day, name, value) VALUES (?, ?, 1)
                ON CONFLICT(day, name) DO UPDATE SET value = value + 1
                """,
                (day.isoformat(), name),
            )
            self._conn.commit()
        return self.counter(name, day)

    # --- local dashboard fallback queue --------------------------------------

    def queue_for_dashboard(self, listing_uid: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO dashboard_queue (listing_uid) VALUES (?)",
                (listing_uid,),
            )
            self._conn.commit()

    def dashboard_pending(self) -> list[Listing]:
        """Listings queued for the local dashboard, most recently queued first."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT l.data AS data
                FROM dashboard_queue q
                JOIN listings l ON l.uid = q.listing_uid
                ORDER BY q.queued_at DESC, q.rowid DESC
                """
            ).fetchall()
        return [
            listing
            for r in rows
            if not (listing := Listing.model_validate(json.loads(r["data"]))).is_ended()
        ]

    def dashboard_pending_count(self) -> int:
        """How many deals are currently queued for the dashboard (feed depth)."""

        return len(self.dashboard_pending())

    def top_unseen_listings(self, limit: int = 60) -> list[Listing]:
        """Best-scored stored listings the user hasn't seen or acted on yet.

        Backfill source so the dashboard is never empty: analysed, relevant
        listings that were stored but never surfaced (not notified), never rated
        and never dismissed, ranked by combined deal+fit score. Callers filter
        out ended auctions (``ends_at`` lives in the JSON blob).
        """

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT l.data AS data
                FROM listings l
                JOIN analyses a ON a.listing_uid = l.uid
                WHERE json_extract(a.data, '$.is_relevant') = 1
                  AND l.uid NOT IN (SELECT listing_uid FROM notifications)
                  AND l.uid NOT IN (SELECT listing_uid FROM feedback)
                  AND l.uid NOT IN (SELECT listing_uid FROM dismissed)
                ORDER BY (a.deal_score + a.fit_score) DESC, l.seen_at DESC, l.rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [Listing.model_validate(json.loads(r["data"])) for r in rows]

    def get_analysis(self, listing_uid: str) -> DealAnalysis | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM analyses WHERE listing_uid = ?", (listing_uid,)
            ).fetchone()
        return DealAnalysis.model_validate(json.loads(row["data"])) if row else None

    def get_feedback(self, listing_uid: str) -> Verdict | None:
        """The verdict recorded for a listing, or ``None`` if never rated."""

        with self._lock:
            row = self._conn.execute(
                "SELECT verdict FROM feedback WHERE listing_uid = ?", (listing_uid,)
            ).fetchone()
        return Verdict(row["verdict"]) if row else None

    def listings_for_watch_item(self, watch_item: str, limit: int = 200) -> list[Listing]:
        """Every stored listing tagged with the given watch item, newest first.

        Independent of the dashboard queue and of feedback: returns all offers
        we've ever seen for this item (pending, rated *or* dismissed). Callers
        filter out ended auctions. ``watch_item`` is stored inside the Listing
        JSON blob, so we match it with ``json_extract``.
        """

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT data FROM listings
                WHERE json_extract(data, '$.watch_item') = ?
                ORDER BY seen_at DESC, rowid DESC
                LIMIT ?
                """,
                (watch_item, limit),
            ).fetchall()
        return [Listing.model_validate(json.loads(r["data"])) for r in rows]

    def dequeue_from_dashboard(self, listing_uid: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM dashboard_queue WHERE listing_uid = ?", (listing_uid,)
            )
            self._conn.commit()

    def clear_dashboard(self) -> int:
        """Clear queued and below-bar listings currently shown by the dashboard.

        Listings removed from the dashboard are deleted from the deduplication
        store so a subsequent manual search can re-evaluate them against an
        updated watchlist. User feedback is retained if it exists.
        """

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT listing_uid FROM dashboard_queue
                UNION
                SELECT l.uid
                FROM listings l
                JOIN analyses a ON a.listing_uid = l.uid
                WHERE json_extract(a.data, '$.is_relevant') = 1
                  AND l.uid NOT IN (SELECT listing_uid FROM notifications)
                  AND l.uid NOT IN (SELECT listing_uid FROM feedback)
                  AND l.uid NOT IN (SELECT listing_uid FROM dismissed)
                """
            ).fetchall()
            uids = [row["listing_uid"] for row in rows]
            if uids:
                placeholders = ",".join("?" * len(uids))
                self._conn.execute(
                    f"DELETE FROM dashboard_queue WHERE listing_uid IN ({placeholders})",
                    uids,
                )
                self._conn.execute(
                    f"DELETE FROM notifications WHERE listing_uid IN ({placeholders})",
                    uids,
                )
                self._conn.execute(
                    f"DELETE FROM analyses WHERE listing_uid IN ({placeholders})"
                    " AND listing_uid NOT IN (SELECT listing_uid FROM feedback)",
                    uids,
                )
                self._conn.execute(
                    f"DELETE FROM listings WHERE uid IN ({placeholders})"
                    " AND uid NOT IN (SELECT listing_uid FROM feedback)",
                    uids,
                )
            self._conn.commit()
        return len(uids)

    def mark_dismissed(self, listing_uid: str) -> None:
        """Record that the user hid a listing, so backfill won't resurface it."""

        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO dismissed (listing_uid) VALUES (?)",
                (listing_uid,),
            )
            self._conn.commit()
