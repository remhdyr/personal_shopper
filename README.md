# Shopper

A self-hosted service that hunts good deals on items in your watchlist
 analyzes each posting (price, description, images) with
**Google Gemini**, and shows them on a **hosted dashboard** with one-tap
*Interested / Not interested* buttons. Your feedback trains it to get pickier
over time so you don't get overwhelmed. When something exceptional is *just*
posted, it also pings you on **Telegram** so you can pounce.

Runs practically free: Gemini free tier + Telegram + local SQLite, hosted on a
machine you already keep on.

## How it works

```
Scheduler (every 10 min) ─▶ Source adapters (eBay / Tradera / Auctionet / Klaravik / Blinto / PS Auction / Blocket)
          ─▶ Dedup (SQLite)
          ─▶ Gemini analysis + personal-fit score
          ─▶ Above dashboard bar? ─▶ Hosted dashboard (browse any time over VPN)
                                        └▶ Also exceptional AND posted in the
                                           last ~10 min? ─▶ Telegram alert
          ─▶ Feedback (either channel) ─▶ future few-shot examples
```

The **dashboard is the primary interface**: a plain web page listing every deal
that cleared the (adaptive) bar, each with a photo, scores, landed-cost estimate
and the same feedback buttons. It binds to `0.0.0.0:8787` by default so you can
reach it over your private VPN (e.g. **Tailscale**) — the VPN, not localhost, is
what controls access. The dashboard has **no login or authentication**, so
never expose port 8787 to the public internet. Set `host: 127.0.0.1` under
`dashboard:` to keep it strictly local, or disable it entirely.

**Telegram is a push alert, not the main feed.** It only fires for the cream of
the crop: a deal that clears the higher `alerts:` bar *and* was posted within the
last few minutes, so you don't miss time-sensitive listings. Everything else
waits quietly on the dashboard. Tune the alert bar and freshness window under
`alerts:` in `config.yaml`.

Bored and want more right now instead of waiting for the next scheduled poll?
Type `/more` in Telegram, or click "🔄 Send more" on the dashboard — both
trigger an extra pass immediately (rate-limited to one every 30s so rapid
clicking can't overrun the Gemini free tier), and reply with a short summary
of what it found. A manual request ignores the daily Telegram *alert* cap
(you're explicitly asking for more, after all) but still respects the per-run
alert cap and the AI analysis budget, which protect actual Gemini quota.


## What you're hunting for

Your wish list lives in its own file, [`watchlist.yaml`](watchlist.yaml). Each
entry has search terms, brand/model keywords, plain-language notes ("prefer
Mitutoyo, avoid rusty lots"), an optional price ceiling and a priority. Those
terms drive the marketplace queries, and the whole entry is handed to Gemini so
it judges how well a listing fits *you*, not just whether a keyword matched.
Edit that file to change what the service looks for; `config.yaml` is only for
*how* it behaves (cadence, thresholds, logistics, sources).

When `github_sync.enabled` is set, the running service fetches the configured
Git remote every `interval_minutes`, commits dashboard edits to `watchlist.yaml`,
merges upstream changes, and pushes the result. A conflict limited to the
watchlist keeps the local dashboard version for conflicting lines while still
applying non-conflicting remote changes. Conflicts in application files are
never auto-resolved.

A companion file, [`inventory.yaml`](inventory.yaml), lists what you already
**own**. Shopper feeds it to Gemini so it can skip near-duplicates of gear you
have and give extra focus to *compatible tooling* — accessories whose taper,
bore, chuck mount or tool-post size fit your machines.

## Setup

1. Install [uv](https://docs.astral.sh/uv/) and Python 3.12+.
2. Copy `.env.example` to `.env` in the project root, then add credentials for
   the integrations you use:

   ```sh
   cp .env.example .env
   ```

   Fill in the credentials you need; leave unused optional entries blank.
   Gemini is required by the default AI configuration. Qwen is optional and
   stays inactive until its key is set. eBay and Tradera are skipped unless
   both credentials for that source are set. Telegram alerts require both
   Telegram values. Auctionet, Klaravik, Blinto, PS Auction, and Blocket's
   public search do not require credentials; `BLOCKET_TOKEN` is optional. The
   same names can instead be supplied as environment variables. Keep `.env`
   private and never commit it.
3. Edit [`watchlist.yaml`](watchlist.yaml) to describe what you're hunting for,
   and `config.yaml` to tune price bounds, thresholds and the alert window.
4. `uv sync` to install dependencies.
5. `uv run shopper` to start (begins in `dry_run` mode — logs candidates without
   posting until you flip `dry_run: false`).

## Status

Work in progress. See `deploy/shopper.service` for running it as a systemd
service.
